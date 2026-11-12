"""QQ 注入通道的管道帧协议（字节级）。

设计目标是「与现成 hook DLL 互操作」，所以帧头是固定 40 字节、字段顺序不可改：

```
off  size  字段
0    4     magic   = 0x31504851（"QHP1"）
4    2     version = 1
6    2     op
8    4     requestId
12   4     status     i32，负数表示错误码
16   4     flags      见 FLAG_*
20   4     cmdLen     请求的业务 cmd（UTF-8），如 MessageSvc.PbSendMsg
24   4     msgLen     uin 字符串 / 错误文案（UTF-8）
28   4     bodyLen    protobuf 裸包
32   8     value0     uin 数值 / 包序号
40   ...   cmd + msg + body
```

一个连接上帧是连续流，没有分隔符，所以必须用 `FrameReader` 增量喂——
半包、粘包、跨 chunk 的头部都要能处理。长度超限直接拒绝，避免被畸形帧撑爆内存。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable

#: "QHP1" —— 帧头魔数，猜错一位整条流就废了
MAGIC = 0x31504851
VERSION = 1

#: 帧头长度
HEADER_SIZE = 40
_HEADER = struct.Struct("<IHHIiIIIIQ")

#: 三类负载的上限：cmd 很短，msg 是 uin/报错，body 才是 protobuf
MAX_CMD_BYTES = 4096
MAX_MSG_BYTES = 64 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024

# ---- op ----
OP_HELLO = 1
OP_REQUEST = 2
OP_ACK = 3
OP_REPLY = 4
OP_ERROR = 5
OP_PACKET = 6
OP_LOGIN_STATE = 7
OP_CLIENT_MESSAGE_RECEIPT = 21
OP_CAPABILITY = 22

# ---- flags ----
FLAG_WANT_REPLY = 1 << 0
FLAG_LOGGED_IN = 1 << 2

#: 服务端主动推送消息时用的 cmd（收消息的主通道）
CMD_MSG_PUSH = "trpc.msg.olpush.OlPushService.MsgPush"


class FrameError(ValueError):
    """帧格式错误（魔数/版本不对，或长度超限）。"""


@dataclass(slots=True)
class Frame:
    """一帧。字段名与帧头一一对应，别改名。"""

    op: int = 0
    request_id: int = 0
    status: int = 0
    flags: int = 0
    value0: int = 0
    cmd: str = ""
    msg: str = ""
    body: bytes = b""

    # ---------------- 语义糖 ----------------
    @property
    def ok(self) -> bool:
        return self.status == 0

    @property
    def logged_in(self) -> bool:
        """登录态帧的判据：flag 置位或 status 非 0，任一成立即视为已登录。"""
        return bool(self.flags & FLAG_LOGGED_IN) or self.status != 0

    @property
    def uin(self) -> str:
        """登录态帧里的 uin：优先用 msg 字符串，退回 value0 的十进制。"""
        return self.msg or (str(self.value0) if self.value0 else "")

    # ---------------- 编解码 ----------------
    def encode(self) -> bytes:
        cmd = self.cmd.encode("utf-8")
        msg = self.msg.encode("utf-8")
        body = self.body if isinstance(self.body, bytes) else bytes(self.body)
        _check_lengths(len(cmd), len(msg), len(body))
        header = _HEADER.pack(
            MAGIC,
            VERSION,
            self.op,
            self.request_id & 0xFFFFFFFF,
            int(self.status),
            self.flags & 0xFFFFFFFF,
            len(cmd),
            len(msg),
            len(body),
            int(self.value0) & 0xFFFFFFFFFFFFFFFF,
        )
        return header + cmd + msg + body

    def __len__(self) -> int:
        return HEADER_SIZE + len(self.cmd.encode()) + len(self.msg.encode()) + len(self.body)


def _check_lengths(cmd_len: int, msg_len: int, body_len: int) -> None:
    for name, value, limit in (
        ("cmd", cmd_len, MAX_CMD_BYTES),
        ("msg", msg_len, MAX_MSG_BYTES),
        ("body", body_len, MAX_BODY_BYTES),
    ):
        if value > limit:
            raise FrameError(f"帧 {name} 长度 {value} 超过上限 {limit}")


def hello(pipe_name: str, pid: int, recv: bool = False) -> Frame:
    """服务端 accept 后立刻发的第一帧。"""
    return Frame(op=OP_HELLO, msg=pipe_name, value0=pid, flags=1 if recv else 0)


def login_state(uin: str | int, logged_in: bool = True) -> Frame:
    uin_text = str(uin or "")
    number = int(uin_text) if uin_text.isdigit() else 0
    return Frame(
        op=OP_LOGIN_STATE,
        status=1 if logged_in else 0,
        flags=FLAG_LOGGED_IN if logged_in else 0,
        value0=number,
        msg=uin_text,
    )


def request(cmd: str, body: bytes = b"", request_id: int = 0, want_reply: bool = True) -> Frame:
    return Frame(
        op=OP_REQUEST,
        request_id=request_id,
        flags=FLAG_WANT_REPLY if want_reply else 0,
        cmd=cmd,
        body=body,
    )


def ack(request_id: int) -> Frame:
    return Frame(op=OP_ACK, request_id=request_id)


def reply(request_id: int, body: bytes = b"", status: int = 0, msg: str = "") -> Frame:
    return Frame(op=OP_REPLY, request_id=request_id, status=status, msg=msg, body=body)


def packet(cmd: str, body: bytes, seq: int = 0, uin: str = "", status: int = 0) -> Frame:
    return Frame(op=OP_PACKET, status=status, value0=seq, cmd=cmd, msg=uin, body=body)


def error(request_id: int, status: int, msg: str) -> Frame:
    return Frame(op=OP_ERROR, request_id=request_id, status=status, msg=msg)


def capability(supported: bool) -> Frame:
    return Frame(op=OP_CAPABILITY, status=1 if supported else 0)


class FrameReader:
    """增量帧解析：喂 chunk，吐完整帧。

    只保留「还没凑齐」的那部分，正常流量下内存占用是一帧的大小。
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, chunk: bytes) -> list[Frame]:
        if chunk:
            self._buf += chunk
        frames: list[Frame] = []
        while True:
            if len(self._buf) < HEADER_SIZE:
                break
            magic, version, op, rid, status, flags, cl, ml, bl, v0 = _HEADER.unpack_from(self._buf, 0)
            if magic != MAGIC:
                raise FrameError(f"帧头魔数不对：0x{magic:08x}")
            if version != VERSION:
                raise FrameError(f"不支持的帧版本：{version}")
            _check_lengths(cl, ml, bl)
            total = HEADER_SIZE + cl + ml + bl
            if len(self._buf) < total:
                break
            off = HEADER_SIZE
            cmd = bytes(self._buf[off : off + cl]).decode("utf-8", "replace")
            off += cl
            msg = bytes(self._buf[off : off + ml]).decode("utf-8", "replace")
            off += ml
            body = bytes(self._buf[off : off + bl])
            del self._buf[:total]
            frames.append(
                Frame(
                    op=op,
                    request_id=rid,
                    status=status,
                    flags=flags,
                    value0=v0,
                    cmd=cmd,
                    msg=msg,
                    body=body,
                )
            )
        return frames

    def reset(self) -> None:
        self._buf.clear()

    @property
    def pending(self) -> int:
        return len(self._buf)


def split_frames(data: bytes) -> list[Frame]:
    """一次性解完（测试/回放用，不处理跨包）。"""
    return FrameReader().push(data)


def iter_encoded(frames: Iterable[Frame]) -> bytes:
    return b"".join(f.encode() for f in frames)
