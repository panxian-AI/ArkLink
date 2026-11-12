"""NT 帧（protocol 12 / 13）——把 OIDB 请求真正发出去的那层壳。

和「古典」SSO 封包（`sso.py`，数字 cmd）不同，NT 客户端用的是 **服务名字符串**：

```
帧 = [int32 总长][13][encType][int32 seq][00][int32-len uin(字符串)][密文]

密文 = TEA(payload)
payload = [int32-len head][head][int32-len body][body]
head   = [int32-len "OidbSvcTrpcTcp.0xfd4_1"][int32-len 空][reserved]
```

这解释了为什么「抄 cmd 表」在 NT 上没用：同一个业务只要改名就能换走，
而服务名的字符串本身才是路由键。

`encType`：0 = 不加密，1 = 用 16 字节空密钥（`wtlogin.trans_emp` 用这个），
2 = 用 D2 派生密钥（绝大多数业务）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import tea

PROTOCOL_SSO = 13
PROTOCOL_HEARTBEAT = 12

ENC_NONE = 0
ENC_EMPTY = 1
ENC_D2 = 2

EMPTY_KEY = b"\x00" * 16


def _prefixed(data: bytes) -> bytes:
    """int32 大端长度前缀（NT 里到处都是这个东西）。"""
    return struct.pack(">I", len(data)) + data


def build_head(service: str, *, cookies: bytes = b"", reserved: bytes = b"") -> bytes:
    return _prefixed(service.encode("utf-8")) + _prefixed(cookies) + _prefixed(reserved)


def pack(
    payload: bytes,
    *,
    service: str,
    uin: int | str = 0,
    seq: int = 0,
    key: bytes = b"",
    encrypt_type: int = ENC_D2,
    protocol: int = PROTOCOL_SSO,
    cookies: bytes = b"",
) -> bytes:
    """把业务体封成一帧。`key` 为空时按 `encrypt_type` 取默认密钥。"""
    if encrypt_type == ENC_NONE:
        cipher, actual = payload, ENC_NONE
    elif encrypt_type == ENC_EMPTY:
        cipher, actual = tea.encrypt_packet(payload, EMPTY_KEY), ENC_EMPTY
    else:
        if not key:
            raise ValueError("encType=2 需要 D2 派生的会话密钥")
        cipher, actual = tea.encrypt_packet(payload, key), ENC_D2
    body = _prefixed(build_head(service, cookies=cookies)) + _prefixed(cipher)
    inner = bytearray()
    inner += bytes([protocol, actual])
    inner += struct.pack(">i", seq)
    inner += b"\x00"
    inner += _prefixed(str(uin).encode("utf-8"))
    inner += body
    return _prefixed(bytes(inner))


@dataclass
class Frame:
    seq: int
    payload: bytes
    protocol: int = PROTOCOL_SSO
    encrypt_type: int = ENC_D2
    service: str = ""
    cookies: bytes = b""
    ret_code: int = 0
    extra: str = ""

    @property
    def ok(self) -> bool:
        return self.ret_code == 0


def unpack(frame: bytes, key: bytes = b"") -> Frame:
    """拆一帧（响应方向）。假服务端与回放都用它，所以和 `pack` 严格互逆。"""
    if len(frame) < 4:
        raise ValueError("帧太短")
    total = struct.unpack(">I", frame[:4])[0]
    if total and len(frame) < 4 + total:
        raise ValueError(f"帧不完整：声明 {total}B，实际 {len(frame) - 4}B")
    inner = frame[4:4 + total] if total else frame[4:]
    protocol, encrypt_type = inner[0], inner[1]
    seq = struct.unpack(">i", inner[2:6])[0]
    pos = 7  # 1 protocol + 1 encType + 4 seq + 1 保留
    uin_len = struct.unpack(">I", inner[pos:pos + 4])[0]
    pos += 4 + uin_len
    head_len = struct.unpack(">I", inner[pos:pos + 4])[0]
    head = inner[pos + 4:pos + 4 + head_len]
    pos += 4 + head_len
    body_len = struct.unpack(">I", inner[pos:pos + 4])[0]
    cipher = inner[pos + 4:pos + 4 + body_len]
    if encrypt_type == ENC_EMPTY:
        payload = tea.decrypt_packet(cipher, EMPTY_KEY) if cipher else b""
    elif encrypt_type == ENC_D2:
        if not key:
            raise ValueError("响应是 D2 加密的，需要会话密钥")
        payload = tea.decrypt_packet(cipher, key)
    else:
        payload = cipher
    return Frame(seq=seq, payload=payload, protocol=protocol, encrypt_type=encrypt_type, **_parse_head(head))


def _parse_head(head: bytes) -> dict[str, object]:
    """响应头：`seq(i32) ret(i32) extra(str) cmd(str) cookie(bytes) flag(i32) reserved`。

    请求头是 `service(str) cookie(bytes) reserved(bytes)`，形状不同。用第一段的
    特征区分：请求头的首字段是「服务名长度 + 纯 ASCII」，响应头的首字段是随机
    序号（远大于 128）。这样 `pack` 出来的帧也能被 `unpack` 原样读回来。
    """
    if len(head) >= 5:
        size = struct.unpack(">I", head[:4])[0]
        chunk = head[4:4 + size]
        if 0 < size < 128 and len(chunk) == size and all(32 <= b < 127 for b in chunk):
            try:
                service = chunk.decode("ascii")
            except UnicodeDecodeError:
                service = ""
            pos = 4 + size
            cookies = b""
            if pos + 4 <= len(head):
                size2 = struct.unpack(">I", head[pos:pos + 4])[0]
                cookies = head[pos + 4:pos + 4 + size2]
            return {"service": service, "cookies": cookies, "ret_code": 0, "extra": ""}
    if len(head) < 8:
        return {"ret_code": 0, "extra": "", "service": "", "cookies": b""}
    _seq, ret = struct.unpack(">ii", head[:8])
    pos = 8
    extra = ""
    if pos + 4 <= len(head):
        size = struct.unpack(">I", head[pos:pos + 4])[0]
        extra = head[pos + 4:pos + 4 + size].decode("utf-8", "replace")
        pos += 4 + size
    service = ""
    cookies = b""
    if pos + 4 <= len(head):
        size = struct.unpack(">I", head[pos:pos + 4])[0]
        chunk = head[pos + 4:pos + 4 + size]
        pos += 4 + size
        try:
            service = chunk.decode("utf-8")
        except UnicodeDecodeError:
            service = ""
    if pos + 4 <= len(head):
        size = struct.unpack(">I", head[pos:pos + 4])[0]
        cookies = head[pos + 4:pos + 4 + size]
    return {"ret_code": ret, "extra": extra, "service": service, "cookies": cookies}


def selftest() -> dict[str, object]:
    key = bytes(range(16))
    payload = b"\x08\x01\x12\x03abc"
    frame = pack(payload, service="OidbSvcTrpcTcp.0xfd4_1", uin=10001, seq=7, key=key)
    parsed = unpack(frame, key)
    empty = unpack(pack(payload, service="wtlogin.trans_emp", encrypt_type=ENC_EMPTY))
    return {
        "payload_ok": parsed.payload == payload,
        "service_ok": parsed.service == "OidbSvcTrpcTcp.0xfd4_1",
        "seq_ok": parsed.seq == 7,
        "empty_ok": empty.payload == payload,
        "frame_len": len(frame),
    }
