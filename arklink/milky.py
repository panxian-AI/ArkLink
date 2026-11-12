"""Milky 协议接入（QQ 机器人应用接口标准 1.3）。

Milky 是 OneBot 11 的「重设计版」：结构体强类型、消息段带 `data` 包装、
事件用 `event_type` 打标签。协议端（NapCat-milky / Lagrange / Acidify /
LLOneBot / LuckyLilliaBot）暴露：

    POST /api/{endpoint}   JSON 请求 → {status, retcode, data}
    GET  /event            WS（优先）/ SSE 事件流

本模块把它翻译成 ArkBot 的统一事件（OneBot 形状的 dict），
因此**上层插件无需知道 Milky 存在**，也不需要 OneBot 兼容层。

参考：https://milky.ntqqrev.org/struct/Event 、 /guide/communication
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncIterator

import websockets

from .base import LinkAdapter, api_error
from .transport import HTTPError, apost_json, with_query

logger = logging.getLogger("arklink.milky")

# ---------------------------------------------------------------- 消息段映射

#: Milky 接收段 → OneBot 段。value 是 (type, data 提取函数)
_SEGMENT_IN: dict[str, str] = {
    "text": "text",
    "mention": "at",
    "mention_all": "at",
    "face": "face",
    "reply": "reply",
    "image": "image",
    "record": "record",
    "video": "video",
    "file": "file",
    "forward": "forward",
    "market_face": "mface",
    "light_app": "json",
    "xml": "xml",
    "markdown": "text",
}


def segments_to_onebot(segments: Any) -> list[dict[str, Any]]:
    """Milky `segments` → OneBot 段数组。"""
    out: list[dict[str, Any]] = []
    for raw in segments or []:
        if not isinstance(raw, dict):
            continue
        stype = str(raw.get("type") or "")
        data = raw.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        otype = _SEGMENT_IN.get(stype, stype)

        if stype == "text":
            out.append({"type": "text", "data": {"text": str(data.get("text") or "")}})
        elif stype == "mention":
            out.append({"type": "at", "data": {"qq": str(data.get("user_id") or "")}})
        elif stype == "mention_all":
            out.append({"type": "at", "data": {"qq": "all"}})
        elif stype == "face":
            out.append({"type": "face", "data": {"id": str(data.get("face_id") or "")}})
        elif stype == "reply":
            out.append({
                "type": "reply",
                "data": {
                    "id": str(data.get("message_seq") or ""),
                    "user_id": str(data.get("sender_id") or ""),
                    "sender_name": str(data.get("sender_name") or ""),
                },
            })
        elif stype in ("image", "record", "video"):
            resource = str(data.get("resource_id") or "")
            url = str(data.get("temp_url") or "")
            seg: dict[str, Any] = {"file": url or resource}
            if resource:
                seg["resource_id"] = resource
            for k in ("width", "height", "duration", "summary", "sub_type"):
                if data.get(k) not in (None, ""):
                    seg[k] = data[k]
            out.append({"type": otype, "data": seg})
        elif stype == "file":
            out.append({
                "type": "file",
                "data": {
                    "id": str(data.get("file_id") or ""),
                    "name": str(data.get("file_name") or ""),
                    "size": data.get("file_size") or 0,
                },
            })
        elif stype == "forward":
            out.append({
                "type": "forward",
                "data": {"id": str(data.get("forward_id") or ""), "title": str(data.get("title") or "")},
            })
        elif stype == "market_face":
            out.append({
                "type": "mface",
                "data": {"summary": str(data.get("summary") or ""), "url": str(data.get("url") or "")},
            })
        elif stype == "light_app":
            out.append({"type": "json", "data": {"data": str(data.get("json_payload") or "")}})
        elif stype == "xml":
            out.append({"type": "xml", "data": {"data": str(data.get("xml_payload") or "")}})
        elif stype == "markdown":
            out.append({"type": "text", "data": {"text": str(data.get("content") or "")}})
        else:  # 未知段：原样透出，插件可自行处理
            out.append({"type": otype or "unknown", "data": dict(data)})
    return out


def onebot_to_segments(message: Any) -> list[dict[str, Any]]:
    """OneBot 段数组（或纯文本）→ Milky 发送段。

    不认识的段直接丢弃并告警：Milky 是强类型协议，塞未知字段会导致整条
    消息被协议端拒绝（-400），丢一段总比整条发不出去好。
    """
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    chain = message
    if hasattr(message, "segments"):
        chain = [{"type": s.type, "data": dict(s.data)} for s in message.segments]
    if not isinstance(chain, list):
        return [{"type": "text", "data": {"text": str(message)}}]

    out: list[dict[str, Any]] = []
    for seg in chain:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if stype == "text":
            out.append({"type": "text", "data": {"text": str(data.get("text") or "")}})
        elif stype == "at":
            qq = str(data.get("qq") or "")
            if qq == "all":
                out.append({"type": "mention_all", "data": {}})
            elif qq.isdigit():
                out.append({"type": "mention", "data": {"user_id": int(qq)}})
        elif stype == "face":
            fid = str(data.get("id") or data.get("face_id") or "")
            if fid:
                out.append({"type": "face", "data": {"face_id": int(fid)}})
        elif stype == "reply":
            seq = str(data.get("id") or "")
            if seq.isdigit():
                out.append({"type": "reply", "data": {"message_seq": int(seq)}})
        elif stype == "image":
            out.append({"type": "image", "data": {"uri": str(data.get("file") or "")}})
        elif stype == "record":
            out.append({"type": "record", "data": {"uri": str(data.get("file") or "")}})
        elif stype == "video":
            out.append({"type": "video", "data": {"uri": str(data.get("file") or "")}})
        else:
            logger.debug("Milky 不支持发送的消息段 %s，已跳过", stype)
    return out


def _sender(data: dict[str, Any], scene: str) -> dict[str, Any]:
    """按场景提取 sender（保留 role/card/title，插件做权限判断要用）。"""
    if scene == "group":
        member = data.get("group_member") or {}
        return {
            "user_id": int(member.get("user_id") or data.get("sender_id") or 0),
            "nickname": str(member.get("nickname") or ""),
            "card": str(member.get("card") or ""),
            "role": str(member.get("role") or "member"),
            "title": str(member.get("title") or ""),
            "level": str(member.get("level") or ""),
        }
    friend = data.get("friend") or {}
    return {
        "user_id": int(friend.get("user_id") or data.get("sender_id") or 0),
        "nickname": str(friend.get("nickname") or ""),
        "remark": str(friend.get("remark") or ""),
    }


def event_to_raw(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Milky 事件 → OneBot 形状的事件 dict（交给 Event.from_raw）。"""
    if not isinstance(payload, dict):
        return None
    etype = str(payload.get("event_type") or "")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    base: dict[str, Any] = {
        "time": int(payload.get("time") or 0),
        "self_id": int(payload.get("self_id") or 0),
        "native": {"event_type": etype, "data": data},
        "milky_event": etype,
    }

    if etype == "message_receive":
        scene = str(data.get("message_scene") or "friend")
        peer_id = int(data.get("peer_id") or 0)
        segments = segments_to_onebot(data.get("segments"))
        text = "".join(
            str((s.get("data") or {}).get("text") or "") for s in segments if s.get("type") == "text"
        )
        raw = {
            **base,
            "post_type": "message",
            "message_type": "group" if scene == "group" else "private",
            "sub_type": "group" if scene == "temp" else ("normal" if scene == "group" else "friend"),
            "message_id": int(data.get("message_seq") or 0),
            "user_id": int(data.get("sender_id") or 0),
            "message": segments,
            "raw_message": text,
            "sender": _sender(data, scene),
        }
        if scene == "group":
            raw["group_id"] = peer_id
        return raw

    if etype == "message_recall":
        scene = str(data.get("message_scene") or "friend")
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_recall" if scene == "group" else "friend_recall",
            "sub_type": str(data.get("display_suffix") or ""),
            "group_id": int(data.get("peer_id") or 0) if scene == "group" else 0,
            "user_id": int(data.get("sender_id") or 0),
            "operator_id": int(data.get("operator_id") or 0),
            "message_id": int(data.get("message_seq") or 0),
        }

    if etype in ("group_member_increase", "group_member_decrease"):
        increase = etype.endswith("increase")
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_increase" if increase else "group_decrease",
            "sub_type": "invite" if data.get("invitor_id") else "approve",
            "group_id": int(data.get("group_id") or 0),
            "user_id": int(data.get("user_id") or 0),
            "operator_id": int(data.get("operator_id") or 0),
        }

    if etype == "group_mute":
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_ban",
            "sub_type": "ban",
            "group_id": int(data.get("group_id") or 0),
            "user_id": int(data.get("user_id") or 0),
            "operator_id": int(data.get("operator_id") or 0),
            "duration": int(data.get("duration") or 0),
        }

    if etype == "group_whole_mute":
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_ban",
            "sub_type": "ban" if data.get("is_mute") else "lift_ban",
            "group_id": int(data.get("group_id") or 0),
            "user_id": 0,
            "operator_id": int(data.get("operator_id") or 0),
            "duration": 0,
        }

    if etype == "group_admin_change":
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_admin",
            "sub_type": "set" if data.get("is_set") else "unset",
            "group_id": int(data.get("group_id") or 0),
            "user_id": int(data.get("user_id") or 0),
            "operator_id": int(data.get("operator_id") or 0),
        }

    if etype in ("friend_nudge", "group_nudge"):
        group = etype == "group_nudge"
        return {
            **base,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": int(data.get("group_id") or 0) if group else 0,
            "user_id": int(data.get("sender_id") or data.get("user_id") or 0),
            "target_id": int(data.get("receiver_id") or data.get("user_id") or 0),
        }

    if etype in ("group_file_upload", "friend_file_upload"):
        group = etype.startswith("group")
        return {
            **base,
            "post_type": "notice",
            "notice_type": "group_upload" if group else "offline_file",
            "group_id": int(data.get("group_id") or 0),
            "user_id": int(data.get("user_id") or 0),
            "file": {
                "id": str(data.get("file_id") or ""),
                "name": str(data.get("file_name") or ""),
                "size": data.get("file_size") or 0,
            },
        }

    if etype == "friend_request":
        return {
            **base,
            "post_type": "request",
            "request_type": "friend",
            "user_id": int(data.get("initiator_id") or 0),
            "comment": str(data.get("comment") or ""),
            "flag": str(data.get("initiator_uid") or data.get("initiator_id") or ""),
        }

    if etype in ("group_join_request", "group_invited_join_request", "group_invitation"):
        return {
            **base,
            "post_type": "request",
            "request_type": "group",
            "sub_type": "invite" if etype == "group_invitation" else "add",
            "group_id": int(data.get("group_id") or 0),
            "user_id": int(data.get("initiator_id") or data.get("target_user_id") or 0),
            "comment": str(data.get("comment") or ""),
            "flag": str(data.get("notification_seq") or data.get("invitation_seq") or ""),
        }

    # 其余事件（群名变更、精华、表态、bot_offline…）：保真透传成 notice，
    # 插件可按 notice_type 自行识别，不丢信息。
    return {**base, "post_type": "notice", "notice_type": etype or "unknown"}


# ---------------------------------------------------------------- 客户端


class MilkyClient:
    """Milky 协议端的最小客户端（HTTP API + WS 事件）。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        access_token: str = "",
        event_url: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout
        if event_url:
            self.event_url = event_url
        else:
            scheme = "wss" if self.base_url.startswith("https") else "ws"
            host = self.base_url.split("://", 1)[-1]
            self.event_url = f"{scheme}://{host}/event"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    async def call(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用一个 Milky API，返回 `data`；失败抛 ApiError。"""
        url = f"{self.base_url}/api/{endpoint}"
        try:
            resp = await apost_json(url, params or {}, self._headers, timeout=self.timeout)
        except HTTPError as e:
            if e.status == 401:
                raise api_error(endpoint, -401, "Milky 协议端鉴权失败") from e
            if e.status == 404:
                raise api_error(endpoint, 1404, "协议端没有该 API") from e
            raise api_error(endpoint, -1, str(e)) from e

        retcode = int(resp.get("retcode") or 0)
        if retcode != 0 or str(resp.get("status") or "ok") != "ok":
            # -404 = API 不存在 → 归一成 1404，让框架知道是「通道不支持」而不是故障
            code = 1404 if retcode == -404 else retcode
            raise api_error(endpoint, code, str(resp.get("message") or ""))
        data = resp.get("data")
        return data if isinstance(data, dict) else {"data": data}

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """连上 /event 并持续产出事件（WS 优先，协议端也支持 SSE）。"""
        headers = dict(self._headers)
        url = with_query(self.event_url, access_token=self.access_token or None)
        async with websockets.connect(
            url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
        ) as ws:
            async for message in ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                try:
                    import json

                    payload = json.loads(message)
                except Exception:
                    logger.debug("Milky 事件不是 JSON，忽略: %r", message[:80])
                    continue
                if isinstance(payload, dict):
                    yield payload


# ---------------------------------------------------------------- 适配器

_ENDPOINT_MAP: dict[str, str] = {
    "send_private_msg": "send_private_message",
    "send_group_msg": "send_group_message",
    "get_login_info": "get_login_info",
    "get_group_list": "get_group_list",
    "get_group_info": "get_group_info",
    "get_group_member_list": "get_group_member_list",
    "get_group_member_info": "get_group_member_info",
    "get_friend_list": "get_friend_list",
    "set_group_name": "set_group_name",
    "set_group_card": "set_group_member_card",
    "set_group_kick": "kick_group_member",
    "set_group_ban": "set_group_member_mute",
    "set_group_whole_ban": "set_group_whole_mute",
    "set_group_admin": "set_group_member_admin",
    "set_group_leave": "quit_group",
    "set_group_special_title": "set_group_member_special_title",
    "group_poke": "send_group_nudge",
    "friend_poke": "send_friend_nudge",
    "get_group_announcement": "get_group_announcements",
    "send_group_announcement": "send_group_announcement",
    "get_group_essence_msg_list": "get_group_essence_messages",
    "set_essence_msg": "set_group_essence_message",
    "upload_group_file": "upload_group_file",
    "get_group_files": "get_group_files",
    "delete_group_file": "delete_group_file",
}


class MilkyAdapter(LinkAdapter):
    """把 Milky 协议端挂载为 ArkBot 通道。

    bot.toml：
        [[adapters]]
        type = "milky"                      # 或 "arklink.milky:MilkyAdapter"
        base_url = "http://127.0.0.1:3000"
        access_token = "xxx"
    """

    name = "milky"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        access_token: str = "",
        event_url: str = "",
        reconnect_interval: float = 3.0,
        max_backoff: float = 60.0,
        timeout: float = 15.0,
    ) -> None:
        super().__init__()
        self.client = MilkyClient(base_url, access_token, event_url, timeout)
        self.reconnect_interval = reconnect_interval
        self.max_backoff = max_backoff
        # message_seq → (message_scene, peer_id)，撤回/查消息需要
        self._seq_index: dict[int, tuple[str, int]] = {}

    # ---------- 生命周期 ----------
    async def connect(self) -> None:
        backoff = self.reconnect_interval
        while self._running:
            try:
                async for payload in self.client.events():
                    backoff = self.reconnect_interval
                    await self._on_payload(payload)
                self._connected.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected.clear()
                logger.warning("Milky 事件流断开: %s（%.1fs 后重连）", e, backoff)
            if not self._running:
                break
            await asyncio.sleep(backoff + random.uniform(0, 1))
            backoff = min(backoff * 2, self.max_backoff)

    async def disconnect(self) -> None:
        self._connected.clear()
        self._seq_index.clear()

    async def _on_payload(self, payload: dict[str, Any]) -> None:
        etype = str(payload.get("event_type") or "")
        if etype == "bot_offline":
            self._connected.clear()
            logger.error("Milky 协议端报告机器人下线: %s", (payload.get("data") or {}).get("reason"))
        else:
            # 能收到事件 = 通道可用（Milky 没有独立的「已连接」信令）
            self._connected.set()

        data = payload.get("data") or {}
        if isinstance(data, dict) and data.get("message_seq") and data.get("peer_id"):
            scene = str(data.get("message_scene") or "friend")
            self._seq_index[int(data["message_seq"])] = (scene, int(data["peer_id"]))
            if len(self._seq_index) > 4096:  # 简单裁剪，避免长跑内存膨胀
                for key in list(self._seq_index)[:2048]:
                    self._seq_index.pop(key, None)

        raw = event_to_raw(payload)
        if raw is not None:
            await self._emit_dict(raw)

    # ---------- API ----------
    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})

        if action in ("send_msg", "send_message"):
            return await self._send_by_type(params)
        if action in ("send_private_msg", "send_group_msg"):
            return await self._send_by_type(params)
        if action == "delete_msg":
            return await self._recall(params)
        if action == "get_msg":
            return await self._get_msg(params)
        if action == "get_status":
            info = await self.client.call("get_impl_info")
            return {"online": True, "good": True, "impl": info}
        if action == "set_friend_add_request":
            endpoint = "accept_friend_request" if _truthy(params.get("approve")) else "reject_friend_request"
            return await self.client.call(endpoint, {"initiator_id": _int(params.get("flag"))})
        if action == "set_group_add_request":
            approve = _truthy(params.get("approve"))
            endpoint = "accept_group_request" if approve else "reject_group_request"
            return await self.client.call(endpoint, {
                "group_id": _int(params.get("group_id")),
                "notification_seq": _int(params.get("flag")),
                "is_filtered": bool(params.get("is_filtered")),
            })

        endpoint = _ENDPOINT_MAP.get(action)
        if endpoint is None:
            raise api_error(action, 1404, f"Milky 未映射的动作 {action}")

        payload = self._adapt_params(action, params)
        data = await self.client.call(endpoint, payload)
        return self._adapt_result(action, data, payload)

    async def _send_by_type(self, params: dict[str, Any]) -> dict[str, Any]:
        segments = onebot_to_segments(params.get("message"))
        if params.get("message_type") == "private" or ("user_id" in params and "group_id" not in params):
            data = await self.client.call("send_private_message", {
                "user_id": _int(params.get("user_id")),
                "message": segments,
            })
            scene, peer = "friend", _int(params.get("user_id"))
        else:
            data = await self.client.call("send_group_message", {
                "group_id": _int(params.get("group_id")),
                "message": segments,
            })
            scene, peer = "group", _int(params.get("group_id"))
        seq = int(data.get("message_seq") or 0)
        if seq:
            self._seq_index[seq] = (scene, peer)
        return {
            "message_id": seq,
            "message_seq": seq,
            "time": int(data.get("time") or 0),
        }

    async def _recall(self, params: dict[str, Any]) -> dict[str, Any]:
        seq = _int(params.get("message_id") or params.get("message_seq"))
        scene, peer = self._seq_index.get(seq, ("", 0))
        if not scene:
            raise api_error("delete_msg", 1404, f"未知 message_id={seq}（未在本通道出现过）")
        if scene == "group":
            await self.client.call("recall_group_message", {"group_id": peer, "message_seq": seq})
        else:
            await self.client.call("recall_private_message", {"user_id": peer, "message_seq": seq})
        return {}

    async def _get_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        seq = _int(params.get("message_id") or params.get("message_seq"))
        scene, peer = self._seq_index.get(seq, ("", 0))
        if not scene:
            raise api_error("get_msg", 1404, f"未知 message_id={seq}")
        data = await self.client.call("get_message", {
            "message_scene": scene,
            "peer_id": peer,
            "message_seq": seq,
        })
        message = data.get("message") or {}
        return {
            "message_id": seq,
            "message": segments_to_onebot(message.get("segments")),
            "sender": {"user_id": _int(message.get("sender_id"))},
            "time": int(message.get("time") or 0),
        }

    @staticmethod
    def _adapt_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in params.items() if k not in ("message_type", "auto_escape")}
        if action == "set_group_ban":
            payload = {
                "group_id": _int(params.get("group_id")),
                "user_id": _int(params.get("user_id")),
                "duration": _int(params.get("duration")),
            }
        elif action == "set_group_kick":
            payload = {
                "group_id": _int(params.get("group_id")),
                "user_id": _int(params.get("user_id")),
            }
        elif action == "group_poke":
            payload = {"group_id": _int(params.get("group_id")), "user_id": _int(params.get("user_id"))}
        elif action == "friend_poke":
            payload = {"user_id": _int(params.get("user_id"))}
        for key in ("group_id", "user_id", "message_seq", "duration"):
            if key in payload and isinstance(payload[key], str) and payload[key].lstrip("-").isdigit():
                payload[key] = int(payload[key])
        return payload

    @staticmethod
    def _adapt_result(action: str, data: dict[str, Any], payload: dict[str, Any]) -> Any:
        if action == "get_login_info":
            return {"user_id": _int(data.get("uin")), "nickname": str(data.get("nickname") or "")}
        if action == "get_group_member_info":
            return _member_to_onebot(data)
        return data


def _member_to_onebot(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": _int(data.get("user_id")),
        "nickname": str(data.get("nickname") or ""),
        "card": str(data.get("card") or ""),
        "role": str(data.get("role") or "member"),
        "title": str(data.get("title") or ""),
        "join_time": int(data.get("join_time") or 0),
        "last_sent_time": int(data.get("last_sent_time") or 0),
    }


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "ok", "approve")


def create(**cfg: Any) -> MilkyAdapter:
    """entry point 工厂：`milky = "arklink.milky:create"`。"""
    return MilkyAdapter(**cfg)
