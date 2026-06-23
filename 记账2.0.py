#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
个人记账插件（ElainaBot v2 适配版）
功能：收入/支出记录、余额管理、按日/周/月汇总查询、详情分页、记录删除
存储：本地 SQLite 数据库，无需外部 MySQL
"""

import os
import re
import sqlite3
import traceback
from datetime import datetime, timedelta

from core.plugin.decorators import handler, on_load

__plugin_meta__ = {
    'name': '个人记账',
    'author': '适配版',
    'description': '群聊/私聊通用的个人记账工具，支持收支记录、余额管理、多维度查询、分页详情',
    'version': '2.0.0',
}

# ==================== 路径配置 ====================
_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE, 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, 'account.db')

# ==================== 正则表达式 ====================
PATTERNS = {
    "income": re.compile(r"^收入\s*(\d+\.?\d*)\s*([^0-9\s]+)\s*([\s\S]*)$"),
    "expense": re.compile(r"^支出\s*(\d+\.?\d*)\s*([^0-9\s]+)\s*([\s\S]*)$"),
    "query": re.compile(r"^查询\s*(当日|当周|当月)$"),
    "init_balance": re.compile(r"^设置余额\s*(\d+\.?\d*)$"),
    "day_detail": re.compile(r"^当日详情(?:\s*(\d+))?$"),
    "week_detail": re.compile(r"^本周详情(?:\s*(\d+))?$"),
    "month_detail": re.compile(r"^本月详情(?:\s*(\d+))?$"),
    "delete": re.compile(r"^删除记录\s*(\d+)$")
}

# ==================== 数据库基础工具 ====================
def _get_conn():
    """获取 SQLite 连接，自动设置行工厂为字典模式"""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        print(f"❌ 数据库连接失败：{str(e)}")
        return None


def _exec_with_commit(sql: str, desc: str = "", params=None):
    """
    执行 SQL 并自动提交，支持参数化查询防注入
    返回: (是否成功, 结果列表, 影响行数)
    """
    conn = _get_conn()
    if not conn:
        return (False, "数据库连接失败", 0)

    cursor = conn.cursor()
    try:
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        conn.commit()
        # 结果统一转字典，和原 DictCursor 行为完全一致
        result = [dict(row) for row in cursor.fetchall()] if cursor.description else None
        return (True, result, cursor.rowcount)
    except sqlite3.Error as e:
        conn.rollback()
        err_msg = f"{str(e)}（SQL：{sql[:80]}...）"
        print(f"❌ [{desc}]失败：{err_msg}")
        return (False, err_msg, 0)
    finally:
        cursor.close()
        conn.close()


def _init_tables():
    """初始化表结构，SQLite 语法适配"""
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
        """
        CREATE INDEX IF NOT EXISTS idx_user_time ON account_records (user_id, timestamp);
        """,
        """
        CREATE TABLE IF NOT EXISTS account_config (
            user_id TEXT NOT NULL,
            "key" TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (user_id, "key")
        );
        """
    ]
    for sql in tables_sql:
        success, _, _ = _exec_with_commit(sql, "创建表结构")
        if not success:
            return False
    return True


# ==================== 分页详情查询工具 ====================
def _get_detail_data(user_id: str, date_cond: str, date_params, page: int = 1, page_size: int = 20):
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    # 参数顺序: user_id + 日期参数 + offset + page_size
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

    success, data, _ = _exec_with_commit(data_sql, "查询详情数据", params)
    if not success:
        return (False, [], 0, 0)

    # 总条数查询
    count_sql = """
        SELECT COUNT(*) AS total FROM account_records
        WHERE user_id = ? AND {date_cond}
    """.format(date_cond=date_cond)
    count_params = (user_id,) + date_params
    success, count_result, _ = _exec_with_commit(count_sql, "查询详情总条数", count_params)
    total = count_result[0]["total"] if (success and count_result) else 0
    total_page = (total + page_size - 1) // page_size

    return (True, data, total, total_page)


# ==================== 删除记录 ====================
@handler(r"^删除记录\s*(\d+)$", ignore_at_check=True)
async def handle_delete(event, match):
    try:
        user_id = event.user_id
        record_id = match.group(1)

        if not _init_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        # 查询记录是否存在且属于当前用户
        query_sql = """
            SELECT type, amount FROM account_records
            WHERE id = ? AND user_id = ?
        """
        success, record, _ = _exec_with_commit(
            query_sql, "查询待删除记录", (record_id, user_id)
        )
        if not success:
            await event.reply("❌ 查询记录失败，请重试")
            return
        if not record:
            await event.reply(f"❌ 未找到ID为{record_id}的记录（仅能删除自己的记录）")
            return

        # 计算余额调整值
        record_type = record[0]["type"]
        amount = float(record[0]["amount"])
        balance_change = -amount if record_type == "收入" else amount

        # 调整余额
        update_sql = """
            UPDATE account_config
            SET value = value + ?
            WHERE user_id = ? AND "key" = 'balance'
        """
        success, _, rowcount = _exec_with_commit(
            update_sql, "删除时调整余额", (balance_change, user_id)
        )
        if not success or rowcount == 0:
            await event.reply("❌ 余额调整失败，删除未执行")
            return

        # 删除记录
        delete_sql = "DELETE FROM account_records WHERE id = ? AND user_id = ?"
        success, _, rowcount = _exec_with_commit(
            delete_sql, "删除记录", (record_id, user_id)
        )
        if not success or rowcount == 0:
            await event.reply("❌ 记录删除失败，请重试")
            return

        await event.reply(f"""\n✅ 记录ID {record_id} 删除成功！
{record_type}金额：{amount:.2f}元
余额已{"减少" if record_type == "收入" else "增加"} {abs(balance_change):.2f}元""")

    except Exception as e:
        err_msg = f"处理删除失败：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


# ==================== 收入记录 ====================
@handler(r"^收入\s*(\d+\.?\d*)\s*([^0-9\s]+)\s*([\s\S]*)$", ignore_at_check=True)
async def handle_income(event, match):
    try:
        user_id = event.user_id
        amount, category, remark = match.groups()

        if not _init_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        # 插入记录
        insert_sql = """
            INSERT INTO account_records 
            (user_id, type, amount, category, remark) 
            VALUES (?, '收入', ?, ?, ?)
        """
        success, _, rowcount = _exec_with_commit(
            insert_sql, "插入收入记录", (user_id, float(amount), category, remark)
        )
        if not success or rowcount == 0:
            await event.reply("❌ 收入记录失败，请重试")
            return

        # 获取自增ID
        get_id_sql = "SELECT last_insert_rowid() AS record_id"
        _, result, _ = _exec_with_commit(get_id_sql, "获取收入记录ID")
        record_id = result[0]["record_id"] if (result and len(result) > 0) else None
        if not record_id:
            # 二次查询兜底
            check_sql = """
                SELECT id FROM account_records
                WHERE user_id = ? AND type = '收入' AND amount = ?
                ORDER BY timestamp DESC LIMIT 1
            """
            _, check_result, _ = _exec_with_commit(
                check_sql, "二次校验收入ID", (user_id, float(amount))
            )
            record_id = check_result[0]["id"] if (check_result and len(check_result) > 0) else "未知"

        # 更新余额
        update_sql = """
            UPDATE account_config 
            SET value = value + ? 
            WHERE user_id = ? AND "key" = 'balance'
        """
        _exec_with_commit(update_sql, "更新收入后余额", (float(amount), user_id))

        await event.reply(f"""\n✅ 收入记录成功！
记录ID：{record_id}
金额：{float(amount):.2f} 元
分类：{category}
备注：{remark or '无'}""")

    except Exception as e:
        err_msg = f"处理错误：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


# ==================== 支出记录 ====================
@handler(r"^支出\s*(\d+\.?\d*)\s*([^0-9\s]+)\s*([\s\S]*)$", ignore_at_check=True)
async def handle_expense(event, match):
    try:
        user_id = event.user_id
        amount, category, remark = match.groups()

        if not _init_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        # 插入记录
        insert_sql = """
            INSERT INTO account_records 
            (user_id, type, amount, category, remark) 
            VALUES (?, '支出', ?, ?, ?)
        """
        success, _, rowcount = _exec_with_commit(
            insert_sql, "插入支出记录", (user_id, float(amount), category, remark)
        )
        if not success or rowcount == 0:
            await event.reply("❌ 支出记录失败，请重试")
            return

        # 获取自增ID
        get_id_sql = "SELECT last_insert_rowid() AS record_id"
        _, result, _ = _exec_with_commit(get_id_sql, "获取支出记录ID")
        record_id = result[0]["record_id"] if (result and len(result) > 0) else None
        if not record_id:
            check_sql = """
                SELECT id FROM account_records
                WHERE user_id = ? AND type = '支出' AND amount = ?
                ORDER BY timestamp DESC LIMIT 1
            """
            _, check_result, _ = _exec_with_commit(
                check_sql, "二次校验支出ID", (user_id, float(amount))
            )
            record_id = check_result[0]["id"] if (check_result and len(check_result) > 0) else "未知"

        # 更新余额
        update_sql = """
            UPDATE account_config 
            SET value = value - ? 
            WHERE user_id = ? AND "key" = 'balance'
        """
        _exec_with_commit(update_sql, "更新支出后余额", (float(amount), user_id))

        await event.reply(f"""\n✅ 支出记录成功！
记录ID：{record_id}
金额：{float(amount):.2f} 元
分类：{category}
备注：{remark or '无'}""")

    except Exception as e:
        err_msg = f"处理错误：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


# ==================== 当日详情 ====================
@handler(r"^当日详情(?:\s*(\d+))?$", ignore_at_check=True)
async def handle_day_detail(event, match):
    try:
        user_id = event.user_id
        page = int(match.group(1)) if (match.group(1) and match.group(1).isdigit()) else 1

        if not _init_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        date_cond = "date(timestamp) = ?"
        date_params = (today,)

        success, data, total, total_page = _get_detail_data(
            user_id, date_cond, date_params, page, page_size=100
        )
        if not success:
            await event.reply("❌ 查询当日详情失败，请稍后重试")
            return

        if total == 0:
            await event.reply(f"\n📅 {today} 暂无收支记录")
            return

        detail_text = [f"\n📝 {today} 当日详情（第{page}/{total_page}页，共{total}条）"]
        for record in data:
            remark = record["remark"] if record["remark"] else "无"
            detail_text.append(
                f"ID:{record['id']} | {record['type']} | {record['amount']:.2f}元 | {record['category']} | "
                f"时间：{record['time']} | 备注：{remark}"
            )
        await event.reply("\n\n".join(detail_text))

        if total_page > 1:
            await event.reply(f"💡 分页提示：发送「当日详情{page+1}」查看下一页（每页100条）")

    except Exception as e:
        err_msg = f"处理当日详情失败：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


# ==================== 本周详情 ====================
@handler(r"^本周详情(?:\s*(\d+))?$", ignore_at_check=True)
async def handle_week_detail(event, match):
    try:
        user_id = event.user_id
        page = int(match.group(1)) if (match.group(1) and match.group(1).isdigit()) else 1

        if not _init_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        monday_str = monday.strftime("%Y-%m-%d 00:00:00")
        sunday_str = sunday.strftime("%Y-%m-%d 23:59:59")
        date_cond = "timestamp BETWEEN ? AND ?"
        date_params = (monday_str, sunday_str)

        success, data, total, total_page = _get_detail_data(
            user_id, date_cond, date_params, page, page_size=20
        )
        if not success:
            await event.reply("❌ 查询本周详情失败")
            return

        if total == 0:
            await event.reply(f"\n📅 {monday_str.split(' ')[0]}-{sunday_str.split(' ')[0]} 本周暂无收支记录")
            return

        detail_text = [f"\n📝 {monday_str.split(' ')[0]}-{sunday_str.split(' ')[0]} 本周详情（第{page}/{total_page}页，共{total}条）"]
        for record in data:
            remark = record["remark"] if record["remark"] else "无"
            detail_text.append(
                f"ID:{record['id']} | {record['type']} | {record['amount']:.2f}元 | {record['category']} | "
                f"时间：{record['time']} | 备注：{remark}"
            )
        await event.reply("\n\n".join(detail_text))

        nav_tips = []
        if page > 1:
            nav_tips.append(f"发送「本周详情{page-1}」查看上一页")
        if page < total_page:
            nav_tips.append(f"发送「本周详情{page+1}」查看下一页")
        if nav_tips:
            await event.reply("💡 " + " | ".join(nav_tips))

    except Exception as e:
        err_msg = f"处理本周详情失败：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


# ==================== 本月详情 ====================
@handler(r"^本月详情(?:\s*(\d+))?$", ignore_at_check=True)
async def handle_month_detail(event, match):
    try:
        user_id = event.user_id
        page = int(match.group(1)) if (match.group(1) and match.group(1).isdigit()) else 1

        if not _init_tables():
            await event.reply("❌ 数据库初始化失败，请重试")
            return

        current_month = datetime.now().strftime("%Y-%m")
        date_cond = "strftime('%Y-%m', timestamp) = ?"
        date_params = (current_month,)

        success, data, total, total_page = _get_detail_data(
            user_id, date_cond, date_params, page, page_size=20
        )
        if not success:
            await event.reply("❌ 查询本月详情失败")
            return

        if total == 0:
            await event.reply(f"\n📅 {current_month} 本月暂无收支记录")
            return

        detail_text = [f"\n📝 {current_month} 本月详情（第{page}/{total_page}页，共{total}条）"]
        for record in data:
            remark = record["remark"] if record["remark"] else "无"
            detail_text.append(
                f"ID:{record['id']} | {record['type']} | {record['amount']:.2f}元 | {record['category']} | "
                f"时间：{record['time']} | 备注：{remark}"
            )
        await event.reply("\n\n".join(detail_text))

        nav_tips = []
        if page > 1:
            nav_tips.append(f"发送「本月详情{page-1}」查看上一页")
        if page < total_page:
            nav_tips.append(f"发送「本月详情{page+1}」查看下一页")
        if nav_tips:
            await event.reply("💡 " + " | ".join(nav_tips))

    except Exception as e:
        err_msg = f"处理本月详情失败：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


# ==================== 设置余额 ====================
@handler(r"^设置余额\s*(\d+\.?\d*)$", ignore_at_check=True)
async def handle_init_balance(event, match):
    try:
        user_id = event.user_id
        amount = match.group(1)

        if not _init_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        update_sql = """
            UPDATE account_config 
            SET value = ? 
            WHERE user_id = ? AND "key" = 'balance'
        """
        success, _, rowcount = _exec_with_commit(
            update_sql, "更新余额", (float(amount), user_id)
        )

        if not success:
            await event.reply("❌ 设置失败，请重试")
            return
        if rowcount == 0:
            init_sql = """
                INSERT INTO account_config (user_id, "key", value)
                VALUES (?, 'balance', ?)
            """
            _exec_with_commit(init_sql, "初始化余额", (user_id, float(amount)))
            await event.reply(f"⚠️ 已自动初始化并设置余额为 {float(amount):.2f} 元")
        else:
            check_sql = "SELECT value FROM account_config WHERE user_id = ? AND \"key\" = 'balance'"
            _, result, _ = _exec_with_commit(check_sql, "查询最新余额", (user_id,))
            final_balance = result[0]["value"] if result else 0
            await event.reply(f"✅ 初始余额已设置为：{final_balance:.2f} 元")

    except Exception as e:
        err_msg = f"处理错误：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


# ==================== 汇总查询 ====================
@handler(r"^查询\s*(当日|当周|当月)$", ignore_at_check=True)
async def handle_query(event, match):
    try:
        user_id = event.user_id
        scope = match.group(1)

        if not _init_tables():
            await event.reply("❌ 初始化失败，请检查数据库")
            return

        today = datetime.now()
        if scope == "当日":
            date_cond = "date(timestamp) = ?"
            time_param = today.strftime("%Y-%m-%d")
            time_desc = today.strftime("%Y年%m月%d日")
            params = (user_id, time_param)
        elif scope == "当周":
            monday = today - timedelta(days=today.weekday())
            sunday = monday + timedelta(days=6)
            date_cond = "timestamp BETWEEN ? AND ?"
            time_param = (monday.strftime("%Y-%m-%d 00:00:00"), sunday.strftime("%Y-%m-%d 23:59:59"))
            time_desc = f"{monday.strftime('%m月%d日')}-{sunday.strftime('%m月%d日')}"
            params = (user_id,) + time_param
        else:
            date_cond = "strftime('%Y-%m', timestamp) = ?"
            time_param = today.strftime("%Y-%m")
            time_desc = today.strftime("%Y年%m月")
            params = (user_id, time_param)

        # 收入查询
        income_sql = f"""
            SELECT ifnull(SUM(amount), 0) AS total, COUNT(*) AS count 
            FROM account_records 
            WHERE user_id = ? AND type = '收入' AND {date_cond}
        """
        _, income_res, _ = _exec_with_commit(income_sql, f"查询{scope}收入", params)

        # 支出查询
        expense_sql = f"""
            SELECT ifnull(SUM(amount), 0) AS total, COUNT(*) AS count 
            FROM account_records 
            WHERE user_id = ? AND type = '支出' AND {date_cond}
        """
        _, expense_res, _ = _exec_with_commit(expense_sql, f"查询{scope}支出", params)

        # 余额查询
        balance_sql = "SELECT value FROM account_config WHERE user_id = ? AND \"key\" = 'balance'"
        _, balance_res, _ = _exec_with_commit(balance_sql, "查询当前余额", (user_id,))

        total_income = income_res[0]["total"] if income_res else 0.0
        income_count = income_res[0]["count"] if income_res else 0
        total_expense = expense_res[0]["total"] if expense_res else 0.0
        expense_count = expense_res[0]["count"] if expense_res else 0
        current_balance = balance_res[0]["value"] if (balance_res and len(balance_res) > 0) else 0.0

        await event.reply(f"""\n📅 {time_desc} 汇总：
📥 总收入：{float(total_income):.2f} 元（{income_count}条记录）
📤 总支出：{float(total_expense):.2f} 元（{expense_count}条记录）
💰 当前余额：{float(current_balance):.2f} 元""")

    except Exception as e:
        err_msg = f"处理错误：{str(e)}"
        await event.reply(err_msg)
        print(traceback.format_exc())


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
    _init_tables()
    print("✅ 个人记账插件已加载，数据库初始化完成")