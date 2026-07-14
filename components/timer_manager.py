"""
计时器管理器组件

版权所有 (C) 2026 cremiracle

本程序是自由软件：你可以根据自由软件基金会发布的 GNU Affero 通用公共许可证
的条款重新分发和/或修改它，可以是该许可证的第3版，或者（按你的选择）任何更高版本。

本程序的发布是希望它能有用，但不提供任何保证；甚至不包含对适销性或特定用途适用性的
暗示保证。有关更多详细信息，请参阅 GNU Affero 通用公共许可证。

你应该已经收到了 GNU Affero 通用公共许可证的副本，以及本程序。
如果没有，请参见 <https://www.gnu.org/licenses/>。

负责管理所有运行中的计时器，包括正计时和番茄钟两种模式。
提供计时器的创建、停止、状态查询等核心功能，并与数据库组件协同工作。

设计要点：
1. 使用字典存储当前运行中的计时器，key 为会话键，value 为计时器信息对象
2. 支持会话隔离模式和共享模式两种工作模式
3. 每个计时器运行独立的 asyncio 后台任务，避免相互阻塞
4. 提供完整的生命周期管理：创建 -> 运行 -> 停止/完成 -> 清理
5. 支持消息通知机制，在关键节点向用户发送提醒

计时器信息结构（timer_info）：
{
    "row_id": int,           # 数据库记录 ID
    "umo": str,              # 统一消息来源标识
    "mode": str,             # 计时模式（stopwatch/pomodoro）
    "start_time": float,     # 开始时间戳
    "task": asyncio.Task,    # 后台任务对象
    # 以下字段仅番茄钟模式存在：
    "work_minutes": int,     # 每轮学习时长（分钟）
    "break_minutes": int,    # 每轮休息时长（分钟）
    "cycles": int,           # 循环次数
    "current_cycle": int,    # 当前轮次（从1开始）
    "phase": str,            # 当前阶段（pending/work/break）
    "phase_start": float,    # 当前阶段开始时间戳
}
"""

import asyncio
import time
from typing import Dict, Optional, Any, Callable

# 数据库操作组件导入
from .database import insert_session, finish_session

# ------------------------------
# 全局常量定义
# ------------------------------

# 共享模式下的会话键名，用于标识全局共享的计时器
SHARED_SESSION_KEY = "__shared__"


class TimerManager:
    """
    计时器管理器类

    负责管理所有计时器的生命周期，包括创建、运行、停止和状态查询。
    支持两种计时模式：正计时（stopwatch）和番茄钟（pomodoro）。

    属性:
        _session_isolation: 是否启用会话隔离模式
        _timers: 当前运行中的计时器字典，key 为会话键
        _send_message: 消息发送回调函数，用于向用户发送提醒
    """

    def __init__(self, session_isolation: bool = True, send_message_callback: Optional[Callable] = None):
        """
        初始化计时器管理器

        参数:
            session_isolation: 是否启用会话隔离模式，默认 True（每个用户独立计时）
            send_message_callback: 消息发送回调函数，签名为 (umo: str, text: str) -> None
        """
        self._session_isolation = session_isolation
        self._timers: Dict[str, Dict[str, Any]] = {}
        self._send_message = send_message_callback or (lambda umo, text: None)

    @property
    def session_isolation(self) -> bool:
        """
        获取会话隔离模式配置

        返回:
            bool: True 表示启用会话隔离，False 表示共享模式
        """
        return self._session_isolation

    def get_session_key(self, umo: str) -> str:
        """
        根据配置获取计时器查找键

        参数:
            umo: 统一消息来源标识

        返回:
            str: 会话键，隔离模式下返回 umo，共享模式下返回 SHARED_SESSION_KEY
        """
        if self._session_isolation:
            return umo
        return SHARED_SESSION_KEY

    def has_active_timer(self, session_key: str) -> bool:
        """
        检查指定会话是否有进行中的计时器

        参数:
            session_key: 会话键

        返回:
            bool: True 表示有进行中的计时器，False 表示没有
        """
        return session_key in self._timers

    def get_timer(self, session_key: str) -> Optional[Dict[str, Any]]:
        """
        获取指定会话的计时器信息

        参数:
            session_key: 会话键

        返回:
            Optional[Dict]: 计时器信息字典，不存在时返回 None
        """
        return self._timers.get(session_key)

    def get_all_active_timers(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有运行中的计时器

        返回:
            Dict: 所有计时器信息字典，key 为会话键
        """
        return dict(self._timers)

    async def create_stopwatch(self, umo: str) -> str:
        """
        创建正计时器

        正计时模式：持续计时，每小时向用户发送休息提醒。

        参数:
            umo: 统一消息来源标识

        返回:
            str: 结果消息，供调用方回复给用户
        """
        session_key = self.get_session_key(umo)

        # 检查是否已有进行中的计时器
        if session_key in self._timers:
            return "当前已有进行中的学习计时，请先停止后再创建新的。"

        # 在数据库中创建会话记录
        row_id = await insert_session(umo, session_key, "stopwatch", 0)
        start_time = time.time()

        # 构建计时器信息
        timer_info = {
            "row_id": row_id,
            "umo": umo,
            "mode": "stopwatch",
            "start_time": start_time,
        }

        # 创建后台计时任务
        task = asyncio.create_task(self._stopwatch_task(session_key, umo, row_id))
        timer_info["task"] = task

        # 注册到计时器字典
        self._timers[session_key] = timer_info

        return "已开启正计时模式！每小时提醒休息一次，结束记得告诉我~"

    async def create_pomodoro(
        self,
        umo: str,
        work_minutes: int = 25,
        break_minutes: int = 5,
        cycles: int = 1,
    ) -> str:
        """
        创建番茄钟

        番茄钟模式：多轮学习-休息循环，默认每轮25分钟学习+5分钟休息。

        参数:
            umo: 统一消息来源标识
            work_minutes: 每轮学习时长（分钟），默认 25
            break_minutes: 每轮休息时长（分钟），默认 5
            cycles: 循环次数，默认 1

        返回:
            str: 结果消息，供调用方回复给用户
        """
        session_key = self.get_session_key(umo)

        # 检查是否已有进行中的计时器
        if session_key in self._timers:
            return "当前已有进行中的学习计时，请先停止后再创建新的。"

        # 参数校验和默认值处理
        if work_minutes <= 0:
            work_minutes = 25
        if break_minutes < 0:
            break_minutes = 5
        if cycles <= 0:
            cycles = 1

        # 计算总学习时长
        total_work_minutes = work_minutes * cycles

        # 在数据库中创建会话记录
        row_id = await insert_session(umo, session_key, "pomodoro", total_work_minutes)
        start_time = time.time()

        # 构建计时器信息
        timer_info = {
            "row_id": row_id,
            "umo": umo,
            "mode": "pomodoro",
            "work_minutes": work_minutes,
            "break_minutes": break_minutes,
            "cycles": cycles,
            "current_cycle": 0,
            "phase": "pending",
            "start_time": start_time,
            "phase_start": start_time,
        }

        # 创建后台计时任务
        task = asyncio.create_task(
            self._pomodoro_task(session_key, umo, row_id, work_minutes, break_minutes, cycles)
        )
        timer_info["task"] = task

        # 注册到计时器字典
        self._timers[session_key] = timer_info

        # 生成结果消息
        if cycles == 1:
            return f"已开启 {work_minutes} 分钟番茄钟！专注当下~"
        return f"已开启 {cycles} 轮番茄钟！每轮 {work_minutes} 分钟学习，{break_minutes} 分钟休息。"

    async def stop_timer(self, session_key: str) -> str:
        """
        停止指定会话的计时器

        参数:
            session_key: 会话键

        返回:
            str: 结果消息，包含本次学习时长
        """
        # 检查是否有进行中的计时器
        if session_key not in self._timers:
            return "当前没有进行中的学习计时~"

        # 从字典中移除计时器
        timer_info = self._timers.pop(session_key)

        # 取消后台任务（如果存在且未完成）
        task = timer_info.get("task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # 更新数据库记录，标记会话结束
        row_id = timer_info.get("row_id")
        if row_id:
            await finish_session(row_id)

        # 计算实际学习时长
        elapsed = int(time.time() - timer_info["start_time"])
        minutes = elapsed // 60
        seconds = elapsed % 60

        # 生成结果消息
        mode_str = "正计时" if timer_info["mode"] == "stopwatch" else "番茄钟"
        return f"已停止{mode_str}！本次学习了 {minutes} 分 {seconds} 秒~"

    async def cleanup_all(self) -> None:
        """
        清理所有计时器

        取消所有运行中的后台任务，并保存未完成的会话记录。
        通常在插件终止时调用。
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
                await finish_session(timer_info["row_id"])
        self._timers.clear()

    # ------------------------------
    # 后台任务实现
    # ------------------------------

    async def _stopwatch_task(self, session_key: str, umo: str, row_id: int) -> None:
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

                # 计算已学习时长
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
            from astrbot.api import logger
            logger.error(f"[TimerManager] Stopwatch task error for session {session_key}: {e}")

    async def _pomodoro_task(
        self,
        session_key: str,
        umo: str,
        row_id: int,
        work_minutes: int,
        break_minutes: int,
        cycles: int,
    ) -> None:
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
                await finish_session(row_id)
                del self._timers[session_key]
                await self._send_message(umo, f"🏆 全部 {cycles} 轮完成！🎉")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            from astrbot.api import logger
            logger.error(f"[TimerManager] Pomodoro task error for session {session_key}: {e}")