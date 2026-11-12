"""把推送的 `MsgPush` protobuf 翻成 OneBot 11 事件。

服务端主动推的消息走 `trpc.msg.olpush.OlPushService.MsgPush`，包体是一层很浅的
protobuf：

```
MsgPush        { 1: CommonMessage, 5: bool }
CommonMessage  { 1: RoutingHead, 2: ContentHead, 3: MessageBody }
RoutingHead    { 1: FromUin, 2: FromUid, 5: ToUin, 6: ToUid, 7: CommonC2C, 8: CommonGroup }
ContentHead    { 1: Type, 2: SubType, 5: Sequence, 6: Time, 11: ClientSeq, 12: MsgUid }
MessageBody    { 1: RichText }
RichText       { 2: repeated Elem }
Elem           { 1: Text, 2: Face, 4: OffPic, 8: CustomFace, 12: RichMsg,
                 19: Video, 45: SourceMsg(引用), 51: LightApp, 53: CommonElem(at 等) }
```

字段号不是猜的：和 PC 端 `nt_msg.msg_common` 一一对应，改版时最可能动的是
`Elem` 里新增的 oneof 分支——遇到不认识的直接降级成 `[不支持的消息]`，
不会把整条事件丢掉（`raw` 里留着原始字节，方便回头补映射）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import pb

#: ContentHead.Type
MSG_TYPE_RECALL = 2

#: CommonElem.ServiceType
SERVICE_AT = 1
SERVICE_AT_ALL = 1  # 群里 @全体：type=1 且 uin=0

#: 常见 Element 的降级文案
FALLBACK = {
    "face": "[表情]",
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片消息]",
    "forward": "[合并转发]",
}


def _bytes(value: Any) -> bytes:
    return value if isinstance(value, bytes) else b""


def _text(value: Any) -> str:
    return _bytes(value).decode("utf-8", "replace")


def _first(fields: dict[int, list[Any]], key: int, default: Any = None) -> Any:
    values = fields.get(key) or []
    return values[0] if values else default


def _uint(fields: dict[int, list[Any]], key: int, default: int = 0) -> int:
    value = _first(fields, key, default)
    return int(value) if isinstance(value, int) else default


def _sub(buf: bytes) -> dict[int, list[Any]]:
    return pb.decode(buf) if buf else {}


@dataclass
class Elem:
    """归一化后的一个消息段（内部结构，最后会转成 OneBot 的 segment）。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class IncomingMessage:
    """推送消息归一化结果，`to_onebot()` 出 OneBot 11 事件。"""

    kind: str = "group"          # group | private | unknown
    group_id: int = 0
    group_name: str = ""
    user_id: int = 0
    user_uid: str = ""
    card: str = ""
    time: int = 0
    msg_id: int = 0
    seq: int = 0
    msg_type: int = 0
    sub_type: int = 0
    elems: list[Elem] = field(default_factory=list)
    raw: bytes = b""

    @property
    def recalled(self) -> bool:
        return self.msg_type == MSG_TYPE_RECALL

    @property
    def text(self) -> str:
        return "".join(
            e.data.get("text", "") if e.type == "text" else FALLBACK.get(e.type, "")
            for e in self.elems
        )

    def to_onebot(self, self_id: int = 0) -> dict[str, Any]:
        base: dict[str, Any] = {
            "post_type": "message",
            "self_id": self_id,
            "time": self.time,
            "message_id": self.msg_id or self.seq,
            "raw_message": self.text,
            "font": 0,
            "message": [{"type": e.type, "data": e.data} for e in self.elems],
            "sender": {"user_id": self.user_id, "nickname": self.card or str(self.user_id)},
        }
        if self.kind == "group":
            base.update(
                {
                    "message_type": "group",
                    "sub_type": "normal",
                    "group_id": self.group_id,
                    "user_id": self.user_id,
                    "message_seq": self.seq,
                    "sender": {
                        "user_id": self.user_id,
                        "nickname": self.card or str(self.user_id),
                        "card": self.card,
                        "role": "member",
                    },
                }
            )
            if self.group_name:
                base["group_name"] = self.group_name
        elif self.kind == "private":
            base.update(
                {
                    "message_type": "private",
                    "sub_type": "friend",
                    "user_id": self.user_id,
                    "target_id": self.user_id,
                }
            )
        else:
            base["message_type"] = "private"
            base["user_id"] = self.user_id
        return base

    def to_notice(self, self_id: int = 0) -> dict[str, Any] | None:
        """撤回、戳一戳这类没有正文的推送。"""
        if not self.recalled:
            return None
        if self.kind == "group":
            return {
                "post_type": "notice",
                "notice_type": "group_recall",
                "self_id": self_id,
                "group_id": self.group_id,
                "user_id": self.user_id,
                "operator_id": self.user_id,
                "message_id": self.msg_id or self.seq,
                "time": self.time,
            }
        if self.kind == "private":
            return {
                "post_type": "notice",
                "notice_type": "friend_recall",
                "self_id": self_id,
                "user_id": self.user_id,
                "message_id": self.msg_id or self.seq,
                "time": self.time,
            }
        return None


# ------------------------------------------------------------------ 段解析


def _elem_of(idx: int, blob: bytes) -> Elem | None:
    """一个 `Elem` 消息 → 一段。不认识的返回 None。"""
    fields = _sub(blob)
    if idx == 1:  # Text
        text = _text(_first(_sub(_bytes(_first(fields, 1))), 1))
        return Elem("text", {"text": text}) if text else None
    if idx == 2:  # Face
        index = _uint(_sub(_bytes(_first(fields, 2))), 1)
        return Elem("face", {"id": str(index)})
    if idx in (4, 8):  # NotOnlineImage / CustomFace
        pic = _sub(_bytes(_first(fields, idx)))
        url = ""
        for key in (15, 14, 26, 12):  # OrigUrl / BigUrl / Url400 / ThumbUrl
            candidate = _text(_first(pic, key))
            if candidate.startswith("http"):
                url = candidate
                break
        md5hex = _bytes(_first(pic, 7)).hex()
        return Elem(
            "image",
            {
                "file": md5hex or _text(_first(pic, 1)),
                "url": url,
                "sub_type": 0,
                "summary": "[图片]",
            },
        )
    if idx == 12:  # RichMsg（xml 卡片）
        xml = _text(_first(_sub(_bytes(_first(fields, 12))), 1))
        return Elem("json", {"data": xml}) if xml else None
    if idx == 19:  # VideoFile
        video = _sub(_bytes(_first(fields, 19)))
        return Elem("video", {"file": _text(_first(video, 1)), "url": _text(_first(video, 11))})
    if idx == 45:  # SourceMsg（引用）
        source = _sub(_bytes(_first(fields, 45)))
        seqs = source.get(1) or []
        target = int(seqs[0]) if seqs and isinstance(seqs[0], int) else 0
        return Elem("reply", {"id": str(target)})
    if idx == 51:  # LightAppElem
        data = _bytes(_first(_sub(_bytes(_first(fields, 51))), 1))
        return Elem("json", {"data": _text(data)}) if data else None
    if idx == 53:  # CommonElem：@ 表情 等都走这里
        common = _sub(_bytes(_first(fields, 53)))
        service = _uint(common, 1)
        pb_elem = _bytes(_first(common, 2))
        if service == SERVICE_AT:
            at = _sub(pb_elem)
            uin = _uint(at, 2)
            uid = _text(_first(at, 5))
            name = _text(_first(at, 6)) or _text(_first(at, 3))
            if not uin and not uid:
                return Elem("at", {"qq": "all", "name": "全体成员"})
            return Elem("at", {"qq": str(uin) if uin else uid, "name": name})
        if service == 33:  # 小表情
            face = _sub(pb_elem)
            return Elem("face", {"id": str(_uint(face, 1))})
        return None
    return None


def _segments(rich_text: bytes) -> list[Elem]:
    fields = _sub(rich_text)
    out: list[Elem] = []
    for blob in fields.get(2, []):
        if not isinstance(blob, bytes):
            continue
        elem_fields = pb.decode(blob)
        for idx in elem_fields:
            parsed = _elem_of(idx, blob)
            if parsed is not None:
                out.append(parsed)
                break
    return out


# ------------------------------------------------------------------ 入口


def parse_push(body: bytes, *, self_id: int = 0) -> IncomingMessage | None:
    """`MsgPush` 原始字节 → 归一化消息；不是消息（心跳/同步包）返回 None。"""
    fields = _sub(body)
    common = _bytes(_first(fields, 1))
    if not common:
        return None
    head = _sub(common)
    routing = _sub(_bytes(_first(head, 1)))
    content = _sub(_bytes(_first(head, 2)))
    msg_body = _sub(_bytes(_first(head, 3)))

    group = _sub(_bytes(_first(routing, 8)))
    c2c = _sub(_bytes(_first(routing, 7)))
    from_uin = _uint(routing, 1)
    from_uid = _text(_first(routing, 2))

    message = IncomingMessage(
        user_id=from_uin,
        user_uid=from_uid,
        time=_uint(content, 6),
        msg_id=_uint(content, 12) or _uint(content, 11),
        seq=_uint(content, 5),
        msg_type=_uint(content, 1),
        sub_type=_uint(content, 2),
        raw=body,
    )
    if group:
        message.kind = "group"
        message.group_id = _uint(group, 1)
        message.group_name = _text(_first(group, 7))
        message.card = _text(_first(group, 4))
    elif c2c or from_uin:
        message.kind = "private"
        message.card = _text(_first(c2c, 6))
        message.user_id = from_uin or _uint(c2c, 4)
    else:
        message.kind = "unknown"

    rich = _bytes(_first(msg_body, 1))
    message.elems = _segments(rich)
    if not message.elems and not message.recalled:
        # 长消息/加密消息走 2/3 字段，当前只标记出来，正文留给上层按需拉取
        if _bytes(_first(msg_body, 2)) or _bytes(_first(msg_body, 3)):
            message.elems = [Elem("text", {"text": "[长消息，需拉取]"})]
    _ = self_id
    return message


def to_onebot(body: bytes, *, self_id: int = 0) -> dict[str, Any] | None:
    """一步到位：推送包体 → OneBot 11 事件（含 notice）。"""
    message = parse_push(body, self_id=self_id)
    if message is None:
        return None
    notice = message.to_notice(self_id=self_id)
    return notice or message.to_onebot(self_id=self_id)


def decode_segments(event: dict[str, Any]) -> str:
    """调试用：把事件里的段拼成一行，方便日志。"""
    parts = []
    for segment in event.get("message", []):
        kind = segment.get("type")
        data = segment.get("data", {})
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            parts.append(f"@{data.get('name') or data.get('qq')}")
        elif kind == "image":
            parts.append(f"[图片 {data.get('url', '')[:40]}]")
        else:
            parts.append(f"[{kind}]")
    return "".join(parts)


def json_of(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, default=str)
