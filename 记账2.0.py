#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
个人记账插件（ElainaBot v2 适配版）
功能：收入/支出记录、余额管理、按日/周/月汇总查询、详情分页、记录删除
存储：本地 SQLite 数据库，无需外部 MySQL
"""

import os
import sqlite3
from datetime import datetime, timedelta

from core.base.logger import PLUGIN, get_logger, report_error
from core.plugin.decorators import handler, on_load

log = get_logger(PLUGIN, '个人记账')

__plugin_meta__ = {
    'name': '个人记账',
    'author': '适配版',
    'description': '群聊/私聊通用的个人记账工具，支持收支记录、余额管理、多维度查询、分页详情',
    'version': '2.1.0',
}

# ==================== 路径配置 ====================
_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE, 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, 'account.db')


# ==================== 数据库基础工具 ====================
def _connect():
    """获取 SQLite 连接（行工厂为 Row，可按列名取值）"""
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, params=()):
    """执行 SELECT，返回 dict 列表；失败抛 sqlite3.Error 由调用方捕获"""
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _balance_upsert_sql():
    """余额 UPSERT：行不存在则以 0 为基准插入，存在则在原值上叠加增量"""
    return (
        'INSERT INTO account_config (user_id, "key", value) VALUES (?, \'balance\', ?) '
        'ON CONFLICT(user_id, "key") DO UPDATE SET value = value + excluded.value'
    )


_tables_ready = False


def _ensure_tables() -> bool:
    """初始化表结构（仅在进程内首次调用时真正执行，避免每条消息重复建表）"""
    global _tables_ready
    if _tables_ready:
        return True
    tables_sql = [
        """
        CREATE TABLE IF NOT EXISTS account_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            timestamp DATETIME NOT NULL DEFAULT (datetime('now', 'localtime')),
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            remark TEXT
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_user_time ON account_records (user_id, timestamp);",
        """
        CREATE TABLE IF NOT EXISTS account_config (
            user_id TEXT NOT NULL,
            "key" TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (user_id, "key")
        );
        """,
    ]
    conn = _connect()
    try:
        with conn:
            for sql in tables_sql:
                conn.execute(sql)
        _tables_ready = True
        return True
    except sqlite3.Error as e:
        log.error(f'创建表结构失败：{e}')
        return False
    finally:
        conn.close()


# ==================== 账目原子操作 ====================
def _add_record(user_id: str, rec_type: str, amount: float, category: str, remark: str) -> int:
    """插入一条收支记录并原子调整余额，返回新记录 id。

    余额行不存在时自动以 0 为基准创建（收入加、支出减）。插入与余额更新在
    同一事务内完成，任一步失败整体回滚，避免明细与余额对不上。
    """
    delta = amount if rec_type == '收入' else -amount
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                'INSERT INTO account_records (user_id, type, amount, category, remark) '
                'VALUES (?, ?, ?, ?, ?)',
                (user_id, rec_type, amount, category, remark),
            )
            record_id = cur.lastrowid
            conn.execute(_balance_upsert_sql(), (user_id, delta))
        return record_id
    finally:
        conn.close()


def _delete_record(user_id: str, record_id: str):
    """删除记录并回滚余额，返回 (是否找到, 类型, 金额)。整体在一个事务内完成。"""
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                'SELECT type, amount FROM account_records WHERE id = ? AND user_id = ?',
                (record_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return (False, None, 0.0)
            rec_type = row['type']
            amount = float(row['amount'])
            # 回滚余额：删收入则减，删支出则加
            delta = -amount if rec_type == '收入' else amount
            conn.execute(_balance_upsert_sql(), (user_id, delta))
            conn.execute(
                'DELETE FROM account_records WHERE id = ? AND user_id = ?',
                (record_id, user_id),
            )
        return (True, rec_type, amount)
    finally:
        conn.close()


def _set_balance(user_id: str, value: float):
    """覆盖式设置余额，返回 (是否为新建, 最终余额)。"""
    conn = _connect()
    try:
        with conn:
            existed = conn.execute(
                'SELECT 1 FROM account_config WHERE user_id = ? AND "key" = \'balance\'',
                (user_id,),
            ).fetchone() is not None
            conn.execute(
                'INSERT INTO account_config (user_id, "key", value) VALUES (?, \'balance\', ?) '
                'ON CONFLICT(user_id, "key") DO UPDATE SET value = excluded.value',
                (user_id, value),
            )
        return (not existed, value)
    finally:
        conn.close()


def _get_balance(user_id: str) -> float:
    rows = _query(
        'SELECT value FROM account_config WHERE user_id = ? AND "key" = \'balance\'',
        (user_id,),
    )
    return float(rows[0]['value']) if rows else 0.0


# ==================== 分页详情查询工具 ====================
def _get_detail_data(user_id: str, date_cond: str, date_params, page: int = 1, page_size: int = 20):
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    params = (user_id,) + date_params + (offset, page_size)
    data_sql = """
        SELECT
            id,
            type,
            amount,
            category,
            strftime('%Y-%m-%d %H:%M:%S', timestamp) AS time,
            remark
        FROM account_records
        WHERE user_id = ? AND {date_cond}
        ORDER BY id ASC
        LIMIT ?, ?
    """.format(date_cond=date_cond)
    data = _query(data_sql, params)

    count_sql = """
        SELECT COUNT(*) AS total FROM account_records
        WHERE user_id = ? AND {date_cond}
    """.format(date_cond=date_cond)
    count_result = _query(count_sql, (user_id,) + date_params)
    total = count_result[0]['total'] if count_result else 0
    total_page = (total + page_size - 1) // page_size
    return (data, total, total_page)


def _format_records(data) -> list:
    """把记录列表格式化为文本行（三个详情命令共用，避免重复）"""
    lines = []
    for record in data:
        remark = record['remark'] if record['remark'] else '无'
        lines.append(
            f"ID:{record['id']} | {record['type']} | {record['amount']:.2f}元 | {record['category']} | "
            f"时间：{record['time']} | 备注：{remark}"
        )
    return lines


def _parse_page(match) -> int:
    raw = match.group(1)
    return int(raw) if raw else 1


# ==================== 删除记录 ====================
@handler(r"^删除记录\s*(\d+)$", ignore_at_check=True)
async def handle_delete(event, match):
    try:
        user_id = event.user_id
        record_id = match.group(1)

        if not _ensure_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        found, record_type, amount = _delete_record(user_id, record_id)
        if not found:
            await event.reply(f"❌ 未找到ID为{record_id}的记录（仅能删除自己的记录）")
            return

        balance_change = amount if record_type == "收入" else -amount
        await event.reply(f"""\n✅ 记录ID {record_id} 删除成功！
{record_type}金额：{amount:.2f}元
余额已{"减少" if record_type == "收入" else "增加"} {abs(balance_change):.2f}元""")

    except Exception as e:
        await event.reply(f"处理删除失败：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 收入记录 ====================
@handler(r"^收入\s*(\d+\.?\d*)\s*([^0-9\s]+)\s*([\s\S]*)$", ignore_at_check=True)
async def handle_income(event, match):
    try:
        user_id = event.user_id
        amount, category, remark = match.groups()

        if not _ensure_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        record_id = _add_record(user_id, '收入', float(amount), category, remark)
        await event.reply(f"""\n✅ 收入记录成功！
记录ID：{record_id}
金额：{float(amount):.2f} 元
分类：{category}
备注：{remark or '无'}""")

    except Exception as e:
        await event.reply(f"处理错误：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 支出记录 ====================
@handler(r"^支出\s*(\d+\.?\d*)\s*([^0-9\s]+)\s*([\s\S]*)$", ignore_at_check=True)
async def handle_expense(event, match):
    try:
        user_id = event.user_id
        amount, category, remark = match.groups()

        if not _ensure_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        record_id = _add_record(user_id, '支出', float(amount), category, remark)
        await event.reply(f"""\n✅ 支出记录成功！
记录ID：{record_id}
金额：{float(amount):.2f} 元
分类：{category}
备注：{remark or '无'}""")

    except Exception as e:
        await event.reply(f"处理错误：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 当日详情 ====================
@handler(r"^当日详情(?:\s*(\d+))?$", ignore_at_check=True)
async def handle_day_detail(event, match):
    try:
        user_id = event.user_id
        page = _parse_page(match)

        if not _ensure_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        data, total, total_page = _get_detail_data(
            user_id, "date(timestamp) = ?", (today,), page, page_size=100
        )

        if total == 0:
            await event.reply(f"\n📅 {today} 暂无收支记录")
            return

        detail_text = [f"\n📝 {today} 当日详情（第{page}/{total_page}页，共{total}条）"]
        detail_text.extend(_format_records(data))
        await event.reply("\n\n".join(detail_text))

        if total_page > 1:
            await event.reply(f"💡 分页提示：发送「当日详情{page+1}」查看下一页（每页100条）")

    except Exception as e:
        await event.reply(f"处理当日详情失败：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 本周详情 ====================
@handler(r"^本周详情(?:\s*(\d+))?$", ignore_at_check=True)
async def handle_week_detail(event, match):
    try:
        user_id = event.user_id
        page = _parse_page(match)

        if not _ensure_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        monday_str = monday.strftime("%Y-%m-%d 00:00:00")
        sunday_str = sunday.strftime("%Y-%m-%d 23:59:59")
        data, total, total_page = _get_detail_data(
            user_id, "timestamp BETWEEN ? AND ?", (monday_str, sunday_str), page, page_size=20
        )

        date_label = f"{monday_str.split(' ')[0]}-{sunday_str.split(' ')[0]}"
        if total == 0:
            await event.reply(f"\n📅 {date_label} 本周暂无收支记录")
            return

        detail_text = [f"\n📝 {date_label} 本周详情（第{page}/{total_page}页，共{total}条）"]
        detail_text.extend(_format_records(data))
        await event.reply("\n\n".join(detail_text))

        nav_tips = []
        if page > 1:
            nav_tips.append(f"发送「本周详情{page-1}」查看上一页")
        if page < total_page:
            nav_tips.append(f"发送「本周详情{page+1}」查看下一页")
        if nav_tips:
            await event.reply("💡 " + " | ".join(nav_tips))

    except Exception as e:
        await event.reply(f"处理本周详情失败：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 本月详情 ====================
@handler(r"^本月详情(?:\s*(\d+))?$", ignore_at_check=True)
async def handle_month_detail(event, match):
    try:
        user_id = event.user_id
        page = _parse_page(match)

        if not _ensure_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        current_month = datetime.now().strftime("%Y-%m")
        data, total, total_page = _get_detail_data(
            user_id, "strftime('%Y-%m', timestamp) = ?", (current_month,), page, page_size=20
        )

        if total == 0:
            await event.reply(f"\n📅 {current_month} 本月暂无收支记录")
            return

        detail_text = [f"\n📝 {current_month} 本月详情（第{page}/{total_page}页，共{total}条）"]
        detail_text.extend(_format_records(data))
        await event.reply("\n\n".join(detail_text))

        nav_tips = []
        if page > 1:
            nav_tips.append(f"发送「本月详情{page-1}」查看上一页")
        if page < total_page:
            nav_tips.append(f"发送「本月详情{page+1}」查看下一页")
        if nav_tips:
            await event.reply("💡 " + " | ".join(nav_tips))

    except Exception as e:
        await event.reply(f"处理本月详情失败：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 设置余额 ====================
@handler(r"^设置余额\s*(\d+\.?\d*)$", ignore_at_check=True)
async def handle_init_balance(event, match):
    try:
        user_id = event.user_id
        amount = float(match.group(1))

        if not _ensure_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        created, final_balance = _set_balance(user_id, amount)
        if created:
            await event.reply(f"⚠️ 已自动初始化并设置余额为 {final_balance:.2f} 元")
        else:
            await event.reply(f"✅ 初始余额已设置为：{final_balance:.2f} 元")

    except Exception as e:
        await event.reply(f"处理错误：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 汇总查询 ====================
@handler(r"^查询\s*(当日|当周|当月)$", ignore_at_check=True)
async def handle_query(event, match):
    try:
        user_id = event.user_id
        scope = match.group(1)

        if not _ensure_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        today = datetime.now()
        if scope == "当日":
            date_cond = "date(timestamp) = ?"
            date_params = (today.strftime("%Y-%m-%d"),)
            time_desc = today.strftime("%Y年%m月%d日")
        elif scope == "当周":
            monday = today - timedelta(days=today.weekday())
            sunday = monday + timedelta(days=6)
            date_cond = "timestamp BETWEEN ? AND ?"
            date_params = (monday.strftime("%Y-%m-%d 00:00:00"), sunday.strftime("%Y-%m-%d 23:59:59"))
            time_desc = f"{monday.strftime('%m月%d日')}-{sunday.strftime('%m月%d日')}"
        else:
            date_cond = "strftime('%Y-%m', timestamp) = ?"
            date_params = (today.strftime("%Y-%m"),)
            time_desc = today.strftime("%Y年%m月")

        sum_sql = f"""
            SELECT ifnull(SUM(amount), 0) AS total, COUNT(*) AS count
            FROM account_records
            WHERE user_id = ? AND type = ? AND {date_cond}
        """
        income_res = _query(sum_sql, (user_id, '收入') + date_params)
        expense_res = _query(sum_sql, (user_id, '支出') + date_params)

        total_income = income_res[0]["total"] if income_res else 0.0
        income_count = income_res[0]["count"] if income_res else 0
        total_expense = expense_res[0]["total"] if expense_res else 0.0
        expense_count = expense_res[0]["count"] if expense_res else 0
        current_balance = _get_balance(user_id)

        await event.reply(f"""\n📅 {time_desc} 汇总：
📥 总收入：{float(total_income):.2f} 元（{income_count}条记录）
📤 总支出：{float(total_expense):.2f} 元（{expense_count}条记录）
💰 当前余额：{float(current_balance):.2f} 元""")

    except Exception as e:
        await event.reply(f"处理错误：{str(e)}")
        report_error(PLUGIN, '个人记账', e)


# ==================== 帮助菜单 ====================
@handler(r"^记账帮助$", ignore_at_check=True)
@handler(r"^记账菜单$", ignore_at_check=True)
async def handle_menu(event, match):
    await event.reply("""\n📋 记账机器人操作指南：

1. 基础操作
   ▶ 设置余额 金额（例：设置余额 1000.50）
   ▶ 收入 金额 分类 [备注]（例：收入 2000 工资）
   ▶ 支出 金额 分类 [备注]（例：支出 50 餐饮）
   ▶ 删除记录 ID（例：删除记录 123，ID从详情中获取）

2. 汇总查询
   ▶ 查询 当日/当周/当月（例：查询 当日）

3. 详情查询（按ID排序）
   ▶ 当日详情 [页码]（例：当日详情 / 当日详情2，每页100条）
   ▶ 本周详情 [页码]（例：本周详情 / 本周详情3，每页20条）
   ▶ 本月详情 [页码]（例：本月详情 / 本月详情4，每页20条）

💡 提示：删除记录后，余额会自动回滚""")


# ==================== 插件生命周期 ====================
@on_load
async def _init():
    if _ensure_tables():
        log.info("个人记账插件已加载，数据库初始化完成")
    else:
        log.error("个人记账插件加载完成，但数据库初始化失败")
