"""测试用最小 HTTP 服务端（标准库，无框架）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

Handler = Callable[[str, dict[str, Any], dict[str, str]], Awaitable[Any]]


class MiniHTTPServer:
    """只支持 POST JSON / GET 的最小 HTTP 服务。

    回调签名：handler(path, body_dict, headers) -> (status, payload_dict)
    """

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            path = parts[1] if len(parts) > 1 else "/"
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            body = await reader.readexactly(length) if length else b""
            payload: dict[str, Any] = {}
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {"_raw": body.decode("utf-8", "replace")}
            self.requests.append((path, payload, headers))
            status, result = await self.handler(path, payload, headers)
        except Exception as e:  # pragma: no cover - 测试失败时会看到
            status, result = 500, {"error": repr(e)}
        data = json.dumps(result, ensure_ascii=False).encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode("latin-1") + data
        )
        try:
            await writer.drain()
        finally:
            writer.close()
