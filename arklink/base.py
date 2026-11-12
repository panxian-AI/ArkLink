"""ArkLink 适配器契约。

这里的类**不 import arkbot**：只有真正把事件交给框架时才解析 `arkbot.event`，
所以本包可以脱离 ArkBot 单独安装、单独测协议（探针/回放都靠这一点）。

契约与 `arkbot.adapters.base.Adapter` 保持一致（鸭子类型，不继承）：

    name            通道名
    is_connected    是否可发送
    wait_connected  等就绪
    on_event        注册回调（框架注入）
    run_forever     启动（内部自旋重连）
    stop            停止
    connect / disconnect
    call_api(action, params)   返回 data，失败抛 ApiError
    send_reply(event, chain, quote)  按来源通道回复

新增协议只要继承 `LinkAdapter` 并实现 connect/call_api，
再把「协议报文」翻译成 OneBot 形状的 dict 交给 `self._emit_dict()`，
框架侧 `Event.from_raw` 会自动还原成统一事件 —— 插件无感。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from arkbot.event import Event

logger = logging.getLogger("arklink")

EventHandler = Callable[["Event"], Any]


class LinkApiError(Exception):
    """arkbot 不可用时的兜底异常（正常路径会构造真正的 ApiError）。"""

    def __init__(self, action: str, retcode: int, message: str = ""):
        self.action = action
        self.retcode = retcode
        self.message = message
        super().__init__(f"API {action!r} 失败 retcode={retcode} {message}")


def api_error(action: str, retcode: int, message: str = "") -> Exception:
    """构造框架认得的 ApiError。

    Bot 依赖 `retcode in (1404, 429)` 做通道降级，所以异常类型必须是
    `arkbot.adapters.base.ApiError` —— 这里在运行时才解析，避免硬依赖。
    """
    try:
        from arkbot.adapters.base import ApiError  # type: ignore

        return ApiError(action, retcode, message)
    except Exception:
        return LinkApiError(action, retcode, message)


class LinkAdapter:
    """协议适配器基类（鸭子类型）。"""

    #: 通道名，出现在日志与 bot.adapter 路由里
    name: str = "link"
    #: 本通道一次能塞多少字，超了由框架/插件自行分段
    max_message_length: int = 3000

    def __init__(self) -> None:
        self._handler: EventHandler | None = None
        self._running = False
        self._connected = asyncio.Event()

    # ---------------- 事件 ----------------
    def on_event(self, handler: EventHandler) -> None:
        self._handler = handler

    async def _emit_dict(self, raw: dict[str, Any]) -> None:
        """把 OneBot 形状的 dict 交给框架。

        必须以独立任务分发：处理器里通常要 await 回复（回调 API），
        而 API 响应要靠接收循环解析，内联 await 会死锁。
        """
        from arkbot.event import Event  # 运行时绑定

        event = Event.from_raw(raw)
        event.adapter = self  # type: ignore[assignment]
        self._dispatch(event)

    async def _emit_event(self, event: "Event") -> None:
        """直接抛一个已构造好的事件（少见，兼容用）。"""
        event.adapter = self  # type: ignore[assignment]
        self._dispatch(event)

    def _dispatch(self, event: "Event") -> None:
        if self._handler is None:
            return
        task = asyncio.get_running_loop().create_task(self._handler(event))
        task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: "asyncio.Task[Any]") -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("事件处理任务异常", exc_info=task.exception())

    # ---------------- 健康度 ----------------
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> bool:
        if self._connected.is_set():
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False

    # ---------------- 回复 ----------------
    async def send_reply(self, event: "Event", chain: Any, quote: bool = True) -> bool:
        """按来源通道回复，默认走本通道的 send_msg。"""
        message_type = getattr(event, "message_type", "")
        if message_type == "group" and getattr(event, "group_id", 0):
            await self.call_api("send_group_msg", {
                "group_id": event.group_id,
                "message": _segments_of(chain, quote=quote, message_id=getattr(event, "message_id", 0)),
            })
            return True
        if message_type == "private" and getattr(event, "user_id", 0):
            await self.call_api("send_private_msg", {
                "user_id": event.user_id,
                "message": _segments_of(chain, quote=quote, message_id=getattr(event, "message_id", 0)),
            })
            return True
        return False

    # ---------------- 生命周期 ----------------
    async def connect(self) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def disconnect(self) -> None:  # pragma: no cover - 抽象
        self._connected.clear()

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def run_forever(self) -> None:
        self._running = True
        await self.connect()

    async def stop(self) -> None:
        self._running = False
        await self.disconnect()


def _segments_of(chain: Any, quote: bool, message_id: int) -> list[dict[str, Any]]:
    """把 MessageChain 转成 OneBot 段数组，必要时前置引用段。"""
    if hasattr(chain, "to_onebot"):
        segments = chain.to_onebot()
    elif isinstance(chain, list):
        segments = chain
    else:  # 字符串
        segments = [{"type": "text", "data": {"text": str(chain)}}]
    if quote and message_id:
        return [{"type": "reply", "data": {"id": str(message_id)}}, *segments]
    return list(segments)


def segments_plain(segments: list[dict[str, Any]]) -> str:
    """段数组里的纯文本（用于 raw_message / 日志）。"""
    out: list[str] = []
    for seg in segments:
        data = seg.get("data") or {}
        if seg.get("type") == "text":
            out.append(str(data.get("text") or ""))
        elif seg.get("type") == "at":
            qq = str(data.get("qq") or "")
            out.append("@全体成员" if qq == "all" else f"@{qq}")
    return "".join(out)
