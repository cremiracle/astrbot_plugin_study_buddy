"""
Study Buddy - AstrBot Plugin

学习陪伴助手插件：提供番茄钟计时与学习统计功能，并附带 WebUI 仪表盘。

插件架构：
├── main.py              # 插件入口，负责注册、API 路由、LLM 工具和指令处理
└── components/
    ├── database.py      # 数据库操作组件（SQLite 持久化）
    └── timer_manager.py # 计时器管理器组件（正计时、番茄钟核心逻辑）

支持两种模式：
- 会话隔离模式：每个会话（用户）拥有独立的番茄钟和统计数据
- 共享模式：所有会话共享同一个番茄钟

核心功能：
1. 番茄钟模式：多轮学习-休息循环，默认每轮25分钟学习+5分钟休息
2. 正计时模式：持续计时，每小时提醒休息
3. 学习统计：记录今日、本周、总计学习时长，支持近7日趋势图
4. WebUI：提供可视化仪表盘，实时显示进行中的番茄钟和统计数据
"""

import os
import sys
import time

# 将插件目录加入 sys.path，确保 AstrBot 加载插件时能正确导入 components 包
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# AstrBot 框架核心 API 导入
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api.web import json_response, request
from astrbot.core.message.message_event_result import MessageChain

# 自定义组件导入
from components.database import init_db, get_stats, get_sessions
from components.timer_manager import TimerManager

# ------------------------------
# 全局常量定义
# ------------------------------

# 插件唯一标识符，用于注册和数据存储路径
PLUGIN_NAME = "astrbot_plugin_study_buddy"


# ------------------------------
# 插件主类
# ------------------------------

@register(PLUGIN_NAME, "YourName", "学习陪伴助手 - 番茄钟与统计", "1.3.0")
class StudyBuddyPlugin(Star):
    """
    学习陪伴助手插件主类

    继承 AstrBot 的 Star 基类，作为插件的统一入口。
    通过 @register 装饰器注册插件，框架会自动识别并加载。

    职责：
    1. 插件初始化与资源管理
    2. Web API 路由注册
    3. LLM 工具暴露（供 AI 调用）
    4. 命令行指令处理
    5. 消息发送代理

    属性:
        config: 插件配置对象，包含 session_isolation 等配置项
        _timer_manager: 计时器管理器实例，封装所有计时逻辑
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

        # 初始化数据库表结构
        init_db()

        # 创建计时器管理器实例，传入消息发送回调
        session_isolation = self.config.get("session_isolation", True)
        self._timer_manager = TimerManager(
            session_isolation=session_isolation,
            send_message_callback=self._send_message,
        )

    @property
    def _session_isolation(self) -> bool:
        """
        获取会话隔离模式配置

        返回:
            bool: True 表示启用会话隔离（每个用户独立计时），False 表示共享模式
        """
        return self._timer_manager.session_isolation

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """
        根据配置获取计时器查找键

        参数:
            event: 消息事件对象，包含 umo 等信息

        返回:
            str: 会话键，隔离模式下返回 umo，共享模式下返回固定共享键
        """
        return self._timer_manager.get_session_key(event.unified_msg_origin)

    async def initialize(self):
        """
        插件初始化回调，注册 Web API 端点

        AstrBot 框架在插件加载后调用此方法，用于注册 HTTP API 和初始化资源。

        注册的 API 端点：
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
        1. 清理所有运行中的计时器（取消任务、保存记录）
        """
        await self._timer_manager.cleanup_all()

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
        stats = await get_stats(umo or None)
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
        for session_key, timer in self._timer_manager.get_all_active_timers().items():
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
        sessions = await get_sessions()
        return json_response({"sessions": sessions, "session_isolation": self._session_isolation})

    # ------------------------------
    # 辅助方法
    # ------------------------------

    async def _send_message(self, umo: str, text: str) -> None:
        """
        向指定会话发送消息

        参数:
            umo: 统一消息来源标识（会话标识）
            text: 消息文本内容
        """
        try:
            # 构建消息链，仅包含纯文本组件
            chain = MessageChain([Plain(text)])
            await self.context.send_message(umo, chain)
        except Exception as e:
            # 消息发送失败时记录警告日志，不抛出异常
            logger.warning(f"[StudyBuddy] Failed to send message: {e}")

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
        mode = mode.lower().strip()
        if mode not in ("stopwatch", "pomodoro"):
            return f"不支持的计时模式: {mode}。请使用 'stopwatch' 或 'pomodoro'。"

        umo = event.unified_msg_origin

        if mode == "pomodoro":
            return await self._timer_manager.create_pomodoro(umo, work_minutes, break_minutes, cycles)
        else:
            return await self._timer_manager.create_stopwatch(umo)

    @filter.llm_tool(name="stop_pomodoro")
    async def stop_pomodoro(self, event: AstrMessageEvent):
        """
        停止当前计时器并记录学习时长

        作为 LLM 工具暴露给 AI，AI 可以通过此方法停止计时器。

        返回:
            str: 结果消息，包含本次学习时长
        """
        session_key = self._get_session_key(event)
        return await self._timer_manager.stop_timer(session_key)

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
        timer = self._timer_manager.get_timer(session_key)
        if timer:
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
        stats = await get_stats(stats_umo)
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