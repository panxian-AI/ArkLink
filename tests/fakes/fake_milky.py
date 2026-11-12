"""假的 Milky 协议端：HTTP /api + WS /event，行为按规范实现。

用途有两个：
1. 单测映射逻辑（段/事件转换）；
2. 全链路挂载测试（ArkBot + MilkyAdapter + 这个假协议端）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from .http import MiniHTTPServer

SELF_ID = 10001
GROUP_ID = 20002
USER_ID = 30003


class FakeMilky:
    def __init__(self) -> None:
        self.http = MiniHTTPServer(self._http)
        self.api_port = 0
        self.ws_port = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.token = "test-token"
        self._clients: set[Any] = set()
        self._ws_server: Any = None
        self._seq = 5000

    # ---------- 生命周期 ----------
    async def start(self) -> "FakeMilky":
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
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def event_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/event"

    # ---------- WS ----------
    async def _ws_handler(self, ws: Any) -> None:
        self._clients.add(ws)
        try:
            async for _ in ws:
                pass
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    async def wait_client(self, timeout: float = 5.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._clients:
                return True
            await asyncio.sleep(0.02)
        return False

    async def push(self, payload: dict[str, Any]) -> None:
        if not await self.wait_client():
            raise AssertionError("没有客户端连上 /event")
        for ws in list(self._clients):
            await ws.send(json.dumps(payload, ensure_ascii=False))

    # ---------- HTTP ----------
    async def _http(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
        if headers.get("authorization") not in (f"Bearer {self.token}", ""):
            return 401, {"status": "failed", "retcode": -401, "message": "鉴权失败"}
        endpoint = path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, body))
        return 200, self._dispatch(endpoint, body)

    def _dispatch(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        if endpoint in ("send_group_message", "send_private_message"):
            self._seq += 1
            return {"status": "ok", "retcode": 0, "data": {"message_seq": self._seq, "time": 1700000000}}
        if endpoint == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"uin": SELF_ID, "nickname": "ArkLink"}}
        if endpoint == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_id": body.get("group_id"), "group_name": "测试群"}}
        if endpoint == "recall_group_message":
            return {"status": "ok", "retcode": 0, "data": {}}
        if endpoint == "get_impl_info":
            return {"status": "ok", "retcode": 0, "data": {"impl_name": "fake-milky", "version": "1.3"}}
        if endpoint == "no_such_api":
            return {"status": "failed", "retcode": -404, "message": "not found"}
        return {"status": "ok", "retcode": 0, "data": {}}

    # ---------- 事件构造 ----------
    @staticmethod
    def group_message(text: str = "/echo hi", message_seq: int = 111) -> dict[str, Any]:
        return {
            "time": 1700000000,
            "self_id": SELF_ID,
            "event_type": "message_receive",
            "data": {
                "message_scene": "group",
                "peer_id": GROUP_ID,
                "message_seq": message_seq,
                "sender_id": USER_ID,
                "time": 1700000000,
                "segments": [
                    {"type": "mention", "data": {"user_id": SELF_ID, "name": "ArkLink"}},
                    {"type": "text", "data": {"text": " " + text}},
                ],
                "group": {"group_id": GROUP_ID, "group_name": "测试群", "member_count": 2},
                "group_member": {
                    "user_id": USER_ID,
                    "nickname": "阿鱼",
                    "card": "鱼",
                    "role": "admin",
                    "level": 3,
                },
            },
        }

    @staticmethod
    def friend_message(text: str = "hi", message_seq: int = 112) -> dict[str, Any]:
        return {
            "time": 1700000000,
            "self_id": SELF_ID,
            "event_type": "message_receive",
            "data": {
                "message_scene": "friend",
                "peer_id": USER_ID,
                "message_seq": message_seq,
                "sender_id": USER_ID,
                "time": 1700000000,
                "segments": [{"type": "text", "data": {"text": text}}],
                "friend": {"user_id": USER_ID, "nickname": "阿鱼", "remark": "鱼"},
            },
        }
