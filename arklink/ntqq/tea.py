"""QQ 使用的 TEA 变体。

QQ 全系（含 QQ NT）的封包加密都是 TEA，与教科书 TEA 的差别只有两点：

1. **16 轮**（教科书是 32 轮），delta = 0x9E3779B9；
2. 分组之间做 CBC 式链接（部分实现用 PCBC，见 `mode`）。

本文件只实现可验证的密码学原语：
  - `encrypt_block` / `decrypt_block`：纯 TEA 单分组，可对标准测试向量；
  - `qq_tea_encrypt` / `qq_tea_decrypt`：带填充与链接模式的分组加密。

⚠️ 填充方案各实现略有差异（`qq` / `pkcs7` / `zero` 三种都提供），
真正上线前必须用目标客户端抓到的密文做一次对拍，见
`docs/协议逆向分析.md` §「对拍清单」。
"""

from __future__ import annotations

import struct

DELTA = 0x9E3779B9
MASK = 0xFFFFFFFF


def _words(key: bytes) -> tuple[int, int, int, int]:
    if len(key) < 16:
        key = key.ljust(16, b"\x00")
    return struct.unpack(">4I", key[:16])


def encrypt_block(v0: int, v1: int, key: bytes, rounds: int = 16) -> tuple[int, int]:
    """单分组加密（大端 32 位字序，和 QQ 实现一致）。"""
    k0, k1, k2, k3 = _words(key)
    total = 0
    for _ in range(rounds):
        total = (total + DELTA) & MASK
        v0 = (v0 + ((((v1 << 4) & MASK) + k0) ^ (v1 + total) ^ ((v1 >> 5) + k1))) & MASK
        v1 = (v1 + ((((v0 << 4) & MASK) + k2) ^ (v0 + total) ^ ((v0 >> 5) + k3))) & MASK
    return v0, v1


def decrypt_block(v0: int, v1: int, key: bytes, rounds: int = 16) -> tuple[int, int]:
    k0, k1, k2, k3 = _words(key)
    total = (DELTA * rounds) & MASK
    for _ in range(rounds):
        v1 = (v1 - ((((v0 << 4) & MASK) + k2) ^ (v0 + total) ^ ((v0 >> 5) + k3))) & MASK
        v0 = (v0 - ((((v1 << 4) & MASK) + k0) ^ (v1 + total) ^ ((v1 >> 5) + k1))) & MASK
        total = (total - DELTA) & MASK
    return v0, v1


# ---------------------------------------------------------------- 填充


def _pad(data: bytes, scheme: str) -> bytes:
    if scheme == "zero":
        return data + b"\x00" * ((8 - len(data) % 8) % 8)
    if scheme == "pkcs7":
        n = 8 - len(data) % 8
        return data + bytes([n]) * n
    # QQ 风格：始终补齐（1..8 字节），填充内容 0x00，末字节 = 填充长度。
    # 有的实现填充位是随机数，但解密只按末字节裁剪，不影响兼容。
    n = 8 - len(data) % 8
    return data + b"\x00" * (n - 1) + bytes([n])


def _unpad(data: bytes, scheme: str) -> bytes:
    if scheme == "zero":
        return data.rstrip(b"\x00")
    if scheme == "pkcs7":
        if not data:
            return b""
        n = data[-1]
        return data[:-n] if 0 < n <= 8 else data
    if not data:
        return b""
    # QQ 风格：末字节就是填充长度
    n = data[-1]
    if 1 <= n <= 8 and n <= len(data):
        return data[: len(data) - n]
    return data


# ---------------------------------------------------------------- 分组链接


def qq_tea_encrypt(plain: bytes, key: bytes, rounds: int = 16, mode: str = "cbc", scheme: str = "qq") -> bytes:
    """按 QQ 方式加密：填充 → 分组 → CBC/PCBC 链接。"""
    data = _pad(plain, scheme)
    out = bytearray()
    prev_c = b"\x00" * 8
    prev_p = b"\x00" * 8
    for i in range(0, len(data), 8):
        block = data[i:i + 8]
        if mode == "pcbc":
            mixed = bytes(a ^ b ^ c for a, b, c in zip(block, prev_p, prev_c))
        else:
            mixed = bytes(a ^ b for a, b in zip(block, prev_c))
        v0, v1 = struct.unpack(">2I", mixed)
        v0, v1 = encrypt_block(v0, v1, key, rounds)
        cipher = struct.pack(">2I", v0, v1)
        out += cipher
        prev_c, prev_p = cipher, block
    return bytes(out)


def qq_tea_decrypt(cipher: bytes, key: bytes, rounds: int = 16, mode: str = "cbc", scheme: str = "qq") -> bytes:
    """解密并去填充。"""
    if len(cipher) % 8:
        raise ValueError("密文长度必须是 8 的倍数")
    out = bytearray()
    prev_c = b"\x00" * 8
    prev_p = b"\x00" * 8
    for i in range(0, len(cipher), 8):
        block = cipher[i:i + 8]
        v0, v1 = struct.unpack(">2I", block)
        v0, v1 = decrypt_block(v0, v1, key, rounds)
        mixed = struct.pack(">2I", v0, v1)
        if mode == "pcbc":
            plain = bytes(a ^ b ^ c for a, b, c in zip(mixed, prev_p, prev_c))
        else:
            plain = bytes(a ^ b for a, b in zip(mixed, prev_c))
        out += plain
        prev_c, prev_p = block, plain
    return _unpad(bytes(out), scheme)


def encrypt_packet(body: bytes, key: bytes, rounds: int = 16) -> bytes:
    """便捷入口：登录后所有业务包都要 encrypt。"""
    return qq_tea_encrypt(body, key, rounds=rounds)


def decrypt_packet(body: bytes, key: bytes, rounds: int = 16) -> bytes:
    return qq_tea_decrypt(body, key, rounds=rounds)
