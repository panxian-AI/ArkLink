"""消息总线：所有「收到的消息」都从这里过一遍。

为什么要有这一层：

* 收消息的来源可能有很多个（进程内 hook、外部采集器、手工灌入做联调），
  但**下游不该关心来源**——它们只订阅一条流。
* 发送与接收要能在一个进程里共存（WebUI / HTTP API / OneBot 服务端同时开），
  所以收件箱必须是线程安全的、可回放的、有界不爆内存的。

设计取舍：

* **有界环形缓冲**（默认 2000 条）。消息是可以丢的，内存不能爆；要长期存档
  请用 `GET /api/v1/messages` 落地，而不是把进程当数据库。
* **单调递增的 seq**：客户端断线重连时用 `since=seq` 续传，比按时间戳去重可靠
  （同一秒内多条消息时间戳会撞）。
* **订阅用标准库 `queue.Queue`**：SSE、OneBot 事件流、脚本回调都能直接消费，
  不引入 asyncio 与线程混用的复杂度。
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 聊天类型，沿用 OneBot 的约定
CHAT_C2C = 1
CHAT_GROUP = 2

DEFAULT_MAXLEN = 2000

#: 外部采集器可能用的字段名 → 我们的字段名。宁可爱吃多种写法，也不要让接入方
#: 为了对齐字段去改自己的采集器。
_ALIASES: dict[str, tuple[str, ...]] = {
    "text": ("text", "message", "raw_message", "content", "msg"),
    "sender_uid": ("sender_uid", "user_id", "from_uid", "from", "sender"),
    "sender_name": ("sender_name", "nickname", "sender_nick", "card"),
    "peer_uid": ("peer_uid", "group_id", "chat_id", "target"),
    "peer_name": ("peer_name", "group_name", "chat_name"),
    "message_id": ("message_id", "msg_id", "id", "msgId"),
    "ts": ("ts", "time", "timestamp"),
}


def _pick(payload: dict[str, Any], key: str, default: Any = "") -> Any:
    for alias in _ALIASES.get(key, (key,)):
        if alias in payload and payload[alias] not in (None, ""):
            return payload[alias]
    return default


def _to_text(value: Any) -> str:
    """把「可能是段数组」的内容压成纯文本。

    采集器有的直接给字符串，有的给 OneBot 段数组（`[{"type":"text","data":{"text":..}}]`），
    这里统一成文本，`raw` 里保留原文，需要细节的下游自己去取。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                data = item.get("data") if isinstance(item.get("data"), dict) else item
                kind = item.get("type") or data.get("type") or "text"
                if kind == "text":
                    parts.append(str(data.get("text") or ""))
                elif kind in ("image", "picture"):
                    parts.append("[图片]")
                elif kind in ("face", "emoji"):
                    parts.append("[表情]")
                elif kind == "at":
                    parts.append("@" + str(data.get("qq") or data.get("name") or ""))
                else:
                    parts.append(f"[{kind}]")
        return "".join(parts)
    if value is None:
        return ""
    return str(value)


@dataclass
class Message:
    """一条收到的消息。字段刻意和 OneBot 保持接近，降低接入方的心智负担。"""

    text: str
    chat_type: int = CHAT_C2C
    peer_uid: str = ""
    peer_name: str = ""
    sender_uid: str = ""
    sender_name: str = ""
    message_id: str = ""
    ts: float = field(default_factory=time.time)
    seq: int = 0
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """去重用的稳定键：优先平台消息 id，没有就用内容指纹。"""
        if self.message_id:
            return f"id:{self.message_id}"
        return f"fp:{self.chat_type}:{self.peer_uid}:{self.sender_uid}:{self.text}:{int(self.ts)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "id": self.message_id,
            "ts": round(self.ts, 3),
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "chat_type": self.chat_type,
            "peer_uid": self.peer_uid,
            "peer_name": self.peer_name,
            "sender_uid": self.sender_uid,
            "sender_name": self.sender_name,
            "text": self.text,
            "source": self.source,
        }


class MessageBus:
    """有界、线程安全、可续传的消息总线。"""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN, *, dedupe_window: int = 200) -> None:
        self._items: deque[Message] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._subs: dict[int, queue.Queue[Message]] = {}
        self._sub_ids = itertools.count(1)
        self._recent_keys: deque[str] = deque(maxlen=dedupe_window)
        self._dropped = 0
        self._dupes = 0
        self._total = 0
        self._started = time.time()

    # ------------------------------------------------------------ 写入
    def publish(self, payload: dict[str, Any], *, source: str = "") -> Message | None:
        """收一条消息进来。重复的返回 None（同一个 message_id 只发一次）。"""
        if not isinstance(payload, dict):
            raise TypeError("消息必须是 dict")

        chat_type = payload.get("chat_type") or payload.get("message_type") or CHAT_C2C
        if isinstance(chat_type, str):
            chat_type = CHAT_GROUP if chat_type in ("group", "guild", "群") else CHAT_C2C
        chat_type = int(chat_type)
        if payload.get("group_id") and not payload.get("peer_uid"):
            chat_type = CHAT_GROUP

        ts_raw = _pick(payload, "ts", 0)
        try:
            ts = float(ts_raw) if ts_raw else time.time()
        except (TypeError, ValueError):
            ts = time.time()
        if ts > 1e11:  # 毫秒时间戳
            ts /= 1000.0

        msg = Message(
            text=_to_text(_pick(payload, "text", "")),
            chat_type=chat_type,
            peer_uid=str(_pick(payload, "peer_uid", "") or (payload.get("group_id") or "")),
            peer_name=str(_pick(payload, "peer_name", "")),
            sender_uid=str(_pick(payload, "sender_uid", "")),
            sender_name=str(_pick(payload, "sender_name", "")),
            message_id=str(_pick(payload, "message_id", "")),
            ts=ts,
            source=source or str(payload.get("source") or ""),
            raw=payload,
        )
        # 私聊里「会话对象」就是发消息的人；采集器只给 user_id 时补上，
        # 否则下游按 peer_uid 会话去重/回复时会拿到空串。
        if msg.chat_type == CHAT_C2C and not msg.peer_uid:
            msg.peer_uid = msg.sender_uid

        with self._lock:
            if msg.key in self._recent_keys:
                self._dupes += 1
                return None
            self._recent_keys.append(msg.key)
            msg.seq = next(self._seq)
            self._total += 1
            if len(self._items) == self._items.maxlen:
                self._dropped += 1
            self._items.append(msg)
            targets = list(self._subs.values())

        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:  # 订阅方自己不快，丢它的，不拖累别人
                pass
        return msg

    def publish_many(self, payloads: Iterable[dict[str, Any]], *, source: str = "") -> list[Message]:
        out: list[Message] = []
        for payload in payloads:
            msg = self.publish(payload, source=source)
            if msg is not None:
                out.append(msg)
        return out

    # ------------------------------------------------------------ 读出
    def since(self, seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """拿 seq 之后的 N 条。`since=0` 表示「从最老的还在缓冲里的开始」。"""
        limit = max(0, min(int(limit or 0), 1000))
        with self._lock:
            items = [m for m in self._items if m.seq > int(seq or 0)]
        return [m.as_dict() for m in items[:limit]]

    def last_seq(self) -> int:
        with self._lock:
            return self._items[-1].seq if self._items else 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self._total,
                "buffered": len(self._items),
                "capacity": self._items.maxlen,
                "last_seq": self._items[-1].seq if self._items else 0,
                "dropped": self._dropped,
                "duplicates": self._dupes,
                "subscribers": len(self._subs),
                "uptime": round(time.time() - self._started, 1),
            }

    # ------------------------------------------------------------ 订阅
    def subscribe(self, maxsize: int = 500) -> tuple[int, queue.Queue[Message]]:
        q: queue.Queue[Message] = queue.Queue(maxsize=maxsize)
        with self._lock:
            sid = next(self._sub_ids)
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)
