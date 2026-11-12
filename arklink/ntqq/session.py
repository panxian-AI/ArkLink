"""扫码登录的状态机：`申请二维码 → 轮询 → 换票据 → keystore`。

这一层只做「编排 + 票据落地」，不碰 socket：

```
QrLoginSession
  ├─ fetch()   0x31  → QrCode(url, sig)          ← 打印/生成二维码给人扫
  ├─ poll()    0x12  → QrState(code)             ← 48 待扫 / 53 待确认 / 0 已确认
  ├─ verify()  0x13  → 票据 TLV 合集
  └─ login()   上面三步串起来，并落盘 keystore.json
```

传输由一个 `transport` 提供，唯一要求是 `async wtlogin(payload) -> bytes`
（payload 是 d0 包装后的 code2d 包）。真机实现见 `native.NativeQQAdapter.wtlogin()`，
单测里用 `LoopbackTransport` 应答构造好的字节。

为什么先做只读：扫码能拉出好友/群列表，就说明 ECDH、TEA、票据、OIDB 信封全对；
发消息还要再过一层「A2 校验 + 频率风控」，出问题时更难定位。所以按
`登录 → 只读 → 发消息` 的顺序推进，每一步都有可观测的成功判据。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import code2d, ecdh, tea

logger = logging.getLogger("arklink.ntqq.login")

#: 传统 wtlogin 票据编号里最稳的一条；其余按 hex 名保留原样，不硬猜
TICKET_TGT = 0x106

QrCallback = Callable[[code2d.QrCode], Any]
StateCallback = Callable[[code2d.QrState], Any]


class WtLoginTransport:
    """只有一件事：把 wtlogin 载荷发出去，把原始响应字节拿回来。"""

    async def wtlogin(self, payload: bytes) -> bytes:  # pragma: no cover - 抽象
        raise NotImplementedError


class LoopbackWtLogin(WtLoginTransport):
    """回环 + 脚本化应答：按顺序吐出预置字节，用于单测与抓包回放。"""

    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.sent: list[bytes] = []
        self.replies = list(replies or [])
        self._index = 0

    async def wtlogin(self, payload: bytes) -> bytes:
        self.sent.append(payload)
        if self._index >= len(self.replies):
            raise RuntimeError("回环传输没有更多应答（检查测试脚本）")
        reply = self.replies[self._index]
        self._index += 1
        return reply


# ------------------------------------------------------------------ keystore


@dataclass
class Keystore:
    """登录产物。落盘时所有 bytes 走 base64，方便直接看 JSON。"""

    uin: int = 0
    guid: bytes = b"\x00" * 16
    secret: int = 0
    share_key: bytes = b""
    tickets: dict[int, bytes] = field(default_factory=dict)
    platform: str = ""
    created_at: float = field(default_factory=time.time)

    # ---- 常用票据（取不到返回空） ----
    def ticket(self, tag: int) -> bytes:
        return self.tickets.get(tag, b"")

    @property
    def tgt(self) -> bytes:
        return self.ticket(TICKET_TGT)

    @property
    def a2(self) -> bytes:
        """A2 的编号随版本变，这里给几个常见位置，取到第一个非空即用。"""
        for tag in (0x10A, 0x106, 0x52):
            value = self.tickets.get(tag, b"")
            if value:
                return value
        return b""

    @property
    def d2(self) -> bytes:
        for tag in (0x10D, 0x143, 0x54):
            value = self.tickets.get(tag, b"")
            if value:
                return value
        return b""

    def to_dict(self) -> dict[str, Any]:
        return {
            "uin": self.uin,
            "guid": base64.b64encode(self.guid).decode(),
            "secret": format(self.secret, "x"),
            "share_key": base64.b64encode(self.share_key).decode(),
            "platform": self.platform,
            "created_at": self.created_at,
            "tickets": {f"0x{tag:03x}": base64.b64encode(value).decode() for tag, value in self.tickets.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Keystore":
        return cls(
            uin=int(data.get("uin", 0)),
            guid=base64.b64decode(data.get("guid") or ""),
            secret=int(data.get("secret") or "0", 16),
            share_key=base64.b64decode(data.get("share_key") or ""),
            platform=str(data.get("platform", "")),
            created_at=float(data.get("created_at", 0)),
            tickets={
                int(tag, 16): base64.b64decode(value)
                for tag, value in (data.get("tickets") or {}).items()
            },
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        try:  # 票据等同于密码，能收就收紧
            target.chmod(0o600)
        except OSError:
            pass
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Keystore":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ------------------------------------------------------------------ 会话


class QrLoginSession:
    """二维码登录会话。"""

    def __init__(
        self,
        transport: WtLoginTransport,
        *,
        app_id: int = code2d.APP_ID,
        guid: bytes = b"",
        keystore_path: str | Path = "",
        poll_interval: float = 2.0,
        timeout: float = 180.0,
        uin: int = 0,
    ) -> None:
        self.transport = transport
        self.app_id = app_id
        self.guid = guid or b"\x00" * 16
        self.keystore_path = keystore_path
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.uin = uin
        self.keys = ecdh.generate()
        self.qr: code2d.QrCode | None = None
        self.state: code2d.QrState | None = None
        self.keystore = Keystore(uin=uin, guid=self.guid)

    # ---- 底层收发 ----
    @property
    def share_key(self) -> bytes:
        """本端私钥与固定服务端公钥协商出的 `_shareKey`（16B）。"""
        return self.keys.exchange(SERVER_PUBLIC)

    async def _call(self, payload: bytes) -> bytes:
        packet = code2d.build_wtlogin(
            payload,
            self.share_key,
            uin=self.uin,
            public_key=self.keys.pack(compress=True),
        )
        response = await self.transport.wtlogin(packet)
        _command, plain = code2d.parse_wtlogin(response, self.share_key, secret=self.keys.secret)
        return plain

    # ---- 三步 ----
    async def fetch(self) -> code2d.QrCode:
        inner = code2d.build_fetch_qr(uin=self.uin, guid=self.guid)
        plain = await self._call(code2d.wrap_d0(inner, app_id=self.app_id))
        _sub, body = code2d.parse_code2d(plain)
        self.qr = code2d.parse_fetch_qr(body)
        logger.info("拿到二维码：%s", self.qr.url or "(URL 为空，见 hexdump)")
        return self.qr

    async def poll(self) -> code2d.QrState:
        if self.qr is None:
            raise RuntimeError("还没申请二维码，先 fetch()")
        inner = code2d.build_query_qr(self.qr.sig, uin=self.uin, app_id=self.app_id)
        plain = await self._call(code2d.wrap_d0(inner, app_id=self.app_id))
        _sub, body = code2d.parse_code2d(plain)
        self.state = code2d.parse_query_qr(body)
        self.keystore.tickets.update(self.state.tickets)
        return self.state

    async def verify(self, code: bytes | None = None) -> dict[str, Any]:
        state = self.state or await self.poll()
        k = code or state.tickets.get(0x1E, b"")
        if not k:
            raise RuntimeError("服务端没有给确认 code（TLV 0x1E），无法换票据")
        inner = code2d.build_verify(k, uin=state.uin or self.uin, guid=self.guid)
        plain = await self._call(code2d.wrap_d0(inner, app_id=self.app_id))
        _sub, body = code2d.parse_code2d(plain)
        result = code2d.parse_verify(body)
        if result["uin"]:
            self.keystore.uin = int(result["uin"])
        self.keystore.platform = str(result.get("platform", ""))
        self.keystore.tickets.update(result.get("tlvs", {}))
        if self.keystore_path and self.keystore.tickets:
            self.keystore.save(self.keystore_path)
            logger.info("keystore 已保存：%s", self.keystore_path)
        return result

    # ---- 编排 ----
    async def login(
        self,
        on_qr: QrCallback | None = None,
        on_state: StateCallback | None = None,
    ) -> Keystore:
        """申请 → 轮询 → 换票据。`on_qr` 负责把二维码递给人扫。"""
        qr = await self.fetch()
        if on_qr is not None:
            result = on_qr(qr)
            if asyncio.iscoroutine(result):
                await result
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            state = await self.poll()
            if on_state is not None:
                result = on_state(state)
                if asyncio.iscoroutine(result):
                    await result
            if state.name == "confirmed":
                await self.verify()
                return self.keystore
            if state.final:
                raise RuntimeError(f"二维码登录终止：{state.name}")
            await asyncio.sleep(self.poll_interval)
        raise TimeoutError("等待扫码超时（可以调大 timeout 重新 fetch）")

    # ---- 恢复 ----
    def restore(self, keystore: Keystore) -> None:
        """用已落盘的票据跳过扫码（票据没过期时有效）。"""
        self.keystore = keystore
        self.uin = keystore.uin
        self.guid = keystore.guid or self.guid
        if keystore.secret:
            self.keys = ecdh.generate(keystore.secret)


#: wtlogin 固定的服务端 ECDH 公钥（非压缩 49B，各协议端一致，不是机密）
SERVER_PUBLIC = bytes.fromhex(
    "04928D8850673088B343264E0C6BACB8496D697799F37211DEB25BB73906CB089FEA9639B4E0260498B51A992D50813DA8"
)
