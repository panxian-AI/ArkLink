"""secp192k1 上的 ECDH —— `wtlogin.trans_emp` 的密钥协商。

QQ 的 wtlogin 用 secp192k1（不是 NIST 的 P-192，两条曲线名字很像但参数不同），
协商流程是：

1. 客户端生成一对临时密钥，把**压缩公钥**（33 字节，`02/03 || X`）放进请求头；
2. 服务端把它的临时公钥塞在响应的 `encryptType == 4` 分支里；
3 双方用共享点的 **X 坐标** 做 MD5，取 16 字节当 TEA 会话密钥。

第 3 步容易写错：不是直接拿 x 当密钥，也不是 SHA1。这里按「x 定长 24 字节 → MD5 →
16 字节」实现，`shared_key()` 就是那个 `_shareKey`。

纯 Python 实现，够慢但够准：一次标量乘约几毫秒，登录只做两三次，不影响使用。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

#: secp192k1（Tencent 内部叫 Secp192K1）
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFEE37
A = 0
B = 3
GX = 0xDB4FF10EC057E9AE26B07D0280B7F4341DA5D1B1EAE06C7D
GY = 0x9B2F2F6D9C5628A7844163D015BE86344082AA88D95E2F9D
N = 0xFFFFFFFFFFFFFFFFFFFFFFFE26F2FC170F69466A74DEFD8D

#: 坐标定长（字节）
SIZE = 24


@dataclass(frozen=True)
class Point:
    x: int
    y: int
    infinity: bool = False


INFINITY = Point(0, 0, True)
G = Point(GX, GY)


def _inv(value: int) -> int:
    return pow(value, P - 2, P)


def is_on_curve(point: Point) -> bool:
    if point.infinity:
        return True
    return (point.y * point.y - (point.x * point.x * point.x + A * point.x + B)) % P == 0


def add(p: Point, q: Point) -> Point:
    if p.infinity:
        return q
    if q.infinity:
        return p
    if p.x == q.x and (p.y + q.y) % P == 0:
        return INFINITY
    if p == q:
        slope = (3 * p.x * p.x + A) * _inv(2 * p.y) % P
    else:
        slope = (q.y - p.y) * _inv(q.x - p.x) % P
    x = (slope * slope - p.x - q.x) % P
    y = (slope * (p.x - x) - p.y) % P
    return Point(x, y)


def mul(k: int, point: Point = G) -> Point:
    result = INFINITY
    addend = point
    while k:
        if k & 1:
            result = add(result, addend)
        addend = add(addend, addend)
        k >>= 1
    return result


def _fixed(value: int, size: int = SIZE) -> bytes:
    return value.to_bytes(size, "big")


def pack_public(point: Point, compress: bool = True) -> bytes:
    """公钥打包：压缩 = `02/03 || X`（33B），非压缩 = `04 || X || Y`（49B）。"""
    if compress:
        prefix = 0x03 if point.y & 1 else 0x02
        return bytes([prefix]) + _fixed(point.x)
    return b"\x04" + _fixed(point.x) + _fixed(point.y)


def unpack_public(data: bytes) -> Point:
    if not data:
        raise ValueError("公钥为空")
    if data[0] == 0x04:
        if len(data) < 1 + SIZE * 2:
            raise ValueError("非压缩公钥长度不足")
        point = Point(int.from_bytes(data[1:1 + SIZE], "big"), int.from_bytes(data[1 + SIZE:1 + SIZE * 2], "big"))
    elif data[0] in (0x02, 0x03):
        if len(data) < 1 + SIZE:
            raise ValueError("压缩公钥长度不足")
        x = int.from_bytes(data[1:1 + SIZE], "big")
        # y² = x³ + 3  → y = ±sqrt，按前缀奇偶取一支
        y = pow((pow(x, 3, P) + A * x + B) % P, (P + 1) // 4, P)
        if y & 1 != (data[0] & 1):
            y = P - y
        point = Point(x, y)
    else:
        raise ValueError(f"未知公钥前缀 0x{data[0]:02x}")
    if not is_on_curve(point):
        raise ValueError("公钥不在曲线上")
    return point


@dataclass
class KeyPair:
    secret: int
    public: Point

    def pack(self, compress: bool = True) -> bytes:
        return pack_public(self.public, compress=compress)

    def exchange(self, peer_public: bytes) -> bytes:
        """与本端私钥算出 16 字节会话密钥（MD5(X)）。"""
        return shared_key(self.secret, peer_public)


def generate(secret: int | None = None) -> KeyPair:
    if secret is None:
        secret = secrets.randbelow(N - 1) + 1
    secret %= N
    if secret == 0:
        raise ValueError("私钥不能为 0")
    return KeyPair(secret=secret, public=mul(secret))


def shared_key(secret: int, peer_public: bytes) -> bytes:
    """共享点 X 坐标 → MD5 → 16 字节。这是 `Keystore.Secp192K1.KeyExchange(pub, true)`。"""
    point = mul(secret, unpack_public(peer_public))
    if point.infinity:
        raise ValueError("共享点为无穷远（对端公钥非法）")
    return hashlib.md5(_fixed(point.x)).digest()


def selftest() -> dict[str, object]:
    """不联网自检：曲线自洽 + 双方协商一致 + 标量乘与加法自洽。"""
    alice = generate()
    bob = generate()
    same = alice.exchange(bob.pack()) == bob.exchange(alice.pack())
    # (n-1)G + G 应是无穷远，顺带验证 G 的阶
    order_ok = add(mul(N - 1), G).infinity
    # 2G == G+G，且 2G 在曲线上
    double_ok = mul(2) == add(G, G) and is_on_curve(mul(2))
    return {
        "on_curve": is_on_curve(G),
        "shared_equal": same,
        "key_len": len(alice.exchange(bob.pack())),
        "order_ok": order_ok,
        "double_ok": double_ok,
    }
