"""QQ NT 原生协议通道（研究脚手架，默认不登录）。

能力边界说清楚：

* **已实现（有单测）**：SSO 封包编解码、TEA 加解密、protobuf 线格式、
  签名服务对接、发送/接收管线的字节级拼装与拆解。
* **未实现（需要继续逆向 + 抓包对拍）**：
  1. 登录握手（`wtlogin.trans_emp` → `wtlogin.login` 的二维码/密码流程）；
  2. 设备指纹（`device.json` 里那一堆 android_id/guid/qua 的生成规则）；
  3. 会话密钥协商（ECDH）后的 keystream 切换时机；
  4. 各业务 cmd 的 protobuf schema。

所以本适配器**挂载后永远是「不健康」状态**，不会抢占总通道；它的价值是：
  - 让框架侧提前具备「原生协议」这个槽位，`call_api` 直接返回 1404，
    Bot 会自动降级到其他健康通道，不会把业务打死；
  - 提供一个可复现的字节级入口，抓到新包就能直接喂进 `parse()` 看结构。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..base import LinkAdapter, api_error
from . import tea
from .sso import SSOPacket, cmd_name

logger = logging.getLogger("arklink.ntqq")


class SSOTransport:
    """传输层抽象：TCP / WebSocket / 抓包回放都实现这四个方法。"""

    async def connect(self) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def send(self, data: bytes) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def recv(self) -> bytes:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - 抽象
        return None


class LoopbackTransport(SSOTransport):
    """回环传输：发出的包留在 `sent` 里，收到的是预置的回复。

    用来在没有真实协议端的情况下验证「拼包 → 解包」整条管线，
    也可以把线上抓到的字节直接回放。
    """

    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.sent: list[bytes] = []
        self._replies = list(replies or [])
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def connect(self) -> None:
        for reply in self._replies:
            self._queue.put_nowait(reply)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes:
        return await self._queue.get()

    async def close(self) -> None:
        return None


class NativeQQAdapter(LinkAdapter):
    """QQ NT 原生协议通道（研究态）。

    bot.toml（默认关闭，说明见 docs/协议逆向分析.md）：
        [[adapters]]
        type = "ntqq-native"
        uin = 10001
        signer_url = "http://127.0.0.1:8080"
        allow_login = false
    """

    name = "ntqq-native"
    max_message_length = 3000

    def __init__(
        self,
        uin: int = 0,
        signer_url: str = "",
        qua: str = "",
        session_key: bytes = b"",
        transport: SSOTransport | None = None,
        allow_login: bool = False,
        tea_rounds: int = 16,
    ) -> None:
        super().__init__()
        self.uin = int(uin)
        self.signer_url = signer_url
        self.qua = qua
        self.session_key = session_key or b""
        self.transport = transport
        self.allow_login = allow_login
        self.tea_rounds = tea_rounds
        self._seq = 0
        self._recv_task: asyncio.Task[Any] | None = None

    # ---------------- 字节级管线 ----------------
    def next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF or 1
        return self._seq

    def build_packet(self, cmd: int, body: bytes, seq: int | None = None) -> bytes:
        """业务包体 → 可发送字节串（有会话密钥时先加密）。"""
        payload = tea.encrypt_packet(body, self.session_key, rounds=self.tea_rounds) if self.session_key else body
        return SSOPacket(seq=seq or self.next_seq(), cmd=cmd, body=payload, uin=self.uin).encode()

    def parse(self, buf: bytes) -> list[SSOPacket]:
        """字节流 → 包列表（能解就解密 body，解不开保留密文）。"""
        packets: list[SSOPacket] = []
        rest = buf
        while len(rest) >= 19:
            try:
                packet, rest = SSOPacket.decode(rest)
            except ValueError as e:
                logger.debug("解包停止: %s", e)
                break
            if self.session_key and packet.body:
                try:
                    packet.body = tea.decrypt_packet(packet.body, self.session_key, rounds=self.tea_rounds)
                except ValueError:
                    logger.debug("包 %s 的 body 解密失败，保留密文", cmd_name(packet.cmd))
            packets.append(packet)
        return packets

    # ---------------- 生命周期 ----------------
    async def connect(self) -> None:
        if not self.allow_login:
            logger.warning(
                "原生协议通道处于研究态（未启用登录握手）。挂载是安全的：call_api 返回 1404，"
                "框架会自动降级到其他健康通道。实现清单见 docs/协议逆向分析.md"
            )
        else:
            logger.warning(
                "allow_login=true，但登录握手尚未实现：需要 wtlogin 流程 + 设备指纹 + qsign 签名。"
                "当前仍以研究态运行。"
            )
        while self._running:
            await asyncio.sleep(5)

    async def disconnect(self) -> None:
        self._connected.clear()
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        if self.transport is not None:
            await self.transport.close()

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        raise api_error(action, 1404, f"原生协议通道未实现 {action}（研究态）")

    # ---------------- 研究工具 ----------------
    async def probe(self, cmd: int, body: bytes = b"") -> list[SSOPacket]:
        """发一个包并把回包解出来，抓包对拍用。"""
        if self.transport is None:
            raise RuntimeError("没有配置 transport")
        await self.transport.connect()
        await self.transport.send(self.build_packet(cmd, body))
        raw = await self.transport.recv()
        return self.parse(raw)

    @classmethod
    def selftest(cls, uin: int = 10001, key: bytes = b"0123456789abcdef") -> dict[str, Any]:
        """不联网验证整条字节管线（CI 用）。"""
        adapter = cls(uin=uin, session_key=key)
        body = b"\x08\x96\x01hello"
        wire = adapter.build_packet(0x1234, body, seq=7)
        packets = adapter.parse(wire)
        packet = packets[0]
        return {
            "wire_len": len(wire),
            "seq": packet.seq,
            "cmd": packet.cmd,
            "body_ok": packet.body == body,
            "uin": packet.uin,
        }


def create(**cfg: Any) -> NativeQQAdapter:
    """entry point 工厂：`ntqq-native = "arklink.ntqq.native:create"`。"""
    return NativeQQAdapter(**cfg)
