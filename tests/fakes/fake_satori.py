"""假的 Satori SDK：HTTP /v1/*. 方法 + WS /v1/events（IDENTIFY/READY/EVENT）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from .http import MiniHTTPServer

SELF_ID = "10001"
GROUP_ID = "20002"
USER_ID = "30003"


class FakeSatori:
    def __init__(self) -> None:
        self.http = MiniHTTPServer(self._http)
        self.api_port = 0
        self.ws_port = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.token = "satori-token"
        self._clients: set[Any] = set()
        self._ws_server: Any = None
        self._msg_seq = 8000
        self.identified: list[dict[str, Any]] = []

    async def start(self) -> "FakeSatori":
        self.api_port = await self.http.start()
        self._ws_server = await websockets.serve(self._ws_handler, "127.0.0.1", 0)
        self.ws_port = self._ws_server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        await self.http.stop()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def events_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/v1/events"

    # ---------- WS ----------
    async def _ws_handler(self, ws: Any) -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if int(frame.get("op") or 0) == 3:
                    self.identified.append(frame.get("body") or {})
                    await ws.send(json.dumps({
                        "op": 4,
                        "body": {
                            "logins": [{
                                "sn": 1,
                                "user": {"id": SELF_ID, "name": "ArkLink"},
                                "self_id": SELF_ID,
                                "platform": "qq",
                                "status": 1,
                            }],
                        },
                    }, ensure_ascii=False))
                elif int(frame.get("op") or 0) == 1:
                    await ws.send(json.dumps({"op": 2, "body": {}}))
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._clients and self.identified:
                return True
            await asyncio.sleep(0.02)
        return False

    async def push(self, event: dict[str, Any]) -> None:
        if not await self.wait_ready():
            raise AssertionError("Satori 客户端没有完成 IDENTIFY")
        for ws in list(self._clients):
            await ws.send(json.dumps({"op": 0, "body": event}, ensure_ascii=False))

    # ---------- HTTP ----------
    async def _http(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
        if self.token and headers.get("authorization") not in (f"Bearer {self.token}", ""):
            return 401, {"message": "unauthorized"}
        self.calls.append((path, body))
        if path.endswith("message.create"):
            self._msg_seq += 1
            # 规范：message.create 返回 Message 数组
            return 200, [{
                "id": str(self._msg_seq),
                "channel": {"id": body.get("channel_id"), "type": 0},
                "content": body.get("content", ""),
            }]
        if path.endswith("message.delete"):
            return 200, {}
        if path.endswith("message.get"):
            return 200, {"id": body.get("message_id"), "content": "hi"}
        if path.endswith("guild.list"):
            return 200, {"data": [{"id": GROUP_ID, "name": "测试群"}]}
        if path.endswith("guild.member.list"):
            return 200, {"data": [{"user": {"id": USER_ID, "name": "阿鱼"}, "nick": "鱼"}]}
        return 404, {"message": "not found"}

    # ---------- 事件构造 ----------
    @staticmethod
    def group_message(text: str = "hello", message_id: str = "90001") -> dict[str, Any]:
        return {
            "id": 1,
            "type": "message-created",
            "platform": "qq",
            "self_id": SELF_ID,
            "timestamp": 1700000000,
            "sn": 1,
            "channel": {"id": GROUP_ID, "type": 0, "name": "测试群"},
            "guild": {"id": GROUP_ID, "name": "测试群"},
            "member": {"user": {"id": USER_ID, "name": "阿鱼"}, "nick": "鱼"},
            "user": {"id": USER_ID, "name": "阿鱼"},
            "message": {"id": message_id, "content": f'<at id="{SELF_ID}"/> {text}'},
        }

    @staticmethod
    def direct_message(text: str = "hi", message_id: str = "90002") -> dict[str, Any]:
        return {
            "id": 2,
            "type": "message-created",
            "platform": "qq",
            "self_id": SELF_ID,
            "timestamp": 1700000000,
            "sn": 2,
            "channel": {"id": USER_ID, "type": 1},
            "user": {"id": USER_ID, "name": "阿鱼"},
            "message": {"id": message_id, "content": text},
        }
