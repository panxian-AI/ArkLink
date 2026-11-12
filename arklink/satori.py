"""Satori 协议接入（v1）。

Satori 是 Koishi 生态的跨平台聊天协议：QQ / Telegram / Discord / Lark
都能被同一个 Satori SDK（如 Chronocat、Koishi、Lagrange 桥）暴露出来。

    POST /v1/{resource}.{method}   HTTP RPC，头带 Satori-Platform / Satori-User-ID
    WS   /v1/events                opcode 3=IDENTIFY 0=EVENT 1=PING 2=PONG 4=READY

消息内容是 XML 片段（`<at id="1"/>你好`），本模块负责和 OneBot 段数组互转。

参考：https://satori.chat/zh-CN/protocol/ 、 /resources/message.html
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import zlib
from html import escape, unescape
from typing import Any, AsyncIterator

import websockets

from .base import LinkAdapter, api_error
from .transport import HTTPError, apost_json

logger = logging.getLogger("arklink.satori")

#: Satori 频道类型
CHANNEL_TEXT = 0
CHANNEL_DIRECT = 1
CHANNEL_CATEGORY = 2
CHANNEL_VOICE = 3

_ELEMENT_RE = re.compile(r"<([a-zA-Z][\w-]*)((?:\s+[\w-]+=\"[^\"]*\")*)\s*/>")
_ATTR_RE = re.compile(r"([\w-]+)=\"([^\"]*)\"")

#: Satori 元素 → OneBot 段类型
_ELEMENT_TO_ONEBOT = {
    "at": "at",
    "quote": "reply",
    "img": "image",
    "audio": "record",
    "video": "video",
    "file": "file",
    "face": "face",
    "sharp": "channel",
}


def _attr_id_to_str(value: Any) -> str:
    return str(value or "")


def parse_content(content: str) -> list[dict[str, Any]]:
    """Satori 消息内容（XML 片段）→ OneBot 段数组。"""
    segments: list[dict[str, Any]] = []
    pos = 0
    for m in _ELEMENT_RE.finditer(content or ""):
        if m.start() > pos:
            text = unescape(content[pos:m.start()])
            if text:
                segments.append({"type": "text", "data": {"text": _decode_qq_escapes(text)}})
        tag = m.group(1)
        attrs = dict(_ATTR_RE.findall(m.group(2) or ""))
        otype = _ELEMENT_TO_ONEBOT.get(tag, tag)
        if tag == "at":
            qq = _attr_id_to_str(attrs.get("id"))
            if qq in ("all", "here", ""):
                segments.append({"type": "at", "data": {"qq": "all" if qq else ""}})
            else:
                segments.append({"type": "at", "data": {"qq": qq, "name": attrs.get("name", "")}})
        elif tag == "quote":
            segments.append({"type": "reply", "data": {"id": _attr_id_to_str(attrs.get("id"))}})
        elif tag == "img":
            segments.append({"type": "image", "data": {"file": attrs.get("src", ""), "url": attrs.get("src", "")}})
        elif tag == "audio":
            segments.append({"type": "record", "data": {"file": attrs.get("src", "")}})
        elif tag == "video":
            segments.append({"type": "video", "data": {"file": attrs.get("src", "")}})
        elif tag == "file":
            segments.append({"type": "file", "data": {"file": attrs.get("src", ""), "name": attrs.get("title", "")}})
        elif tag == "face":
            segments.append({"type": "face", "data": {"id": _attr_id_to_str(attrs.get("id"))}})
        else:
            segments.append({"type": otype, "data": attrs})
        pos = m.end()
    if pos < len(content or ""):
        text = unescape(content[pos:])
        if text:
            segments.append({"type": "text", "data": {"text": _decode_qq_escapes(text)}})
    return segments


def _decode_qq_escapes(text: str) -> str:
    """QQ 桥常把 [ ] , 转义成 &#91; 形式（OneBot CQ 兼容），还原回字面量。"""
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",")


def encode_content(message: Any) -> str:
    """OneBot 段数组 → Satori 消息内容。"""
    if isinstance(message, str):
        return escape(message)
    chain = message
    if hasattr(message, "segments"):
        chain = [{"type": s.type, "data": dict(s.data)} for s in message.segments]
    if not isinstance(chain, list):
        return escape(str(message))

    out: list[str] = []
    for seg in chain:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if stype == "text":
            out.append(escape(str(data.get("text") or "")))
        elif stype == "at":
            qq = str(data.get("qq") or "")
            if qq:
                out.append(f'<at id="{escape(qq)}"/>')
        elif stype == "reply":
            mid = str(data.get("id") or "")
            if mid:
                out.append(f'<quote id="{escape(mid)}"/>')
        elif stype == "image":
            out.append(f'<img src="{escape(str(data.get("file") or data.get("url") or ""))}"/>')
        elif stype == "record":
            out.append(f'<audio src="{escape(str(data.get("file") or ""))}"/>')
        elif stype == "video":
            out.append(f'<video src="{escape(str(data.get("file") or ""))}"/>')
        elif stype == "file":
            out.append(f'<file src="{escape(str(data.get("file") or ""))}"/>')
        elif stype == "face":
            out.append(f'<face id="{escape(str(data.get("id") or ""))}"/>')
        else:
            logger.debug("Satori 不支持发送的消息段 %s，已跳过", stype)
    return "".join(out)


def _stable_id(value: Any) -> int:
    """Satori 的 id 是字符串；OneBot 用 int。

    纯数字直接转，否则用 CRC32 生成稳定伪 ID（同 id 永远同值，可跨重启），
    反向映射由适配器自己维护。
    """
    text = str(value or "")
    if text.lstrip("-").isdigit():
        return int(text)
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


def event_to_raw(event: dict[str, Any], self_id: int = 0) -> dict[str, Any] | None:
    """Satori 事件 → OneBot 形状 dict。"""
    if not isinstance(event, dict):
        return None
    etype = str(event.get("type") or "")
    base: dict[str, Any] = {
        "time": int(event.get("timestamp") or 0),
        "self_id": int(event.get("self_id") or self_id or 0),
        "native": event,
        "satori_event": etype,
    }
    channel = event.get("channel") or {}
    channel_id = str(channel.get("id") or "")
    channel_type = int(channel.get("type") or 0)
    user = event.get("user") or {}
    member = event.get("member") or {}
    message = event.get("message") or {}

    if etype in ("message-created", "message-updated"):
        segments = parse_content(str(message.get("content") or ""))
        mid = _stable_id(message.get("id"))
        text = "".join(
            str((s.get("data") or {}).get("text") or "") for s in segments if s.get("type") == "text"
        )
        is_direct = channel_type == CHANNEL_DIRECT
        raw = {
            **base,
            "post_type": "message",
            "message_type": "private" if is_direct else "group",
            "sub_type": "friend" if is_direct else "normal",
            "message_id": mid,
            "user_id": _stable_id(user.get("id") or member.get("user_id") or channel_id),
            "message": segments,
            "raw_message": text,
            "sender": {
                "user_id": _stable_id(user.get("id") or member.get("user_id")) or 0,
                "nickname": str(user.get("name") or member.get("name") or ""),
                "card": str(member.get("nick") or ""),
                "role": _role_of(member),
            },
            "satori_channel_id": channel_id,
            "satori_message_id": str(message.get("id") or ""),
        }
        if not is_direct:
            raw["group_id"] = _stable_id(channel_id)
        return raw

    if etype in ("message-deleted",):
        is_direct = channel_type == CHANNEL_DIRECT
        return {
            **base,
            "post_type": "notice",
            "notice_type": "friend_recall" if is_direct else "group_recall",
            "group_id": 0 if is_direct else _stable_id(channel_id),
            "user_id": _stable_id(user.get("id") or member.get("user_id")),
            "message_id": _stable_id(message.get("id")),
        }

    if etype == "guild-member-added":
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "approve",
            "group_id": _stable_id((event.get("guild") or {}).get("id")),
            "user_id": _stable_id(user.get("id")),
            "operator_id": _stable_id((event.get("operator") or {}).get("id")),
        }

    if etype == "guild-member-removed":
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "kick",
            "group_id": _stable_id((event.get("guild") or {}).get("id")),
            "user_id": _stable_id(user.get("id")),
            "operator_id": _stable_id((event.get("operator") or {}).get("id")),
        }

    return {**base, "post_type": "notice", "notice_type": etype or "unknown"}


def _role_of(member: dict[str, Any]) -> str:
    if member.get("is_owner"):
        return "owner"
    roles = member.get("roles") or []
    if isinstance(roles, list) and roles:
        return "admin"
    return "member"


class SatoriClient:
    """Satori SDK 客户端（HTTP RPC + WS 事件）。"""

    def __init__(
        self,
        api_base: str = "http://127.0.0.1:5140",
        token: str = "",
        platform: str = "qq",
        user_id: str = "",
        events_url: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.platform = platform
        self.user_id = user_id
        self.timeout = timeout
        if events_url:
            self.events_url = events_url
        else:
            scheme = "wss" if self.api_base.startswith("https") else "ws"
            host = self.api_base.split("://", 1)[-1]
            self.events_url = f"{scheme}://{host}/v1/events"

    def _headers(self, platform: str = "", user_id: str = "") -> dict[str, str]:
        headers = {
            "Satori-Platform": platform or self.platform,
            "Satori-User-ID": user_id or self.user_id,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def call(self, resource_method: str, payload: dict[str, Any]) -> Any:
        url = f"{self.api_base}/v1/{resource_method}"
        try:
            resp = await apost_json(url, payload, self._headers(), timeout=self.timeout)
        except HTTPError as e:
            if e.status == 404:
                raise api_error(resource_method, 1404, "Satori SDK 不支持该 API") from e
            if e.status == 401:
                raise api_error(resource_method, -401, "Satori 鉴权失败") from e
            raise api_error(resource_method, -1, str(e)) from e
        return resp

    async def events(self, sn: int = 0) -> AsyncIterator[dict[str, Any]]:
        """连上 /v1/events，IDENTIFY 后持续产出事件 body。"""
        url = self.events_url
        async with websockets.connect(url, max_size=16 * 1024 * 1024, ping_interval=20) as ws:
            await ws.send(json.dumps({
                "op": 3,
                "body": {"token": self.token, "sn": sn},
            }, ensure_ascii=False))
            keepalive = asyncio.create_task(_ping_loop(ws), name="arklink-satori-ping")
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        message = message.decode("utf-8", "replace")
                    try:
                        frame = json.loads(message)
                    except Exception:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    op = int(frame.get("op") or 0)
                    body = frame.get("body")
                    if op == 4:  # READY：拿到 login 列表
                        yield {"type": "ready", "body": body}
                    elif op == 0 and isinstance(body, dict):
                        yield body
                    elif op == 5:  # META
                        logger.debug("Satori META: %s", body)
            finally:
                keepalive.cancel()


async def _ping_loop(ws: Any, interval: float = 10.0) -> None:  # pragma: no cover - 网络
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "body": {}}))
    except asyncio.CancelledError:
        raise
    except Exception:
        return


class SatoriAdapter(LinkAdapter):
    """把 Satori SDK（Koishi / Chronocat / Lagrange）挂载为 ArkBot 通道。

    bot.toml：
        [[adapters]]
        type = "satori"
        api_base = "http://127.0.0.1:5140"
        token = "xxx"
        platform = "qq"
    """

    name = "satori"

    def __init__(
        self,
        api_base: str = "http://127.0.0.1:5140",
        token: str = "",
        platform: str = "qq",
        user_id: str = "",
        events_url: str = "",
        reconnect_interval: float = 3.0,
        max_backoff: float = 60.0,
        timeout: float = 15.0,
    ) -> None:
        super().__init__()
        self.client = SatoriClient(api_base, token, platform, user_id, events_url, timeout)
        self.reconnect_interval = reconnect_interval
        self.max_backoff = max_backoff
        self.self_id_str = user_id
        # 伪数字 ID → 原生字符串 ID
        self._channel_of: dict[int, str] = {}
        self._message_of: dict[int, tuple[str, str]] = {}
        self._login: dict[str, Any] = {}

    # ---------- 生命周期 ----------
    async def connect(self) -> None:
        backoff = self.reconnect_interval
        while self._running:
            try:
                async for event in self.client.events():
                    backoff = self.reconnect_interval
                    await self._on_event(event)
                self._connected.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected.clear()
                logger.warning("Satori 事件流断开: %s（%.1fs 后重连）", e, backoff)
            if not self._running:
                break
            await asyncio.sleep(backoff + random.uniform(0, 1))
            backoff = min(backoff * 2, self.max_backoff)

    async def disconnect(self) -> None:
        self._connected.clear()
        self._channel_of.clear()
        self._message_of.clear()

    async def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "ready":
            logins = (event.get("body") or {}).get("logins") or []
            if logins:
                self._login = logins[0]
                self.self_id_str = str(self._login.get("user", {}).get("id") or self.self_id_str)
                if self._login.get("self_id"):
                    self.client.user_id = str(self._login["self_id"])
            self._connected.set()
            logger.info("Satori 就绪: %s", self.self_id_str)
            return

        self._connected.set()
        channel_id = str((event.get("channel") or {}).get("id") or "")
        message_id = str((event.get("message") or {}).get("id") or "")
        if channel_id:
            self._channel_of[_stable_id(channel_id)] = channel_id
        if channel_id and message_id:
            self._message_of[_stable_id(message_id)] = (channel_id, message_id)

        raw = event_to_raw(event, self_id=_stable_id(self.self_id_str))
        if raw is not None:
            await self._emit_dict(raw)

    # ---------- API ----------
    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})

        if action in ("send_msg", "send_private_msg", "send_group_msg"):
            return await self._send(action, params)
        if action == "delete_msg":
            mid = _stable_id(params.get("message_id"))
            channel_id, message_id = self._message_of.get(mid, ("", ""))
            if not channel_id:
                raise api_error(action, 1404, f"未知 message_id={mid}")
            await self.client.call("message.delete", {"channel_id": channel_id, "message_id": message_id})
            return {}
        if action == "get_msg":
            mid = _stable_id(params.get("message_id"))
            channel_id, message_id = self._message_of.get(mid, ("", ""))
            if not channel_id:
                raise api_error(action, 1404, f"未知 message_id={mid}")
            data = await self.client.call("message.get", {"channel_id": channel_id, "message_id": message_id})
            return {"message_id": mid, "message": parse_content(str((data.get("content") or "")))}
        if action == "get_login_info":
            return {"user_id": _stable_id(self.self_id_str), "nickname": str(self._login.get("nickname") or "")}
        if action == "get_status":
            return {"online": self.is_connected, "good": self.is_connected}
        if action in ("get_group_list", "get_guild_list"):
            data = await self.client.call("guild.list", {"next": ""})
            guilds = data.get("data") or []
            return [
                {
                    "group_id": _stable_id(g.get("id")),
                    "group_name": str(g.get("name") or ""),
                    "member_count": 0,
                }
                for g in guilds
                if isinstance(g, dict)
            ]
        if action == "get_group_member_list":
            guild_id = self._channel_of.get(_stable_id(params.get("group_id")), str(params.get("group_id")))
            data = await self.client.call("guild.member.list", {"guild_id": guild_id, "next": ""})
            return [
                {
                    "user_id": _stable_id(m.get("user", {}).get("id") if isinstance(m.get("user"), dict) else m.get("id")),
                    "nickname": str((m.get("user") or {}).get("name") or m.get("name") or ""),
                    "card": str(m.get("nick") or ""),
                    "role": _role_of(m if isinstance(m, dict) else {}),
                }
                for m in (data.get("data") or [])
                if isinstance(m, dict)
            ]
        if action == "group_poke":
            return await self.client.call("message.create", {
                "channel_id": self._channel_of.get(_stable_id(params.get("group_id")), str(params.get("group_id"))),
                "content": f'<at id="{params.get("user_id")}"/>',
            })
        raise api_error(action, 1404, f"Satori 未映射的动作 {action}")

    async def _send(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "send_private_msg" or params.get("message_type") == "private":
            channel_id = self._channel_of.get(_stable_id(params.get("user_id")), str(params.get("user_id")))
        else:
            channel_id = self._channel_of.get(_stable_id(params.get("group_id")), str(params.get("group_id")))
        content = encode_content(params.get("message"))
        data = await self.client.call("message.create", {"channel_id": channel_id, "content": content})
        # 规范要求返回 Message 数组；个别 SDK 直接返回单个对象，两种都认
        messages = data.get("data") if isinstance(data, dict) else None
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            messages = [data]
        first = messages[0] if isinstance(messages, list) and messages else {}
        message_id = str(first.get("id") or "")
        hid = _stable_id(message_id) if message_id else 0
        if message_id:
            self._message_of[hid] = (channel_id, message_id)
        return {"message_id": hid}


def create(**cfg: Any) -> SatoriAdapter:
    """entry point 工厂：`satori = "arklink.satori:create"`。"""
    return SatoriAdapter(**cfg)
