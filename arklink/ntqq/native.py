"""QQ NT 原生协议通道：扫码登录 → 只读花名册 → 收推送。

能力边界（写清楚，避免误用）：

| 环节 | 状态 | 说明 |
|------|------|------|
| ECDH / TEA / code2d / wtlogin 头 | ✅ 已实现且有单测 | 字节级可对拍 |
| `0x31` 申请二维码 | ✅ | 出 URL / PNG，终端可直接扫 |
| `0x12` 轮询状态 | ✅ | 48/53/0 状态机，终止态会停 |
| `0x13` 换票据 | ✅ | 票据落盘 `keystore.json`（0600） |
| 好友 / 群列表 | ✅ 逻辑完整 | 走 OIDB 信封，`RosterClient` 翻页 |
| 收推送 → OneBot 事件 | ✅ | `push.parse_push` 映射段与撤回 |
| 发消息 | ❌ 故意不做 | 改状态风险高，等只读链路在真机跑顺再说 |

所以挂载后它**默认不健康**（`is_connected` 只在拿到票据后置真），
不会抢占总通道；`send_*` 一律 `1404`，框架会自动换通道，不会把业务打死。

真机使用顺序建议：

```bash
python -m arklink login --qr          # 出二维码，扫完自动存 keystore
python -m arklink roster              # 只读体检：好友数 / 群数
```
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from ..base import LinkAdapter, api_error
from . import code2d, ecdh, ntframe, push, roster, tea
from .session import Keystore, QrLoginSession
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
        self._replies_callback(data)

    def _replies_callback(self, data: bytes) -> None:
        """子类/测试可以在这里按请求生成应答（假服务端就是这么做的）。"""

    async def recv(self) -> bytes:
        return await self._queue.get()

    async def close(self) -> None:
        return None


class TcpTransport(SSOTransport):
    """最小 TCP 传输：`[4B 长度][数据]` 一问一答。

    QQ 的 msfwifi 入口就是这种「长度前缀 + 裸包」形态；不同版本在包外可能还有
    一层握手/加密，所以这里只做最朴素的一种，连不上或对不上时错误会直接冒出来，
    不会假装成功。
    """

    def __init__(self, host: str = "msfwifi.3g.qq.com", port: int = 8080, timeout: float = 15.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        logger.info("已连接 %s:%d", self.host, self.port)

    async def send(self, data: bytes) -> None:
        if self._writer is None:
            raise RuntimeError("TCP 未连接")
        self._writer.write(len(data).to_bytes(4, "big") + data)
        await self._writer.drain()

    async def recv(self) -> bytes:
        if self._reader is None:
            raise RuntimeError("TCP 未连接")
        head = await asyncio.wait_for(self._reader.readexactly(4), self.timeout)
        size = int.from_bytes(head, "big")
        return await asyncio.wait_for(self._reader.readexactly(size), self.timeout)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # pragma: no cover - 关连接异常无关紧要
                pass
        self._reader = self._writer = None


class NativeQQAdapter(LinkAdapter):
    """QQ NT 原生协议通道。

    bot.toml：

        [[adapters]]
        type = "ntqq-native"
        qr_login = true                  # 首次要扫码
        keystore_path = "data/ntqq.json" # 票据落盘，第二次直接复用
        read_only = true                 # 只读：不发消息
    """

    name = "ntqq-native"
    max_message_length = 3000
    #: 只读模式下不发消息，也没必要声称能收（收推送是另开的循环）
    supports_receive = False

    def __init__(
        self,
        uin: int = 0,
        signer_url: str = "",
        qua: str = "",
        session_key: bytes = b"",
        transport: SSOTransport | None = None,
        allow_login: bool = False,
        tea_rounds: int = 16,
        qr_login: bool = False,
        keystore_path: str = "",
        read_only: bool = True,
        guid: bytes = b"",
        app_id: int = code2d.APP_ID,
        qr_out: str = "",
        poll_interval: float = 2.0,
        timeout: float = 180.0,
    ) -> None:
        super().__init__()
        self.uin = int(uin)
        self.signer_url = signer_url
        self.qua = qua
        self.session_key = session_key or b""
        self.transport = transport
        self.allow_login = allow_login
        self.tea_rounds = tea_rounds
        self.qr_login = qr_login
        self.read_only = read_only
        self.qr_out = qr_out
        self.keystore = Keystore(uin=self.uin, guid=guid or b"\x00" * 16)
        self.session = QrLoginSession(
            self,
            app_id=app_id,
            guid=self.keystore.guid,
            keystore_path=keystore_path,
            poll_interval=poll_interval,
            timeout=timeout,
            uin=self.uin,
        )
        self.keystore_path = keystore_path
        self._seq = 0
        self._recv_task: asyncio.Task[Any] | None = None
        self._roster: dict[str, Any] | None = None

    # ---------------- 字节级管线 ----------------
    def next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0x7FFFFFFF or 1
        return self._seq

    def build_packet(self, cmd: int, body: bytes, seq: int | None = None) -> bytes:
        """古典 SSO 封包（数字 cmd），用于自定义/历史业务。"""
        payload = tea.encrypt_packet(body, self.session_key, rounds=self.tea_rounds) if self.session_key else body
        return SSOPacket(seq=seq or self.next_seq(), cmd=cmd, body=payload, uin=self.uin).encode()

    def parse(self, buf: bytes) -> list[SSOPacket]:
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

    # ---------------- 传输适配 ----------------
    async def wtlogin(self, payload: bytes) -> bytes:
        """`QrLoginSession` 的传输实现：发一包，收一包。"""
        if self.transport is None:
            raise RuntimeError("没有配置 transport（真机需要实现 connect/send/recv）")
        await self.transport.send(payload)
        return await self.transport.recv()

    @property
    def d2_key(self) -> bytes:
        """业务请求用的会话密钥。D2 → MD5 是社区通行做法，真机若 401 再校准。"""
        d2 = self.keystore.d2
        return hashlib.md5(d2).digest() if d2 else b""

    async def call_service(self, service: str, body: bytes) -> bytes:
        """按 NT 帧发一个 OIDB 请求，返回响应 payload。"""
        if self.transport is None:
            raise RuntimeError("没有配置 transport")
        frame = ntframe.pack(
            body,
            service=service,
            uin=self.keystore.uin or self.uin,
            seq=self.next_seq(),
            key=self.d2_key,
        )
        await self.transport.send(frame)
        response = await self.transport.recv()
        return ntframe.unpack(response, self.d2_key).payload

    # ---------------- 登录 ----------------
    async def login(self, on_qr: Any = None) -> Keystore:
        """扫码登录并落盘。`on_qr` 收到 `QrCode`（含 url / png()）。"""

        async def default_on_qr(qr: code2d.QrCode) -> None:
            image = qr.png()
            if image and self.qr_out:
                Path(self.qr_out).write_bytes(image)
                logger.info("二维码已保存：%s", self.qr_out)
            logger.info("请扫码登录：%s", qr.url)

        result = await self.session.login(on_qr=on_qr or default_on_qr)
        self.keystore = result
        self.uin = result.uin or self.uin
        self._connected.set()
        logger.info("登录成功：uin=%s 票据 %d 张", self.uin, len(result.tickets))
        return result

    def restore(self, keystore: Keystore) -> None:
        """用落盘票据恢复会话（票据未过期时免扫码）。"""
        self.keystore = keystore
        self.uin = keystore.uin or self.uin
        self.session.restore(keystore)
        self._connected.set()

    # ---------------- 只读 ----------------
    async def read_roster(self, *, force: bool = False) -> dict[str, Any]:
        """拉好友 + 群。这是「登录是否真的成功」的判据。"""
        if self._roster is not None and not force:
            return self._roster
        client = roster.RosterClient(call=self.call_service)
        self._roster = await client.overview()
        logger.info(
            "只读体检通过：好友 %d，群 %d",
            self._roster["friend_count"],
            self._roster["group_count"],
        )
        return self._roster

    # ---------------- 收推送 ----------------
    async def run_push(self) -> None:
        """订阅推送：收到 `MsgPush` 就翻成 OneBot 事件交给框架。"""
        if self.transport is None:
            raise RuntimeError("没有配置 transport")
        while self._running:
            try:
                raw = await self.transport.recv()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("推送接收失败，2s 后重试")
                await asyncio.sleep(2)
                continue
            self._consume(raw)

    def _consume(self, raw: bytes) -> None:
        try:
            frame = ntframe.unpack(raw, self.d2_key)
        except ValueError as e:
            logger.debug("非 NT 帧（可能是古典 SSO）：%s", e)
            for packet in self.parse(raw):
                self._emit_push_body(packet.body)
            return
        self._emit_push_body(frame.payload)

    def _emit_push_body(self, body: bytes) -> None:
        if not body:
            return
        try:
            event = push.to_onebot(body, self_id=self.keystore.uin or self.uin)
        except Exception:
            logger.exception("推送解析失败（%dB），保留原始字节", len(body))
            return
        if event is None:
            return
        logger.debug("推送事件：%s", push.decode_segments(event) or event.get("notice_type"))
        asyncio.get_running_loop().create_task(self._emit_dict(event))

    # ---------------- 生命周期 ----------------
    async def connect(self) -> None:
        if self.transport is not None:
            await self.transport.connect()
        if self.keystore_path and Path(self.keystore_path).is_file():
            self.restore(Keystore.load(self.keystore_path))
            logger.info("已从 %s 恢复票据", self.keystore_path)
        if self.qr_login and not self._connected.is_set():
            await self.login()
        if not self._connected.is_set():
            logger.warning(
                "原生协议通道没有登录态：只作为「研究槽位」挂载，call_api 返回 1404，"
                "框架会自动降级到其他健康通道。开启 qr_login=true 可扫码登录。"
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
        # 只读通道：能查的花名册就查，其余一律让框架换通道
        if action == "get_friend_list":
            data = await self.read_roster()
            return data["friends"]
        if action == "get_group_list":
            data = await self.read_roster()
            return data["groups"]
        if action in ("get_login_info", "get_status"):
            return {"user_id": self.keystore.uin or self.uin, "nickname": "", "online": self.is_connected}
        raise api_error(action, 1404, f"原生协议通道不实现 {action}（只读模式）")

    # ---------------- 研究工具 ----------------
    async def probe(self, cmd: int, body: bytes = b"") -> list[SSOPacket]:
        """发一个古典包并把回包解出来，抓包对拍用。"""
        if self.transport is None:
            raise RuntimeError("没有配置 transport")
        await self.transport.connect()
        await self.transport.send(self.build_packet(cmd, body))
        return self.parse(await self.transport.recv())

    @classmethod
    def selftest(cls, uin: int = 10001, key: bytes = b"0123456789abcdef") -> dict[str, Any]:
        """不联网验证整条字节管线（CI 用）。"""
        adapter = cls(uin=uin, session_key=key)
        body = b"\x08\x96\x01hello"
        wire = adapter.build_packet(0x1234, body, seq=7)
        packets = adapter.parse(wire)
        packet = packets[0]
        ecdh_result = ecdh.selftest()
        return {
            "wire_len": len(wire),
            "seq": packet.seq,
            "cmd": packet.cmd,
            "body_ok": packet.body == body,
            "uin": packet.uin,
            "ntframe": ntframe.selftest(),
            "ecdh": ecdh_result,
        }


def create(**cfg: Any) -> NativeQQAdapter:
    """entry point 工厂：`ntqq-native = "arklink.ntqq.native:create"`。"""
    return NativeQQAdapter(**cfg)
