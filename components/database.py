"""
数据库操作组件

版权所有 (C) 2026 cremiracle

本程序是自由软件：你可以根据自由软件基金会发布的 GNU Affero 通用公共许可证
的条款重新分发和/或修改它，可以是该许可证的第3版，或者（按你的选择）任何更高版本。

本程序的发布是希望它能有用，但不提供任何保证；甚至不包含对适销性或特定用途适用性的
暗示保证。有关更多详细信息，请参阅 GNU Affero 通用公共许可证。

你应该已经收到了 GNU Affero 通用公共许可证的副本，以及本程序。
如果没有，请参见 <https://www.gnu.org/licenses/>。

负责学习会话记录的持久化存储，基于 SQLite 实现。
提供完整的 CRUD 操作，包括会话插入、结束、统计查询等。
所有数据库操作均通过线程池异步执行，避免阻塞事件循环。

数据模型说明：
- study_sessions 表：存储每次学习会话
  - id: 自增主键
  - umo: 统一消息来源标识（用户/会话唯一标识）
  - session_id: 会话键（隔离模式下等于 umo，共享模式下为固定值）
  - mode: 计时模式（stopwatch/pomodoro）
  - duration_minutes: 计划学习时长（分钟）
  - start_time: 开始时间戳
  - end_time: 结束时间戳（未结束时为 NULL）
  - total_seconds: 实际学习时长（秒）
  - created_at: 记录创建时间戳
"""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List

# AstrBot 框架工具导入
from astrbot.core.star.star_tools import StarTools

# ------------------------------
# 全局常量定义
# ------------------------------

# 数据库文件名
DB_FILE_NAME = "study_records.db"

# 插件唯一标识符，用于数据存储路径
PLUGIN_NAME = "astrbot_plugin_study_buddy"

# 数据库操作线程池，限制为单线程以避免 SQLite 并发问题
_executor = ThreadPoolExecutor(max_workers=1)


def _get_db_path() -> str:
    """
    获取 SQLite 数据库文件的绝对路径

    返回:
        str: 数据库文件的完整路径，位于插件专属数据目录下
    """
    data_dir = StarTools.get_data_dir(PLUGIN_NAME)
    return str(data_dir / DB_FILE_NAME)


def init_db() -> None:
    """
    初始化 SQLite 数据库表结构

    创建 study_sessions 表及必要索引。
    若表已存在则跳过创建（CREATE TABLE IF NOT EXISTS）。
    """
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            umo TEXT NOT NULL,
            session_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            duration_minutes INTEGER DEFAULT 0,
            start_time REAL NOT NULL,
            end_time REAL,
            total_seconds INTEGER DEFAULT 0,
            created_at REAL DEFAULT (strftime('%s','now'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_study_sessions_umo ON study_sessions(umo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_study_sessions_time ON study_sessions(start_time)")
    conn.commit()
    conn.close()


async def insert_session(umo: str, session_id: str, mode: str, duration_minutes: int) -> int:
    """
    异步插入学习会话记录

    参数:
        umo: 统一消息来源标识
        session_id: 会话键（隔离模式下等于 umo，共享模式下为固定值）
        mode: 计时模式（stopwatch/pomodoro）
        duration_minutes: 计划学习时长（分钟）

    返回:
        int: 新插入记录的自增 ID
    """
    loop = asyncio.get_event_loop()

    def _insert() -> int:
        """同步插入操作，在线程池中执行"""
        db_path = _get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "INSERT INTO study_sessions (umo, session_id, mode, duration_minutes, start_time) VALUES (?, ?, ?, ?, ?)",
            (umo, session_id, mode, duration_minutes, time.time()),
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    return await loop.run_in_executor(_executor, _insert)


async def finish_session(row_id: int) -> None:
    """
    异步结束学习会话，更新结束时间和实际学习时长

    参数:
        row_id: 会话记录的 ID
    """
    loop = asyncio.get_event_loop()

    def _finish() -> None:
        """同步更新操作，在线程池中执行"""
        db_path = _get_db_path()
        conn = sqlite3.connect(db_path)
        now = time.time()
        conn.execute(
            "UPDATE study_sessions SET end_time = ?, total_seconds = CAST(? - start_time AS INTEGER) WHERE id = ?",
            (now, now, row_id),
        )
        conn.commit()
        conn.close()

    await loop.run_in_executor(_executor, _finish)


async def get_stats(umo: Optional[str] = None) -> Dict:
    """
    异步获取学习统计数据

    参数:
        umo: 统一消息来源标识，为 None 时查询所有用户的汇总数据

    返回:
        Dict: 统计字典，包含以下字段：
            - total_seconds: 总学习时长（秒）
            - today_seconds: 今日学习时长（秒）
            - week_seconds: 近7日学习时长（秒）
            - daily: 近7日每日学习时长字典，key 为日期字符串 "YYYY-MM-DD"
    """
    loop = asyncio.get_event_loop()

    def _get() -> Dict:
        """同步查询操作，在线程池中执行"""
        db_path = _get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        now = datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        today_timestamp = today_start.timestamp()
        week_ago_timestamp = (today_start - timedelta(days=6)).timestamp()

        base_where = "end_time IS NOT NULL"
        params: List = []

        if umo:
            base_where += " AND umo = ?"
            params.append(umo)

        cursor.execute(f"SELECT COALESCE(SUM(total_seconds), 0) FROM study_sessions WHERE {base_where}", params)
        total_seconds = cursor.fetchone()[0] or 0

        cursor.execute(f"SELECT COALESCE(SUM(total_seconds), 0) FROM study_sessions WHERE {base_where} AND start_time >= ?", params + [today_timestamp])
        today_seconds = cursor.fetchone()[0] or 0

        cursor.execute(f"SELECT COALESCE(SUM(total_seconds), 0) FROM study_sessions WHERE {base_where} AND start_time >= ?", params + [week_ago_timestamp])
        week_seconds = cursor.fetchone()[0] or 0

        cursor.execute(
            f"""
            SELECT date(datetime(start_time, 'unixepoch', 'localtime')) as d, SUM(total_seconds)
            FROM study_sessions WHERE {base_where} AND start_time >= ?
            GROUP BY d ORDER BY d
            """,
            params + [week_ago_timestamp],
        )
        daily_rows = cursor.fetchall()
        conn.close()

        daily = {(today_start - timedelta(days=6 - i)).strftime("%Y-%m-%d"): 0 for i in range(7)}
        for row in daily_rows:
            if row[0] in daily:
                daily[row[0]] = row[1]

        return {
            "total_seconds": total_seconds,
            "today_seconds": today_seconds,
            "week_seconds": week_seconds,
            "daily": daily,
        }

    return await loop.run_in_executor(_executor, _get)


async def get_sessions() -> List[str]:
    """
    异步获取所有有学习记录的会话（umo）列表

    返回:
        List[str]: 包含所有不同 umo 的列表，按 umo 排序
    """
    loop = asyncio.get_event_loop()

    def _get() -> List[str]:
        """同步查询操作，在线程池中执行"""
        db_path = _get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT DISTINCT umo FROM study_sessions WHERE end_time IS NOT NULL ORDER BY umo"
        )
        rows = [row[0] for row in cursor.fetchall()]
        conn.close()
        return rows

    return await loop.run_in_executor(_executor, _get)