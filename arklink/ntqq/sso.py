"""QQ NT 的 SSO 封包（TCP/WS 上跑的那层）。

逆向资料里对「NT 封包」的描述基本一致，结构如下（大端）：

    offset  size  field
    0       1     magic      0x02
    1       4     length     从 seq 起算、到包尾 0x03 为止的长度（不含 magic 与自身）
    5       4     seq        客户端自增序号，响应包会回填同一个值
    9       4     cmd        指令号（如登录/心跳/发消息各有一条）
    13      2     version    0x0001
    15      4     uin        登录后填自己 QQ 号，登录前为 0
    19      N     body       业务包体（多为 TEA 加密后的 protobuf）
    19+N    1     tail       0x03

注意：NT 还存在一种带 `uid` 字符串的变体（登录后使用），本实现覆盖
「无 uid / 有 uid」两种，`uid` 为空时走上面的经典布局。

⚠️ 这里只做编解码，不做登录握手。指令号必须来自抓包，
不要相信任何「网上抄来的 cmd 表」——它们随客户端版本变化。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = 0x02
TAIL = 0x03
DEFAULT_VERSION = 0x0001

#: 指令号 → 名字，按抓包逐步补充（例：CAPTURED[0x1234] = "wtlogin.login"）
KNOWN_CMDS: dict[int, str] = {}


def register_cmd(cmd: int, name: str) -> None:
    KNOWN_CMDS[int(cmd)] = name


def cmd_name(cmd: int) -> str:
    return KNOWN_CMDS.get(int(cmd), f"0x{int(cmd):08x}")


@dataclass
class SSOPacket:
    seq: int
    cmd: int
    body: bytes = b""
    uin: int = 0
    version: int = DEFAULT_VERSION
    uid: str = ""
    #: 解析时保留原始字节，方便对拍
    raw: bytes = field(default=b"", repr=False)

    def encode(self) -> bytes:
        """序列化为可发送字节串。"""
        body = bytes(self.body or b"")
        uid_part = b""
        if self.uid:
            uid_bytes = self.uid.encode("utf-8")
            uid_part = bytes([len(uid_bytes)]) + uid_bytes
        # length 从 seq 开始，含 uid 段、body 与结尾 0x03
        length = 4 + 4 + 2 + 4 + len(uid_part) + len(body) + 1
        out = bytearray([MAGIC])
        out += struct.pack(">I", length)
        out += struct.pack(">IIH", self.seq, self.cmd, self.version)
        out += struct.pack(">I", self.uin)
        out += uid_part
        out += body
        out.append(TAIL)
        return bytes(out)

    @classmethod
    def decode(cls, buf: bytes) -> tuple["SSOPacket", bytes]:
        """从字节流里解出一个包，返回 (packet, 剩余字节)。"""
        if len(buf) < 19:
            raise ValueError("数据不足一个包头（<19B）")
        if buf[0] != MAGIC:
            raise ValueError(f"包 magic 不对: 0x{buf[0]:02x}")
        length = struct.unpack(">I", buf[1:5])[0]
        if length < 15:
            raise ValueError(f"包长度字段异常: {length}")
        total = 1 + 4 + length
        if len(buf) < total:
            raise ValueError(f"包不完整：需要 {total}B，实际 {len(buf)}B")
        seq, cmd, version = struct.unpack(">IIH", buf[5:15])
        uin = struct.unpack(">I", buf[15:19])[0]
        cursor = 19
        # length 校验：结尾必须是 0x03
        if buf[total - 1] != TAIL:
            raise ValueError(f"包尾不是 0x03: 0x{buf[total - 1]:02x}")
        uid = ""
        body_start = cursor
        # 有 uid 的变体：body 之前有一个长度前缀的字符串，且长度对不上经典布局
        body_len = (total - 1) - cursor
        if body_len > 1:
            uid_len = buf[cursor]
            expect = cursor + 1 + uid_len
            if 0 < uid_len < 64 and expect <= total - 1:
                candidate = buf[cursor + 1:expect]
                if all(32 <= b < 127 or b == 45 for b in candidate):
                    uid = candidate.decode("utf-8", "replace")
                    body_start = expect
        packet = cls(
            seq=seq,
            cmd=cmd,
            body=buf[body_start:total - 1],
            uin=uin,
            version=version,
            uid=uid,
            raw=buf[:total],
        )
        return packet, buf[total:]

    def __str__(self) -> str:  # pragma: no cover - 日志
        return (
            f"SSOPacket(seq={self.seq} cmd={cmd_name(self.cmd)} uin={self.uin} "
            f"body={len(self.body)}B uid={self.uid!r})"
        )


def iter_packets(buf: bytes):
    """从一个粘连的字节流里连续解包（TCP 粘包处理）。"""
    while len(buf) >= 19:
        packet, buf = SSOPacket.decode(buf)
        yield packet
