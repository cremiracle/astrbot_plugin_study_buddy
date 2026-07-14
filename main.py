"""
Study Buddy - AstrBot Plugin

学习陪伴助手插件：提供番茄钟计时与学习统计功能，并附带 WebUI 仪表盘。
支持两种模式：
- 会话隔离模式：每个会话（用户）拥有独立的番茄钟和统计数据
- 共享模式：所有会话共享同一个番茄钟

核心功能：
1. 番茄钟模式：多轮学习-休息循环，默认每轮25分钟学习+5分钟休息
2. 正计时模式：持续计时，每小时提醒休息
3. 学习统计：记录今日、本周、总计学习时长，支持近7日趋势图
4. WebUI：提供可视化仪表盘，实时显示进行中的番茄钟和统计数据
"""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# AstrBot 框架核心 API 导入
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api.web import json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools

# ------------------------------
# 全局常量定义
# ------------------------------

# 数据库文件名
DB_FILE_NAME = "study_records.db"

# 插件唯一标识符，用于注册和数据存储路径
PLUGIN_NAME = "astrbot_plugin_study_buddy"

# 共享模式下的会话键名，用于标识全局共享的番茄钟
SHARED_SESSION_KEY = "__shared__"

# 数据库操作线程池，用于异步执行 SQLite 操作，避免阻塞事件循环
_executor = ThreadPoolExecutor(max_workers=1)


# ------------------------------
# 数据库操作函数（同步）
# ------------------------------

def _get_db_path() -> str:
    """
    获取 SQLite 数据库文件的绝对路径

    返回:
        str: 数据库文件的完整路径，位于插件数据目录下
    """
    # 通过 StarTools 获取插件专属数据目录，确保多插件隔离
    data_dir = StarTools.get_data_dir(PLUGIN_NAME)
    return str(data_dir / DB_FILE_NAME)


def _init_db() -> None:
    """
    初始化 SQLite 数据库表结构

    创建 study_sessions 表用于存储学习会话记录，并创建必要的索引以优化查询性能。
    表结构说明：
    - id: 自增主键
    - umo: 统一消息来源标识（用户/会话唯一标识）
    - session_id: 会话键（隔离模式下等于 umo，共享模式下为 SHARED_SESSION_KEY）
    - mode: 计时模式（stopwatch/pomodoro）
    - duration_minutes: 计划学习时长（分钟，番茄钟模式下为总学习时长）
    - start_time: 开始时间戳
    - end_time: 结束时间戳（未结束时为 NULL）
    - total_seconds: 实际学习时长（秒）
    - created_at: 记录创建时间戳
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
    # 为 umo 字段创建索引，加速按用户查询
    conn.execute("CREATE INDEX IF NOT EXISTS idx_study_sessions_umo ON study_sessions(umo)")
    # 为 start_time 字段创建索引，加速按时间范围查询
    conn.execute("CREATE INDEX IF NOT EXISTS idx_study_sessions_time ON study_sessions(start_time)")
    conn.commit()
    conn.close()


# ------------------------------
# 数据库操作函数（异步封装）
# ------------------------------

async def _insert_session_async(umo: str, session_id: str, mode: str, duration_minutes: int) -> int:
    """
    异步插入学习会话记录

    参数:
        umo: 统一消息来源标识
        session_id: 会话键
        mode: 计时模式（stopwatch/pomodoro）
        duration_minutes: 计划学习时长（分钟）

    返回:
        int: 新插入记录的 ID
    """
    loop = asyncio.get_event_loop()

    def _insert():
        """同步插入操作，将在线程池中执行"""
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

    # 在线程池中执行同步数据库操作，不阻塞事件循环
    return await loop.run_in_executor(_executor, _insert)


async def _finish_session_async(row_id: int) -> None:
    """
    异步结束学习会话，更新结束时间和实际学习时长

    参数:
        row_id: 会话记录的 ID
    """
    loop = asyncio.get_event_loop()

    def _finish():
        """同步更新操作，将在线程池中执行"""
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


async def _get_stats_async(umo: str | None = None) -> dict:
    """
    异步获取学习统计数据

    参数:
        umo: 统一消息来源标识，为 None 时查询所有用户的汇总数据

    返回:
        dict: 包含以下字段的统计字典：
            - total_seconds: 总学习时长（秒）
            - today_seconds: 今日学习时长（秒）
            - week_seconds: 近7日学习时长（秒）
            - daily: 近7日每日学习时长字典，key 为日期字符串 "YYYY-MM-DD"
    """
    loop = asyncio.get_event_loop()

    def _get():
        """同步查询操作，将在线程池中执行"""
        db_path = _get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 计算时间边界：今日 UTC 开始时间和7天前 UTC 时间
        now = datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        today_timestamp = today_start.timestamp()
        week_ago_timestamp = (today_start - timedelta(days=6)).timestamp()

        # 基础查询条件：仅统计已结束的会话
        base_where = "end_time IS NOT NULL"
        params: list = []

        # 如果指定了 umo，则按用户筛选
        if umo:
            base_where += " AND umo = ?"
            params.append(umo)

        # 查询总学习时长
        cursor.execute(f"SELECT COALESCE(SUM(total_seconds), 0) FROM study_sessions WHERE {base_where}", params)
        total_seconds = cursor.fetchone()[0] or 0

        # 查询今日学习时长
        cursor.execute(f"SELECT COALESCE(SUM(total_seconds), 0) FROM study_sessions WHERE {base_where} AND start_time >= ?", params + [today_timestamp])
        today_seconds = cursor.fetchone()[0] or 0

        # 查询近7日学习时长
        cursor.execute(f"SELECT COALESCE(SUM(total_seconds), 0) FROM study_sessions WHERE {base_where} AND start_time >= ?", params + [week_ago_timestamp])
        week_seconds = cursor.fetchone()[0] or 0

        # 查询近7日每日学习时长，按日期分组
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

        # 初始化近7日字典，确保每一天都有记录（默认为0）
        daily = {(today_start - timedelta(days=6 - i)).strftime("%Y-%m-%d"): 0 for i in range(7)}
        for row in daily_rows:
            if row[0] in daily:
                daily[row[0]] = row[1]

        return {"total_seconds": total_seconds, "today_seconds": today_seconds, "week_seconds": week_seconds, "daily": daily}

    return await loop.run_in_executor(_executor, _get)


async def _get_sessions_async() -> list:
    """
    异步获取所有有学习记录的会话（umo）列表

    返回:
        list: 包含所有不同 umo 的列表，按 umo 排序
    """
    loop = asyncio.get_event_loop()

    def _get():
        """同步查询操作，将在线程池中执行"""
        db_path = _get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT DISTINCT umo FROM study_sessions WHERE end_time IS NOT NULL ORDER BY umo"
        )
        rows = [row[0] for row in cursor.fetchall()]
        conn.close()
        return rows

    return await loop.run_in_executor(_executor, _get)


# ------------------------------
# 插件主类
# ------------------------------

@register(PLUGIN_NAME, "YourName", "学习陪伴助手 - 番茄钟与统计", "1.3.0")
class StudyBuddyPlugin(Star):
    """
    学习陪伴助手插件主类

    继承 AstrBot 的 Star 基类，实现番茄钟计时和学习统计功能。
    通过 @register 装饰器注册插件，框架会自动识别并加载。

    属性:
        config: 插件配置对象，包含 session_isolation 等配置项
        _timers: 当前运行中的计时器字典，key 为会话键，value 为计时器信息字典
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        """
        初始化插件实例

        参数:
            context: AstrBot 上下文对象，提供 API 注册、消息发送等能力
            config: 插件配置对象，从 _conf_schema.json 加载
        """
        super().__init__(context)
        self.config = config
        # 存储当前运行中的计时器，结构: {session_key: {row_id, umo, mode, start_time, task, ...}}
        self._timers: dict[str, dict] = {}
        # 初始化数据库表结构
        _init_db()

    @property
    def _session_isolation(self) -> bool:
        """
        获取会话隔离模式配置

        返回:
            bool: True 表示启用会话隔离（每个用户独立计时），False 表示共享模式
        """
        return self.config.get("session_isolation", True)

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """
        根据配置获取计时器查找键

        参数:
            event: 消息事件对象，包含 umo 等信息

        返回:
            str: 会话键，隔离模式下返回 umo，共享模式下返回 SHARED_SESSION_KEY
        """
        if self._session_isolation:
            return event.unified_msg_origin
        return SHARED_SESSION_KEY

    async def initialize(self):
        """
        插件初始化回调，注册 Web API 端点

        AstrBot 框架在插件加载后调用此方法，用于注册 HTTP API 和初始化资源。
        注册了三个 API 端点：
        - /{PLUGIN_NAME}/api/study_buddy/stats: 获取学习统计数据
        - /{PLUGIN_NAME}/api/study_buddy/active: 获取当前运行中的番茄钟
        - /{PLUGIN_NAME}/api/study_buddy/sessions: 获取有记录的会话列表
        """
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/api/study_buddy/stats",
            self._handle_stats_api,
            ["GET"],
            "获取学习统计数据",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/api/study_buddy/active",
            self._handle_active_api,
            ["GET"],
            "获取当前运行中的番茄钟",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/api/study_buddy/sessions",
            self._handle_sessions_api,
            ["GET"],
            "获取有记录的会话列表",
        )

    async def terminate(self):
        """
        插件终止回调，清理资源

        AstrBot 框架在插件卸载或服务关闭时调用此方法。
        负责：
        1. 取消所有运行中的计时器任务
        2. 保存未完成的会话记录
        3. 清空计时器字典
        """
        for session_key, timer_info in list(self._timers.items()):
            task = timer_info.get("task")
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if "row_id" in timer_info:
                await _finish_session_async(timer_info["row_id"])
        self._timers.clear()

    # ------------------------------
    # Web API 处理方法
    # ------------------------------

    async def _handle_stats_api(self, **kwargs):
        """
        处理学习统计数据的 Web API 请求

        查询参数:
            umo (可选): 指定用户的 umo，为空时查询所有用户汇总数据

        返回:
            JSON 响应，包含 total_seconds, today_seconds, week_seconds, daily 字段
        """
        umo = request.query.get("umo", "")
        stats = await _get_stats_async(umo or None)
        return json_response(stats)

    async def _handle_active_api(self, **kwargs):
        """
        处理当前运行中番茄钟的 Web API 请求

        返回:
            JSON 响应，包含：
                - active: 运行中的计时器列表
                - count: 计时器数量
                - session_isolation: 当前会话隔离模式
        """
        active = []
        for session_key, timer in self._timers.items():
            elapsed = int(time.time() - timer["start_time"])
            item = {
                "session_key": session_key,
                "umo": timer.get("umo", ""),
                "mode": timer["mode"],
                "start_time": timer["start_time"],
                "elapsed_seconds": elapsed,
            }
            # 番茄钟模式额外返回周期和阶段信息
            if timer["mode"] == "pomodoro":
                item["work_minutes"] = timer.get("work_minutes", 25)
                item["break_minutes"] = timer.get("break_minutes", 5)
                item["cycles"] = timer.get("cycles", 1)
                item["current_cycle"] = timer.get("current_cycle", 0)
                item["phase"] = timer.get("phase", "pending")
                phase_start = timer.get("phase_start", timer["start_time"])
                phase_elapsed = int(time.time() - phase_start)
                item["phase_elapsed_seconds"] = phase_elapsed
                # 计算当前阶段剩余时间
                if item["phase"] == "work":
                    item["phase_remain_seconds"] = max(0, timer["work_minutes"] * 60 - phase_elapsed)
                elif item["phase"] == "break":
                    item["phase_remain_seconds"] = max(0, timer["break_minutes"] * 60 - phase_elapsed)
                else:
                    item["phase_remain_seconds"] = 0
            active.append(item)
        return json_response({
            "active": active,
            "count": len(active),
            "session_isolation": self._session_isolation,
        })

    async def _handle_sessions_api(self, **kwargs):
        """
        处理会话列表的 Web API 请求

        返回:
            JSON 响应，包含：
                - sessions: 有学习记录的 umo 列表
                - session_isolation: 当前会话隔离模式
        """
        sessions = await _get_sessions_async()
        return json_response({"sessions": sessions, "session_isolation": self._session_isolation})

    # ------------------------------
    # 辅助方法
    # ------------------------------

    async def _send_message(self, session: str, text: str) -> None:
        """
        向指定会话发送消息

        参数:
            session: 会话标识（umo）
            text: 消息文本内容
        """
        try:
            # 构建消息链，仅包含纯文本组件
            chain = MessageChain([Plain(text)])
            await self.context.send_message(session, chain)
        except Exception as e:
            # 消息发送失败时记录警告日志，不抛出异常
            logger.warning(f"[StudyBuddy] Failed to send message: {e}")

    # ------------------------------
    # 计时器后台任务
    # ------------------------------

    async def _stopwatch_task(self, session_key: str, umo: str, row_id: int):
        """
        正计时模式后台任务

        持续运行，每分钟检查一次，每小时向用户发送休息提醒。
        当计时器被停止（session_key 从 _timers 中移除）时自动退出。

        参数:
            session_key: 会话键
            umo: 统一消息来源标识，用于发送消息
            row_id: 数据库记录 ID
        """
        try:
            last_remind_hour = 0
            while True:
                # 每分钟检查一次
                await asyncio.sleep(60)
                # 检查计时器是否已被停止
                if session_key not in self._timers:
                    break
                timer_info = self._timers[session_key]
                elapsed = int(time.time() - timer_info["start_time"])
                current_hour = elapsed // 3600
                # 每满一小时发送休息提醒
                if current_hour > last_remind_hour and current_hour >= 1:
                    last_remind_hour = current_hour
                    await self._send_message(umo, f"已学习 {current_hour} 小时，记得休息一下~")
        except asyncio.CancelledError:
            # 任务被取消时静默退出
            pass
        except Exception as e:
            logger.error(f"[StudyBuddy] Stopwatch task error: {e}")

    async def _pomodoro_task(
        self,
        session_key: str,
        umo: str,
        row_id: int,
        work_minutes: int,
        break_minutes: int,
        cycles: int,
    ):
        """
        番茄钟模式后台任务

        执行多轮学习-休息循环：
        1. 每轮开始时发送学习提醒
        2. 等待学习时长后发送完成提醒
        3. 如果不是最后一轮，发送休息提醒并等待休息时长
        4. 全部轮次完成后结束会话并发送恭喜消息

        参数:
            session_key: 会话键
            umo: 统一消息来源标识，用于发送消息
            row_id: 数据库记录 ID
            work_minutes: 每轮学习时长（分钟）
            break_minutes: 每轮休息时长（分钟）
            cycles: 循环次数
        """
        try:
            for cycle in range(1, cycles + 1):
                # 检查计时器是否已被停止
                if session_key not in self._timers:
                    break

                # 更新当前轮次和阶段状态为"学习中"
                self._timers[session_key]["current_cycle"] = cycle
                self._timers[session_key]["phase"] = "work"
                self._timers[session_key]["phase_start"] = time.time()

                # 发送学习开始提醒
                await self._send_message(umo, f"🔔 第 {cycle}/{cycles} 轮学习开始，加油！")

                # 等待学习时长
                await asyncio.sleep(work_minutes * 60)
                if session_key not in self._timers:
                    break

                # 发送本轮完成提醒
                await self._send_message(umo, f"🎉 第 {cycle}/{cycles} 轮完成！")

                # 如果不是最后一轮，进入休息阶段
                if cycle < cycles:
                    self._timers[session_key]["phase"] = "break"
                    self._timers[session_key]["phase_start"] = time.time()
                    await self._send_message(umo, f"☕ 休息 {break_minutes} 分钟~")
                    await asyncio.sleep(break_minutes * 60)
                    if session_key not in self._timers:
                        break

            # 全部轮次完成后，结束会话并发送恭喜消息
            if session_key in self._timers:
                await _finish_session_async(row_id)
                del self._timers[session_key]
                await self._send_message(umo, f"🏆 全部 {cycles} 轮完成！🎉")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[StudyBuddy] Pomodoro task error: {e}")

    # ------------------------------
    # LLM 工具方法（供 AI 调用）
    # ------------------------------

    @filter.llm_tool(name="create_pomodoro")
    async def create_pomodoro(
        self,
        event: AstrMessageEvent,
        mode: str,
        work_minutes: int = 25,
        break_minutes: int = 5,
        cycles: int = 1,
    ):
        """
        创建番茄钟或正计时器

        作为 LLM 工具暴露给 AI，AI 可以通过此方法创建计时器。

        参数:
            event: 消息事件对象
            mode: 计时模式，'stopwatch'（正计时）或 'pomodoro'（番茄钟）
            work_minutes: 每轮学习时长（分钟），默认 25
            break_minutes: 每轮休息时长（分钟），默认 5
            cycles: 循环次数，默认 1

        返回:
            str: 结果消息，供 AI 回复给用户
        """
        session_key = self._get_session_key(event)
        umo = event.unified_msg_origin

        # 检查当前是否已有进行中的计时器
        if session_key in self._timers:
            return "当前已有进行中的学习计时，请先停止后再创建新的。"

        # 标准化模式参数
        mode = mode.lower().strip()
        if mode not in ("stopwatch", "pomodoro"):
            return f"不支持的计时模式: {mode}。请使用 'stopwatch' 或 'pomodoro'。"

        if mode == "pomodoro":
            # 参数校验和默认值处理
            if work_minutes <= 0:
                work_minutes = 25
            if break_minutes < 0:
                break_minutes = 5
            if cycles <= 0:
                cycles = 1

            # 计算总学习时长
            total_work_minutes = work_minutes * cycles
            # 插入数据库记录
            row_id = await _insert_session_async(umo, session_key, mode, total_work_minutes)
            start_time = time.time()

            # 构建计时器信息字典
            timer_info = {
                "row_id": row_id,
                "umo": umo,
                "mode": mode,
                "work_minutes": work_minutes,
                "break_minutes": break_minutes,
                "cycles": cycles,
                "current_cycle": 0,
                "phase": "pending",
                "start_time": start_time,
                "phase_start": start_time,
            }

            # 创建后台任务
            task = asyncio.create_task(
                self._pomodoro_task(session_key, umo, row_id, work_minutes, break_minutes, cycles)
            )
            timer_info["task"] = task
            self._timers[session_key] = timer_info

            # 返回结果消息
            if cycles == 1:
                return f"已开启 {work_minutes} 分钟番茄钟！专注当下~"
            return f"已开启 {cycles} 轮番茄钟！每轮 {work_minutes} 分钟学习，{break_minutes} 分钟休息。"
        else:
            # 正计时模式
            row_id = await _insert_session_async(umo, session_key, mode, 0)
            start_time = time.time()

            timer_info = {
                "row_id": row_id,
                "umo": umo,
                "mode": mode,
                "duration_minutes": 0,
                "start_time": start_time,
            }

            task = asyncio.create_task(self._stopwatch_task(session_key, umo, row_id))
            timer_info["task"] = task
            self._timers[session_key] = timer_info
            return "已开启正计时模式！每小时提醒休息一次，结束记得告诉我~"

    @filter.llm_tool(name="stop_pomodoro")
    async def stop_pomodoro(self, event: AstrMessageEvent):
        """
        停止当前计时器并记录学习时长

        作为 LLM 工具暴露给 AI，AI 可以通过此方法停止计时器。

        返回:
            str: 结果消息，包含本次学习时长
        """
        session_key = self._get_session_key(event)

        # 检查是否有进行中的计时器
        if session_key not in self._timers:
            return "当前没有进行中的学习计时~"

        # 移除计时器并取消后台任务
        timer_info = self._timers.pop(session_key)
        task = timer_info.get("task")
        if task and not task.done():
            task.cancel()

        # 更新数据库记录，标记会话结束
        row_id = timer_info.get("row_id")
        if row_id:
            await _finish_session_async(row_id)

        # 计算实际学习时长
        elapsed = int(time.time() - timer_info["start_time"])
        minutes = elapsed // 60
        seconds = elapsed % 60

        # 生成结果消息
        mode_str = "正计时" if timer_info["mode"] == "stopwatch" else "番茄钟"
        return f"已停止{mode_str}！本次学习了 {minutes} 分 {seconds} 秒~"

    @filter.llm_tool(name="get_study_status")
    async def get_study_status(self, event: AstrMessageEvent):
        """
        获取当前计时器状态和学习统计

        作为 LLM 工具暴露给 AI，AI 可以通过此方法查询状态。

        返回:
            str: 状态描述文本，包含当前计时状态和统计数据
        """
        session_key = self._get_session_key(event)
        umo = event.unified_msg_origin

        result_parts = []

        # 查询当前计时器状态
        if session_key in self._timers:
            timer = self._timers[session_key]
            elapsed = int(time.time() - timer["start_time"])
            minutes = elapsed // 60
            seconds = elapsed % 60

            if timer["mode"] == "stopwatch":
                result_parts.append(f"当前正计时已运行 {minutes} 分 {seconds} 秒。")
            else:
                phase = timer.get("phase", "pending")
                current_cycle = timer.get("current_cycle", 0)
                cycles = timer.get("cycles", 1)
                work_minutes = timer.get("work_minutes", 25)
                break_minutes = timer.get("break_minutes", 5)

                if phase == "pending":
                    result_parts.append(f"番茄钟即将开始，共 {cycles} 轮。")
                elif phase == "work":
                    phase_elapsed = int(time.time() - timer.get("phase_start", timer["start_time"]))
                    phase_remain = max(0, work_minutes * 60 - phase_elapsed)
                    result_parts.append(f"第 {current_cycle}/{cycles} 轮学习中，还剩 {phase_remain // 60} 分。")
                elif phase == "break":
                    phase_elapsed = int(time.time() - timer.get("phase_start", timer["start_time"]))
                    phase_remain = max(0, break_minutes * 60 - phase_elapsed)
                    result_parts.append(f"第 {current_cycle}/{cycles} 轮休息中，还剩 {phase_remain // 60} 分。")
        else:
            result_parts.append("当前没有进行中的学习计时。")

        # 查询学习统计数据
        stats_umo = umo if self._session_isolation else None
        stats = await _get_stats_async(stats_umo)
        total_m = stats["total_seconds"] // 60
        today_m = stats["today_seconds"] // 60
        week_m = stats["week_seconds"] // 60

        result_parts.append(f"今日: {today_m}分钟; 本周: {week_m}分钟; 总计: {total_m}分钟。")

        return "\n".join(result_parts)

    # ------------------------------
    # 命令行指令处理方法
    # ------------------------------

    @filter.command("study")
    async def study_command(self, event: AstrMessageEvent, sub_command: str = ""):
        """
        手动控制命令处理

        通过 /study 命令手动控制计时器，支持以下子命令：
        - /study start stopwatch - 开始正计时
        - /study start pomodoro [学习时长] [休息时长] [循环次数] - 开始番茄钟
        - /study stop - 停止计时
        - /study stats - 查看统计

        参数:
            event: 消息事件对象
            sub_command: 子命令（由框架自动提取）
        """
        args = event.message_str.strip().split()
        # 参数校验：至少需要 /study [action] 两个参数
        if len(args) < 2:
            yield event.plain_result(
                "用法:\n"
                "/study start stopwatch - 开始正计时\n"
                "/study start pomodoro [学习时长] [休息时长] [循环次数]\n"
                "/study stop - 停止计时\n"
                "/study stats - 查看统计"
            )
            return

        action = args[1].lower()
        if action == "start":
            # 设置默认参数
            mode = "stopwatch"
            work_minutes = 25
            break_minutes = 5
            cycles = 1

            # 解析模式参数
            if len(args) >= 3:
                mode = args[2].lower()

            # 解析番茄钟参数
            if mode == "pomodoro":
                if len(args) >= 4:
                    try:
                        work_minutes = int(args[3])
                    except ValueError:
                        pass
                if len(args) >= 5:
                    try:
                        break_minutes = int(args[4])
                    except ValueError:
                        pass
                if len(args) >= 6:
                    try:
                        cycles = int(args[5])
                    except ValueError:
                        pass

                # 调用创建番茄钟方法
                result = await self.create_pomodoro(event, mode, work_minutes, break_minutes, cycles)
            else:
                # 调用创建正计时方法
                result = await self.create_pomodoro(event, mode, 0, 0, 0)

            yield event.plain_result(result)

        elif action == "stop":
            # 停止计时器
            result = await self.stop_pomodoro(event)
            yield event.plain_result(result)

        elif action == "stats":
            # 查看统计
            result = await self.get_study_status(event)
            yield event.plain_result(result)

        else:
            # 未知指令
            yield event.plain_result("未知指令。请使用 /study start | stop | stats")