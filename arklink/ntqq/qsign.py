"""qSign 签名服务客户端。

QQ NT 的登录与关键业务包都带一个由「协议端」算出的签名字段，社区通行的做法是
把签名外包给本地 qsign 服务（一个把设备指纹 + 包体 交给签名算法的进程）：

    GET  /sign?uin=10001&qua=V1_...&cmd=wtlogin.login&seq=1&buffer=<hex>
    →    {"code": 0, "msg": "", "value": {"sign": "<hex>", "token": "<hex>", "extra": "<hex>"}}

不同分支的路径/字段略有差异，本客户端同时支持 GET 与 POST，
并把 sign/token/extra 全部解码成 bytes，交给调用方决定塞进哪个 protobuf 字段。

⚠️ 本模块不含任何签名算法本身，也不内置任何可用服务地址。
"""

from __future__ import annotations

import binascii
import logging
from dataclasses import dataclass
from typing import Any

from ..transport import HTTPError, apost_json, with_query

logger = logging.getLogger("arklink.qsign")


class QSignError(Exception):
    def __init__(self, code: int, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"qsign 失败 code={code} {message}")


def _hex(value: Any) -> bytes:
    text = str(value or "").strip()
    if not text:
        return b""
    try:
        return binascii.unhexlify(text)
    except (binascii.Error, ValueError):
        logger.debug("qsign 返回的不是 hex，按原始字节处理")
        return text.encode("utf-8", "replace")


@dataclass
class Signature:
    sign: bytes = b""
    token: bytes = b""
    extra: bytes = b""
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, bytes]:
        return {"sign": self.sign, "token": self.token, "extra": self.extra}


class QSignClient:
    """本地 qsign 服务的客户端。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        uin: int = 0,
        qua: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.uin = int(uin)
        self.qua = qua
        self.timeout = timeout

    async def sign(self, cmd: str, seq: int, buffer: bytes, uin: int | None = None) -> Signature:
        """请求一次签名。`cmd` 是字符串指令名（如 `wtlogin.login`）。"""
        params = {
            "uin": uin or self.uin,
            "qua": self.qua,
            "cmd": cmd,
            "seq": seq,
            "buffer": buffer.hex(),
        }
        try:
            resp = await apost_json(
                with_query(f"{self.base_url}/sign", **params), None, None, timeout=self.timeout, method="GET"
            )
        except HTTPError as get_err:
            # 有的实现只给 POST，GET 会 404/405，退回 POST 再试一次
            try:
                resp = await apost_json(f"{self.base_url}/sign", params, None, timeout=self.timeout)
            except HTTPError:
                raise QSignError(-1, f"签名服务不可达: {get_err}") from get_err

        code = int(resp.get("code") or 0)
        if code != 0:
            raise QSignError(code, str(resp.get("msg") or resp.get("message") or ""))
        value = resp.get("value") or {}
        if not isinstance(value, dict):
            raise QSignError(-1, "响应缺少 value 对象")
        return Signature(
            sign=_hex(value.get("sign")),
            token=_hex(value.get("token")),
            extra=_hex(value.get("extra")),
            raw=value,
        )
