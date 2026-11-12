"""`wtlogin.trans_emp` —— 二维码登录的封包层（扫码 → 轮询 → 换票据）。

QQ 的扫码登录分三条子命令，全部跑在 `wtlogin` 的 code2d 容器里：

| 子命令 | 用途 | 返回 |
|--------|------|------|
| `0x31` | 申请二维码 | `QrUrl`、`QrSig`，以及后续轮询要带的签名 |
| `0x12` | 轮询扫码状态 | 状态码（48 待扫 / 53 待确认 / 0 已确认） |
| `0x13` | 用确认后的 code 换票据 | A2/D2/tgtgt 等 keystore |

三层封装（由内到外）：

```
code2d 容器   ts(4) | 02 | len(2) | cmd(2) | 21×00 | 03 | 00 00 | 32 00 | seq(4) | uin(8) | tlv | 03
d0 包装       00 | len(2) | appid(4) | role(4) | uSt(len2) | rollback(len1) | 上面那坨
wtlogin 头    02 | len(2) | 8001 | 812 | seq(2) | uin(4) | 03 | method | 0(4) | 02 | insId(2) | ver(2) | 0(4)
              01 01 | randomKey(16) | 0102 | 压缩公钥(len2) | TEA(上面那坨) | 03
```

⚠️ 长度屏障（`len`）里那个「+1」在不同实现里差一字节，是最容易对不上的地方。
`build_*` 全部留了 `length_extra` 参数，配 `hexdump()` 抓一次包即可校准；
`tests/test_ntqq_code2d.py` 只断言「自己写进去的偏移能原样读回来」，不去猜服务端行为。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import tea

#: Android 手机 QQ 的 appid / 子 appid / 版本号（Lagrange 用的同一套）
APP_ID = 1600001615
SUB_APP_ID = 537345891
APP_CLIENT_VERSION = 46494

CMD_FETCH_QR = 0x31
CMD_QUERY_QR = 0x12
CMD_VERIFY = 0x13
CMD_CANCEL = 0x14
CMD_CLOSE = 0x16

#: 扫码状态码（服务端返回的 retCode）
QR_STATES: dict[int, str] = {
    0: "confirmed",        # 已确认，可以换票据
    17: "expired",         # 二维码过期
    48: "waiting_scan",    # 待扫描
    53: "waiting_confirm", # 已扫码，待确认
    54: "canceled",        # 用户取消
    144: "invalid",        # 票据无效
}

#: 终态：轮询可以停
FINAL_STATES = {"confirmed", "expired", "canceled", "invalid"}


# ------------------------------------------------------------------ TLV

def tlv(tag: int, value: bytes = b"") -> bytes:
    return struct.pack(">HH", tag, len(value)) + value


def pack_tlvs(items: Iterable[tuple[int, bytes]]) -> bytes:
    """按 `[tag(2) len(2) data]` 排列，前面不带 count（由调用方决定）。"""
    return b"".join(tlv(tag, value) for tag, value in items)


def unpack_tlvs(buf: bytes, count: int | None = None) -> dict[int, bytes]:
    """读 TLV 块。

    `count=None` 时按「前两字节是数量」读；但日志类/局部响应经常没有数量前缀，
    这时把它当裸流一直读到结尾（数量当成 0 或大得离谱都走这条路），
    抓包时宁可多读一个字段，也别把整段丢掉。
    """
    out: dict[int, bytes] = {}
    pos = 0
    if count is None:
        if len(buf) < 2:
            return out
        count = struct.unpack(">H", buf[:2])[0]
        pos = 2
        if count == 0 or count > 64:
            count, pos = len(buf) // 4, 0  # 裸流：按最坏情况给个上限，循环里会自然停
    for _ in range(count):
        if pos + 4 > len(buf):
            break
        tag, length = struct.unpack(">HH", buf[pos:pos + 4])
        pos += 4
        if pos + length > len(buf):
            out[tag] = buf[pos:]
            break
        out[tag] = buf[pos:pos + length]
        pos += length
    return out


# ------------------------------------------------------------------ 内层：code2d 容器

def build_code2d(
    command: int,
    payload: bytes = b"",
    *,
    uin: int = 0,
    seq: int = 0,
    timestamp: int | None = None,
    length_extra: int = 1,
) -> bytes:
    """拼 code2d 容器（不做加密，交给外层 wtlogin 头）。"""
    body = bytearray()
    body += struct.pack(">I", int(timestamp if timestamp is not None else time.time()))
    body += b"\x02"
    inner = bytearray()
    inner += struct.pack(">H", command)
    inner += b"\x00" * 21          # 客户端信息占位，服务端不校验内容
    inner += b"\x03"               # flag：3 = code_2d
    inner += struct.pack(">H", 0)  # close
    inner += struct.pack(">H", 0x32)   # 协议版本 50
    inner += struct.pack(">i", seq)
    inner += int(uin).to_bytes(8, "big", signed=False)
    inner += payload
    inner += b"\x03"
    body += struct.pack(">H", len(inner) + length_extra) + inner
    return bytes(body)


def wrap_d0(code2d: bytes, *, app_id: int = APP_ID, role: int = 0x72) -> bytes:
    """d0 包装：明文（真正的保密靠外层 TEA + ECDH）。"""
    out = bytearray(b"\x00")
    out += struct.pack(">H", len(code2d))
    out += struct.pack(">I", app_id)
    out += struct.pack(">I", role)
    out += struct.pack(">H", 0)   # uSt 空
    out += b"\x00"                # rollback 空
    out += code2d
    return bytes(out)


# ------------------------------------------------------------------ 外层：wtlogin 头

def build_wtlogin(
    payload: bytes,
    key: bytes,
    *,
    uin: int = 0,
    seq: int = 0,
    app_version: int = APP_CLIENT_VERSION,
    method: int = 0x87,
    public_key: bytes = b"",
    random_key: bytes = b"",
    command: int = 0x812,
    version: int = 8001,
) -> bytes:
    """TEA 加密 payload 并套上 wtlogin 头。

    `key` 是 ECDH 协商出来的 `_shareKey`；`public_key` 是本端压缩公钥（33→25 字节），
    服务端据此算出同一把钥匙。
    """
    cipher = tea.encrypt_packet(payload, key) if payload else b""
    inner = bytearray()
    inner += struct.pack(">H", version)
    inner += struct.pack(">H", command)
    inner += struct.pack(">H", seq)
    inner += struct.pack(">I", uin)
    inner += b"\x03"                       # flag
    inner += bytes([method])               # 0x87 = ECDH + ST
    inner += struct.pack(">i", 0)          # state/retry
    inner += b"\x02"
    inner += struct.pack(">H", 0)          # insId
    inner += struct.pack(">H", app_version)
    inner += struct.pack(">i", 0)          # retryTime
    inner += b"\x01\x01"
    inner += (random_key or b"\x00" * 16)[:16]
    inner += struct.pack(">H", 0x102)      # encrypt type
    inner += struct.pack(">H", len(public_key)) + public_key
    inner += cipher
    inner += b"\x03"
    return b"\x02" + struct.pack(">H", len(inner)) + bytes(inner)


def parse_wtlogin(
    buf: bytes,
    share_key: bytes,
    *,
    secret: int | None = None,
    session_key: bytes = b"",
) -> tuple[int, bytes]:
    """拆 wtlogin 头，返回 `(command, 明文body)`。

    `encryptType` 的三个分支和客户端一致：
    0 → 协商密钥；3 → wt session；4 → 响应里先夹了服务端临时公钥，
    要用本端 `secret` 再算一把（所以要同时传 `secret`）。
    """
    if len(buf) < 17:
        raise ValueError("wtlogin 响应不足一个包头")
    command = struct.unpack(">H", buf[5:7])[0]
    encrypt_type = buf[14]
    payload = buf[16:-1]
    if encrypt_type == 0:
        key = share_key
    elif encrypt_type == 3:
        if not session_key:
            raise ValueError("encryptType=3 需要 wt session key")
        key = session_key
    elif encrypt_type == 4:
        if secret is None:
            raise ValueError("encryptType=4 需要本端 ECDH 私钥才能重算密钥")
        from . import ecdh

        raw = tea.decrypt_packet(payload, share_key)
        size = struct.unpack(">H", raw[:2])[0]
        peer_public = raw[2:2 + size]
        key = ecdh.shared_key(secret, peer_public)
        payload = raw[2 + size:]
    else:
        raise ValueError(f"未知 encryptType: {encrypt_type}")
    plain = tea.decrypt_packet(payload, key) if payload else b""
    return command, plain


def parse_code2d(inner: bytes) -> tuple[int, bytes]:
    """拆 code2d 容器，返回 `(子命令, 业务体)`。

    偏移来自服务端响应的实际布局：`00 | encrypt | len(2) | ? | 头部(3) | cmd(2) | 21×00 …`
    """
    if len(inner) < 5:
        raise ValueError("code2d 响应太短")
    encrypt = inner[1]
    length = struct.unpack(">H", inner[2:4])[0]
    span = inner[5:5 + length] if length else inner[5:]
    if encrypt and span:
        raise ValueError("code2d 响应被 ST 加密，需要 keystore 里的 StKey")
    _header = span[0]
    _len = struct.unpack(">H", span[1:3])[0]
    command = struct.unpack(">H", span[3:5])[0]
    body = span[5 + 21 + 1 + 2 + 2 + 4 + 8:]
    return command, body


# ------------------------------------------------------------------ 请求构造

def _device_tlvs(guid: bytes = b"", app_id: int = APP_ID, sub_app_id: int = SUB_APP_ID) -> bytes:
    """TlvQrCode 的默认组合：0x16 应用信息 + 0x1B/0x1D/0x33/0x35/0x66 设备占位 + 0xD1。"""
    items: list[tuple[int, bytes]] = [
        (0x16, struct.pack(">II", app_id, sub_app_id) + (guid or b"\x00" * 16)),
        (0x1B, b"\x00"),
        (0x1D, b"\x00"),
        (0x33, b"\x00"),
        (0x35, b"\x00"),
        (0x66, b"\x00"),
        (0xD1, b""),
    ]
    return pack_tlvs(items)


def build_fetch_qr(*, uin: int = 0, guid: bytes = b"", ts: int | None = None) -> bytes:
    """0x31：申请二维码。"""
    return build_code2d(CMD_FETCH_QR, _device_tlvs(guid), uin=uin, timestamp=ts)


def build_query_qr(qr_sig: bytes, *, uin: int = 0, app_id: int = APP_ID, ts: int | None = None) -> bytes:
    """0x12：轮询扫码状态（`qr_sig` 就是 0x31 返回的签名）。"""
    payload = bytearray()
    payload += struct.pack(">I", app_id)
    payload += struct.pack(">H", len(qr_sig)) + qr_sig
    payload += int(uin).to_bytes(8, "big")
    payload += struct.pack(">H", 0)     # TGT 空
    payload += b"\x00"
    payload += struct.pack(">H", 0)     # 附加字段空
    payload += struct.pack(">H", 0)     # TLV 数量 0
    return build_code2d(CMD_QUERY_QR, bytes(payload), uin=uin, timestamp=ts)


def build_verify(code: bytes, a2: bytes = b"", *, uin: int, guid: bytes = b"", ts: int | None = None) -> bytes:
    """0x13：扫码确认后用 code 换票据（A2/D2/tgtgt）。"""
    payload = bytearray()
    payload += int(uin).to_bytes(8, "big")
    payload += struct.pack(">H", len(code)) + code
    payload += struct.pack(">H", len(a2)) + a2
    payload += guid or b"\x00" * 16
    payload += b"\x01"
    payload += struct.pack(">h", 1)
    payload += b"\x08"
    tags = (0x03, 0x05, 0x20, 0x35, 0x36)
    payload += struct.pack(">h", len(tags))
    for tag in tags:
        payload += struct.pack(">h", tag)
    payload += pack_tlvs([(0x09, b"com.tencent.qq"), (0x12C, b"\x00"), (0x39, b"\x00")])
    return build_code2d(CMD_VERIFY, bytes(payload), uin=uin, timestamp=ts)


# ------------------------------------------------------------------ 响应解析

@dataclass
class QrCode:
    """0x31 的产物：给人扫的 URL + 轮询要带的签名。"""

    url: str
    sig: bytes
    extra: dict[str, Any] = field(default_factory=dict)

    def png(self) -> bytes | None:
        """装了 `qrcode` 就顺便出图；没装返回 None（终端里直接打印 URL 也能扫）。"""
        try:
            import io

            import qrcode  # type: ignore
        except Exception:
            return None
        image = qrcode.make(self.url)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()


@dataclass
class QrState:
    """0x12 的产物。`name` 见 `QR_STATES`。"""

    code: int
    uin: int = 0
    tickets: dict[int, bytes] = field(default_factory=dict)
    raw: bytes = b""

    @property
    def name(self) -> str:
        return QR_STATES.get(self.code, f"unknown({self.code})")

    @property
    def final(self) -> bool:
        return self.name in FINAL_STATES


def _read_tlv_proto(blob: bytes, field: int) -> str:
    """从 QrExtInfo 里取一个字符串字段（2=QrUrl，3=QrSig）。"""
    from . import pb

    value = pb.decode(blob).get(field, [])
    if not value:
        return ""
    first = value[0]
    return first.decode("utf-8", "replace") if isinstance(first, bytes) else str(first)


def parse_fetch_qr(body: bytes) -> QrCode:
    """解析 0x31 的业务体：`<sig(len2)><tlv块>`，URL 躲在 0xD1 的 QrExtInfo 里。"""
    sig = b""
    pos = 0
    if len(body) >= 2:
        size = struct.unpack(">H", body[0:2])[0]
        if 0 < size <= len(body) - 2:
            sig = body[2:2 + size]
            pos = 2 + size
    tlvs = unpack_tlvs(body[pos:], None)
    url = ""
    if 0xD1 in tlvs:
        url = _read_tlv_proto(tlvs[0xD1], 2)
    if not url and 0x17 in tlvs:
        url = tlvs[0x17].decode("utf-8", "replace").strip("\x00")
    return QrCode(url=url, sig=sig, extra={f"0x{tag:02x}": value for tag, value in tlvs.items()})


def parse_query_qr(body: bytes) -> QrState:
    """解析 0x12 的业务体：`<uin(8)><retry(4)><tlv块>`。"""
    uin = 0
    pos = 0
    if len(body) >= 8:
        uin = int.from_bytes(body[0:8], "big")
        pos = 12 if len(body) >= 12 else 8
    tlvs = unpack_tlvs(body[pos:], None)
    code = 0
    if 0x1E in tlvs and tlvs[0x1E]:
        # 0x1E 是状态/票据混合体，首字节常是状态码；拿不到就交给调用方按「已确认」处理
        code = tlvs[0x1E][0] if tlvs[0x1E][0] in QR_STATES else 0
    return QrState(code=code, uin=uin, tickets=tlvs, raw=body)


def parse_verify(body: bytes) -> dict[str, Any]:
    """解析 0x13 的业务体：`<uin(8)><state(1)><ts(4)><platform(len2 str)><tlv块>`。"""
    if len(body) < 9:
        return {"uin": 0, "state": 0, "tlvs": {}}
    uin = int.from_bytes(body[0:8], "big")
    state = body[8]
    pos = 9
    platform = ""
    if state == 0 and len(body) >= pos + 2:
        size = struct.unpack(">H", body[pos:pos + 2])[0]
        platform = body[pos + 2:pos + 2 + size].decode("utf-8", "replace")
        pos = pos + 2 + size
    return {"uin": uin, "state": state, "platform": platform, "tlvs": unpack_tlvs(body[pos:], None)}


def hexdump(data: bytes, width: int = 16) -> str:
    """抓包对拍用：`python -m arklink decode --hex ...` 也可。"""
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hexed = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hexed:<{width * 3}}  {text}")
    return "\n".join(lines)
