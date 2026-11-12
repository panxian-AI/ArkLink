"""发消息的编排层：策略 → 解析 → 引擎。

分三层，是因为它们的失败模式完全不同：

```
SendService.send(target, message)
  ├─ SendPolicy   账号/收件人/群/长度        失败 = 明确的 1400/1403（不该发）
  ├─ elements     消息段解析                  失败 = 消息本身不合法
  └─ SendEngine   真正送进 QQ
        ├─ DryRunEngine   只记录不发送（默认）
        └─ PipeEngine     op=REQUEST, cmd="arklink.send" → hook DLL
```

**引擎是可替换的**，这是这一层的全部价值：hook DLL 今天是「抓模板 + 改文本」，
明天换成「直调 wrapper」，上层（OneBot / CLI / 框架适配）一行都不用改。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import elements as el
from .sendpolicy import Decision, Peer, SendPolicy

logger = logging.getLogger("arklink.ntqq.send")

CHAT_C2C = 1
CHAT_GROUP = 2

#: 管道请求的 cmd；DLL 侧按这个字符串分发
PIPE_CMD_SEND = "arklink.send"


@dataclass
class SendRequest:
    chat_type: int
    peer_uid: str
    elements: list[el.Element]
    uin: str = ""
    echo: str = ""
    timeout: float = 15.0
    #: 人能看懂的名字（备注优先）。UI 通道拿它去搜索框里打字，
    #: 因为 QQ 的搜索认备注/昵称，不认 `u_xxx` 这种内部 uid。
    peer_name: str = ""

    @property
    def search_key(self) -> str:
        """UI 通道往搜索框里敲的字符串：备注 → 昵称 → QQ 号 → uid。"""
        return self.peer_name or self.peer_uid

    @property
    def text(self) -> str:
        return el.plain_text(self.elements)

    def body(self) -> bytes:
        payload = {
            "uin": self.uin,
            "chat_type": self.chat_type,
            "peer_uid": self.peer_uid,
            "echo": self.echo,
            "elements": [e.as_dict() for e in self.elements],
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@dataclass
class SendResult:
    ok: bool
    message_id: int = 0
    code: int = 0
    msg: str = ""
    engine: str = ""
    dry_run: bool = False
    text: str = ""
    echo: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "code": self.code,
            "msg": self.msg,
            "engine": self.engine,
            "dry_run": self.dry_run,
            "text": self.text,
            "echo": self.echo,
            **self.extra,
        }


class SendEngine(Protocol):
    """引擎接口：只有一个方法，方便替换和打桩。"""

    name: str

    @property
    def available(self) -> bool:  # pragma: no cover - 协议声明
        ...

    async def send(self, request: SendRequest) -> SendResult:  # pragma: no cover
        ...


class DryRunEngine:
    """只记录不发送。默认引擎，也是「策略试跑」的开关。"""

    name = "dry-run"

    def __init__(self) -> None:
        self.sent: list[SendRequest] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, request: SendRequest) -> SendResult:
        self.sent.append(request)
        logger.info(
            "[dry-run] → %s(%s) %r len=%d",
            request.peer_uid,
            request.chat_type,
            request.text[:40],
            len(request.text),
        )
        return SendResult(
            ok=True,
            message_id=0,
            msg="dry-run：没有真的发送",
            engine=self.name,
            dry_run=True,
            text=request.text,
            echo=request.echo,
        )


class PipeEngine:
    """把请求交给注入的 DLL：`cmd=arklink.send`，body 是 JSON。

    约定（DLL 侧必须一致）：

    * 请求：`{"uin","chat_type","peer_uid","elements":[...],"echo"}`
    * 回包：`status=0` + `{"message_id":123,"client_seq":456}`；
      `status!=0` 时 `msg` 是给人看的原因。
    """

    name = "pipe"

    def __init__(self, client: Any) -> None:
        self.client = client

    @property
    def available(self) -> bool:
        client = self.client
        return bool(client is not None and getattr(client, "logged_in", False))

    async def send(self, request: SendRequest) -> SendResult:
        if not self.available:
            return SendResult(
                ok=False,
                code=1404,
                msg="hook 通道未就绪（没连上管道，或还没登录）",
                engine=self.name,
                text=request.text,
                echo=request.echo,
            )
        try:
            frame = await self.client.request(PIPE_CMD_SEND, request.body(), timeout=request.timeout)
        except Exception as exc:  # 管道断了 / 超时
            return SendResult(
                ok=False,
                code=1404,
                msg=f"送进 QQ 失败：{exc}",
                engine=self.name,
                text=request.text,
                echo=request.echo,
            )
        data: dict[str, Any] = {}
        if frame.body:
            try:
                data = json.loads(frame.body.decode("utf-8", "replace") or "{}")
            except ValueError:
                data = {}
        if frame.status != 0:
            return SendResult(
                ok=False,
                code=frame.status if frame.status > 0 else 1404,
                msg=frame.msg or data.get("msg") or "DLL 拒绝了这次发送",
                engine=self.name,
                text=request.text,
                echo=request.echo,
                extra=data,
            )
        return SendResult(
            ok=True,
            message_id=int(data.get("message_id") or 0),
            msg=frame.msg or "ok",
            engine=self.name,
            text=request.text,
            echo=request.echo,
            extra=data,
        )


class HostEngine:
    """操作官方客户端窗口发送（`arklink.ntqq.host`）。

    为什么它是 1.0 之前的默认引擎：QQ 9.9.31 的 `KernelMsgService` 在进程里
    没有可直调的实例（详见 `qqabi.py` 的说明），而**客户端自己**当然能把消息
    发出去。于是这一版走「把文本交给客户端」——不是拼包、不是伪造协议，
    消息由官方客户端产生，风控看到的就是用户自己在打字。

    代价要说清楚：只支持纯文本、会抢焦点、依赖界面语言与快捷键。
    所以它是 `SendEngine`，不是唯一实现：`PipeEngine`（进程内直调）接好后
    换引擎即可，上层零改动。
    """

    name = "host"

    def __init__(
        self,
        host: Any | None = None,
        *,
        dry_run: bool = False,
        uin: str = "",
        candidates: tuple[str, ...] = (),
        min_gap: float = 0.35,
    ) -> None:
        if host is None:
            from .host import ClientHost  # 延迟导入：POSIX 上没有 win32api

            host = ClientHost(dry_run=dry_run, uin=uin, candidates=candidates)
        self.host = host
        #: 两条消息之间的最小间隔：客户端对连发有节流，抢太快会丢
        self.min_gap = float(min_gap)
        self._lock = threading.Lock()      # 一个窗口，天然只能一个一个来
        self._last = 0.0
        self._depth = 0                    # 排队中的请求数（含正在发的）
        self._depth_lock = threading.Lock()

    @property
    def depth(self) -> int:
        with self._depth_lock:
            return self._depth

    @property
    def available(self) -> bool:
        try:
            info = self.host.detect()
        except Exception:  # 探测失败一律当不可用，不要影响上层判断
            return False
        return bool(getattr(info, "running", False))

    async def send(self, request: SendRequest) -> SendResult:
        """并发进来的请求在这里排队：同一个窗口同时只能有一个操作。"""
        with self._depth_lock:
            self._depth += 1
        try:
            return await asyncio.to_thread(self._send_blocking, request)
        finally:
            with self._depth_lock:
                self._depth -= 1

    def _send_blocking(self, request: SendRequest) -> SendResult:
        with self._lock:
            gap = self.min_gap - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            result = self._send_locked(request)
            self._last = time.time()
            return result

    def _send_locked(self, request: SendRequest) -> SendResult:
        info = self.host.detect()
        if not getattr(info, "running", False):
            return SendResult(
                ok=False,
                code=1404,
                msg="官方客户端不在线，UI 通道发不出去",
                engine=self.name,
                text=request.text,
                echo=request.echo,
            )
        # 同机多开时窗口标题都是 "QQ"：认出来的号对不上就宁可不发，
        # 免得用另一个号把消息发给同一个人。
        want = getattr(self.host, "uin", "")
        got = getattr(info, "uin", "")
        if want and got and want != got:
            return SendResult(
                ok=False,
                code=1403,
                msg=f"当前窗口属于 {got}，不是允许发送的 {want}（多开时请确认账号）",
                engine=self.name,
                text=request.text,
                echo=request.echo,
            )
        if want and not got:
            return SendResult(
                ok=False,
                code=1403,
                msg=f"认不出这个窗口是哪个号，无法确认是不是 {want}（拒绝盲发）",
                engine=self.name,
                text=request.text,
                echo=request.echo,
            )
        text = request.text
        if not text:
            return SendResult(ok=False, code=1400, msg="空消息", engine=self.name, echo=request.echo)
        ok = self.host.open_and_send(request.search_key, text)
        action = getattr(self.host, "last_action", "")
        if not ok:
            return SendResult(
                ok=False,
                code=-1,
                msg=f"操作客户端失败：{action}",
                engine=self.name,
                text=text,
                echo=request.echo,
            )
        return SendResult(
            ok=True,
            message_id=0,
            msg="已交给官方客户端发送",
            engine=self.name,
            text=text,
            echo=request.echo,
            extra={"via": "client-host", "peer": request.search_key, "action": action},
        )


def engine_from_config(
    kind: str = "auto",
    *,
    client: Any = None,
    dry_run: bool = False,
    uin: str = "",
    candidates: tuple[str, ...] = (),
) -> SendEngine:
    """按名字造引擎。`auto` = 有 hook 客户端就用管道，否则用 UI 通道。

    `auto` 故意不选 `PipeEngine`：当前 DLL 还没有发送实现，选它只会把
    「能发」变成「报 1404」。等 DLL 补上 `arklink.send`，把这里改成优先 pipe 即可。
    """
    kind = (kind or "auto").strip().lower()
    if kind in ("dry", "dry-run", "dryrun", "none"):
        return DryRunEngine()
    if kind == "pipe":
        return PipeEngine(client)
    if kind in ("host", "client", "ui"):
        return HostEngine(dry_run=dry_run, uin=uin, candidates=candidates)
    if kind in ("x11", "xdotool", "xvfb"):
        from .x11 import X11Engine  # 延迟导入：Windows 上没有 xdotool

        return X11Engine(dry_run=dry_run)
    return HostEngine(dry_run=dry_run, uin=uin, candidates=candidates)


class SendService:
    """策略 + 解析 + 引擎。所有入口（OneBot / CLI）都走这里。"""

    def __init__(self, policy: SendPolicy | None = None, engine: SendEngine | None = None) -> None:
        self.policy = policy or SendPolicy.from_config()
        self.engine: SendEngine = engine or DryRunEngine()
        self.last: SendResult | None = None
        self.history: list[SendResult] = []
        #: 正在处理中的请求数（WebUI 用它显示「排队中」）
        self.pending = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return getattr(self.engine, "available", False)

    def status(self) -> dict[str, Any]:
        with self._lock:
            last = self.last.as_dict() if self.last else None
            sent = len(self.history)
        return {
            "engine": getattr(self.engine, "name", "?"),
            "available": self.available,
            "policy": self.policy.describe(),
            "sent": sent,
            "pending": self.pending,
            "queue_depth": getattr(self.engine, "depth", 0),
            "last": last,
        }

    async def send(
        self,
        target: str | int | Peer,
        message: Any,
        *,
        uin: str = "",
        echo: str = "",
        chat_type: int = CHAT_C2C,
    ) -> SendResult:
        started = time.time()
        with self._lock:
            self.pending += 1
        try:
            return await self._send_one(
                target, message, uin=uin, echo=echo, chat_type=chat_type, started=started
            )
        finally:
            with self._lock:
                self.pending -= 1

    async def _send_one(
        self,
        target: str | int | Peer,
        message: Any,
        *,
        uin: str,
        echo: str,
        chat_type: int,
        started: float,
    ) -> SendResult:
        account: Decision = self.policy.check_account(uin)
        if not account.ok:
            return self._fail(account.code, account.reason, echo)

        decision = self.policy.resolve(target) if not isinstance(target, Peer) else self.policy._check_peer(target)
        if not decision.ok or decision.peer is None:
            return self._fail(decision.code, decision.reason, echo)
        peer = decision.peer
        if chat_type == CHAT_GROUP and not self.policy.allow_group:
            return self._fail(1403, "群聊发送被策略关闭（allow_group=false）", echo)
        if peer.chat_type != chat_type:
            chat_type = peer.chat_type

        try:
            parsed = el.parse_message(message)
            length: Decision = self.policy.check_length(el.plain_text(parsed))
            if not length.ok:
                return self._fail(length.code, length.reason, echo)
            el.validate(parsed, max_length=self.policy.max_length)
        except el.MessageError as exc:
            return self._fail(1400, str(exc), echo)

        request = SendRequest(
            chat_type=chat_type,
            peer_uid=peer.peer_uid,
            elements=parsed,
            uin=str(uin or (self.policy.allowed_uins[0] if self.policy.allowed_uins else "")),
            echo=echo,
            peer_name=peer.name or peer.nick or "",
        )
        result = await self.engine.send(request)
        result.extra.setdefault("elapsed_ms", int((time.time() - started) * 1000))
        result.extra.setdefault("peer", peer.as_dict())
        result.extra.setdefault("ts", time.strftime("%H:%M:%S", time.localtime(started)))
        result.extra.setdefault("peer_name", request.search_key)
        self._record(result)
        if not result.ok:
            logger.warning("发送失败 → %s：%s", peer.name or peer.peer_uid, result.msg)
        return result

    async def send_many(
        self,
        targets: Iterable[str | int | Peer],
        message: Any,
        *,
        uin: str = "",
        echo: str = "",
        chat_type: int = CHAT_C2C,
    ) -> list[SendResult]:
        """群发：同一份内容发给多个对象。

        故意**顺序**执行——UI 通道只有一个窗口，并发只会互相抢焦点。
        换成 `PipeEngine` 之后这里可以并行（`asyncio.gather`），
        但返回的列表永远与入参一一对应、顺序一致。
        """
        return [
            await self.send(target, message, uin=uin, echo=echo, chat_type=chat_type)
            for target in targets
        ]

    def _record(self, result: SendResult) -> None:
        with self._lock:
            self.last = result
            self.history.append(result)
            if len(self.history) > 500:      # 长跑不涨内存
                del self.history[: len(self.history) - 500]

    def _fail(self, code: int, msg: str, echo: str) -> SendResult:
        result = SendResult(ok=False, code=code, msg=msg, engine=getattr(self.engine, "name", "?"), echo=echo)
        self._record(result)
        logger.info("拒绝发送：%s", msg)
        return result
