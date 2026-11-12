"""同端口 HTTP + WebSocket 服务端（标准库实现）。

从 ArkBot 的兼容层移植，去掉对框架的依赖，让 ArkLink 可以**单独**扮演
一个 OneBot 11 实现（NapCat 的平替），这样上层 NoneBot2 / Koishi 只改地址。


为什么自己写：OneBot 11 的实现（NapCat 等）习惯把 HTTP API 和正向 WS 放在
**同一个端口**上，而 `websockets` 只处理握手后的帧、不解析 HTTP 请求体。
要做到「一个端口既能被 NoneBot2 连 WS，又能被 curl 打 HTTP」，最省事的是
手写一份最小实现 —— 只支持本项目需要的子集：

- HTTP/1.1：解析请求行 + 头 + Content-Length 体，响应后关闭连接；
- WebSocket（RFC 6455）：握手、文本/二进制帧、分片重组、ping/pong、close。

不追求完整实现，但要求严格到真实框架（websockets / NoneBot2 / Koishi）
能正常握手通信。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("arklink.onebot.ws")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_HEADER_LIMIT = 64 * 1024
_BODY_LIMIT = 32 * 1024 * 1024

# 操作码
_OP_CONT, _OP_TEXT, _OP_BIN = 0x0, 0x1, 0x2
_OP_CLOSE, _OP_PING, _OP_PONG = 0x8, 0x9, 0xA

_REASONS = {
    101: "Switching Protocols",
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    426: "Upgrade Required",
    500: "Internal Server Error",
}


@dataclass
class HTTPRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]      # 键统一小写
    body: bytes = b""
    remote: str = ""

    def json(self) -> dict[str, Any]:
        """尽力把请求体解析成 dict，失败返回空 dict（不抛异常）。"""
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


@dataclass
class HTTPResponse:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data: Any, status: int = 200) -> "HTTPResponse":
        return cls(
            status=status,
            body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        )


class WSConnection:
    """一条已完成握手的 WebSocket 连接（服务端侧）。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        path: str = "/",
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        remote: str = "",
    ):
        self._reader = reader
        self._writer = writer
        self.path = path
        self.headers = headers or {}
        self.query = query or {}
        self.remote = remote
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._frag_opcode = 0
        self._frag_buf = bytearray()

    # ---------------- 状态 ----------------
    @property
    def closed(self) -> bool:
        return self._closed or self._writer.is_closing()

    def _mark_closed(self) -> None:
        self._closed = True

    # ---------------- 发送 ----------------
    async def send_text(self, text: str) -> None:
        await self._send_frame(_OP_TEXT, text.encode("utf-8"))

    async def send_json(self, obj: Any) -> None:
        await self.send_text(json.dumps(obj, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        await self._send_frame(_OP_BIN, data)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise ConnectionError("WebSocket 已关闭")
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        async with self._send_lock:
            try:
                self._writer.write(header + payload)
                await self._writer.drain()
            except (ConnectionError, RuntimeError, asyncio.CancelledError):
                self._mark_closed()
                raise

    # ---------------- 接收 ----------------
    async def recv(self) -> str | None:
        """收一条文本消息。连接关闭返回 None。"""
        while True:
            try:
                fin, opcode, payload = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self._mark_closed()
                return None
            if opcode == _OP_CLOSE:
                try:
                    await self._send_frame(_OP_CLOSE, payload[:2])
                except Exception:
                    pass
                self._mark_closed()
                return None
            if opcode == _OP_PING:
                try:
                    await self._send_frame(_OP_PONG, payload)
                except Exception:
                    self._mark_closed()
                    return None
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CONT:
                if not self._frag_opcode:
                    continue
                self._frag_buf += payload
                if not fin:
                    continue
                data = bytes(self._frag_buf)
                self._frag_opcode = 0
                self._frag_buf.clear()
                return data.decode("utf-8", "replace")
            if opcode in (_OP_TEXT, _OP_BIN):
                if not fin:
                    self._frag_opcode = opcode
                    self._frag_buf = bytearray(payload)
                    continue
                return payload.decode("utf-8", "replace")
            logger.debug("忽略未知 WS 操作码 %s", opcode)

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        head = await self._reader.readexactly(2)
        b1, b2 = head[0], head[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
        if length > _BODY_LIMIT:
            raise ConnectionError(f"帧过大: {length}")
        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length) if length else b""
        if masked and payload:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    # ---------------- 关闭 ----------------
    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
        try:
            await self._send_frame(_OP_CLOSE, payload)
        except Exception:
            pass
        self._mark_closed()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


WSHandler = Callable[[WSConnection], Awaitable[None]]
HTTPHandler = Callable[[HTTPRequest], Awaitable[HTTPResponse]]


class WebSocketServer:
    """同端口 HTTP + WS 服务端。`port=0` 时由系统分配（测试用）。"""

    def __init__(
        self,
        on_ws: WSHandler,
        on_http: HTTPHandler,
        host: str = "127.0.0.1",
        port: int = 0,
        server_header: str = "ArkLink",
    ):
        self._on_ws = on_ws
        self._on_http = on_http
        self.host = host
        self.port = port
        self.server_header = server_header
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> "WebSocketServer":
        self._server = await asyncio.start_server(
            self._on_client, self.host, self.port, limit=_HEADER_LIMIT
        )
        for sock in self._server.sockets:
            self.port = sock.getsockname()[1]
            break
        logger.info(
            "兼容层监听 http://%s:%d  ws://%s:%d/",
            self.host, self.port, self.host, self.port,
        )
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ---------------- 连接处理 ----------------
    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        remote = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer or "")
        try:
            req = await asyncio.wait_for(self._read_request(reader, remote), timeout=20)
        except Exception:
            req = None
        if req is None:
            await self._hard_close(writer)
            return

        if _is_ws_upgrade(req):
            await self._accept_ws(req, reader, writer)
            return

        try:
            resp = await self._on_http(req)
        except Exception:
            logger.exception("HTTP 处理异常: %s %s", req.method, req.path)
            resp = HTTPResponse.json(
                {"status": "failed", "retcode": 500, "data": None, "msg": "internal error"},
                status=500,
            )
        await self._write_http(writer, resp)

    async def _accept_ws(
        self,
        req: HTTPRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        key = req.headers.get("sec-websocket-key", "")
        if not key:
            await self._write_http(
                writer, HTTPResponse.json({"msg": "missing Sec-WebSocket-Key"}, status=400)
            )
            return
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        lines = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept}",
            f"Server: {self.server_header}",
        ]
        proto = req.headers.get("sec-websocket-protocol")
        if proto:
            # 部分框架（如 aiocqhttp）会声明子协议，原样回一个即可
            lines.append(f"Sec-WebSocket-Protocol: {proto.split(',')[0].strip()}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        try:
            await writer.drain()
        except Exception:
            await self._hard_close(writer)
            return

        conn = WSConnection(reader, writer, req.path, req.headers, req.query, req.remote)
        task = asyncio.create_task(self._run_ws(conn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_ws(self, conn: WSConnection) -> None:
        try:
            await self._on_ws(conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WS 会话异常: %s", conn.remote)
        finally:
            # 任务被取消时，`await conn.close()` 会在第一个 await 点直接抛
            # CancelledError，close 帧永远发不出去，对端就一直挂着等。
            # 用 shield + 独立任务，保证关闭流程能走完。
            closer = asyncio.ensure_future(conn.close())
            try:
                await asyncio.shield(closer)
            except asyncio.CancelledError:
                pass

    # ---------------- HTTP 基础 ----------------
    async def _read_request(
        self, reader: asyncio.StreamReader, remote: str
    ) -> HTTPRequest | None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            return None
        try:
            text = head.decode("iso-8859-1")
            lines = text.split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
        except (UnicodeDecodeError, ValueError):
            return None
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        body = b""
        if length > 0:
            if length > _BODY_LIMIT:
                return None
            try:
                body = await reader.readexactly(length)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
        split = urlsplit(target)
        query = {k: v[-1] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
        return HTTPRequest(
            method=method.upper(),
            path=split.path or "/",
            query=query,
            headers=headers,
            body=body,
            remote=remote,
        )

    async def _write_http(self, writer: asyncio.StreamWriter, resp: HTTPResponse) -> None:
        reason = _REASONS.get(resp.status, "OK")
        lines = [
            f"HTTP/1.1 {resp.status} {reason}",
            f"Content-Length: {len(resp.body)}",
            f"Content-Type: {resp.content_type}",
            "Connection: close",
            f"Server: {self.server_header}",
        ]
        for k, v in resp.headers.items():
            lines.append(f"{k}: {v}")
        try:
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + resp.body)
            await writer.drain()
        except Exception:
            pass
        await self._hard_close(writer)

    @staticmethod
    async def _hard_close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _is_ws_upgrade(req: HTTPRequest) -> bool:
    if req.method != "GET":
        return False
    conn = req.headers.get("connection", "").lower()
    upgrade = req.headers.get("upgrade", "").lower()
    return "upgrade" in conn and upgrade == "websocket"
