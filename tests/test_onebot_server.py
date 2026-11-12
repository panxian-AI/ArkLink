"""独立 OneBot 11 服务端测试。

用真的 WebSocket 客户端（websockets）和真的 HTTP 请求打服务端，只把适配器
换成假实现 —— 验证的是「上层框架眼里的 NapCat 行为」，不是内部函数。
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request

import pytest
import websockets

from arklink.base import LinkAdapter, api_error
from arklink.onebot import OneBot11Server


def run(coro):
    return asyncio.run(coro)


class FakeAdapter(LinkAdapter):
    name = "fake"

    def __init__(self, self_id: int = 0, token: str = ""):
        super().__init__()
        self.token = token
        self.self_id = self_id
        self.sent: list[tuple[str, dict]] = []
        self.fail_send = False

    async def connect(self) -> None:
        self._connected.set()

    async def disconnect(self) -> None:
        self._connected.clear()

    async def call_api(self, action, params=None):
        params = dict(params or {})
        if action == "get_login_info":
            if not self.self_id:
                raise api_error(action, 1404, "还没登录")
            return {"user_id": self.self_id, "nickname": "阿方"}
        if action in ("send_group_msg", "send_private_msg", "send_msg"):
            if self.fail_send:
                raise api_error(action, 1404, "只读通道")
            self.sent.append((action, params))
            return {"message_id": len(self.sent)}
        if action == "get_status":
            return {"online": self.is_connected, "good": self.is_connected}
        if action == "get_group_list":
            return [{"group_id": 1, "group_name": "测试群"}]
        raise api_error(action, 1404, f"不支持 {action}")


def _message(text: str = "你好") -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": 7200123,
        "user_id": 10086,
        "message_id": 55,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": 10086, "nickname": "测试"},
    }


def test_ws_lifecycle_and_event_push():
    async def main():
        adapter = FakeAdapter(self_id=10001)
        await adapter.connect()
        server = OneBot11Server([adapter], port=0)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/") as ws:
                hello = json.loads(await ws.recv())
                assert hello["post_type"] == "meta_event"
                assert hello["meta_event_type"] == "lifecycle"
                assert hello["sub_type"] == "connect"
                assert hello["self_id"] == 10001, "lifecycle 要带账号"
                beat = json.loads(await ws.recv())
                assert beat["meta_event_type"] == "heartbeat"
                assert beat["status"]["online"] is True

                await adapter._emit_dict(_message("hi"))
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                assert event["post_type"] == "message"
                assert event["message_type"] == "group"
                assert event["self_id"] == 10001
                assert event["message"][0]["data"]["text"] == "hi"
        finally:
            await server.stop()

    run(main())


def test_ws_api_roundtrip_and_unsupported_retcode():
    async def main():
        adapter = FakeAdapter(self_id=10001)
        await adapter.connect()
        server = OneBot11Server([adapter], port=0)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/onebot/v11/ws") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 1, "message": "x"}, "echo": "a"}))
                resp = json.loads(await ws.recv())
                assert resp["status"] == "ok" and resp["retcode"] == 0
                assert resp["echo"] == "a"
                assert adapter.sent[0][0] == "send_group_msg"

                adapter.fail_send = True
                await ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 1, "message": "x"}, "echo": "b"}))
                resp = json.loads(await ws.recv())
                assert resp["status"] == "failed"
                assert resp["retcode"] == 1404, "只读通道必须原样透出 1404，上层才知道要降级"
                assert resp["echo"] == "b"
        finally:
            await server.stop()

    run(main())


def test_auth_rejects_bad_token():
    async def main():
        adapter = FakeAdapter(self_id=1)
        await adapter.connect()
        server = OneBot11Server([adapter], port=0, access_token="s3cret")
        await server.start()
        try:
            with pytest.raises(Exception):
                async with websockets.connect(f"ws://127.0.0.1:{server.port}/") as ws:
                    await ws.recv()
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/?access_token=s3cret") as ws:
                assert json.loads(await ws.recv())["meta_event_type"] == "lifecycle"
            async with websockets.connect(
                f"ws://127.0.0.1:{server.port}/", additional_headers={"Authorization": "Bearer s3cret"}
            ) as ws:
                assert json.loads(await ws.recv())["meta_event_type"] == "lifecycle"
        finally:
            await server.stop()

    run(main())


def test_http_api_universal_and_action_path():
    async def main():
        adapter = FakeAdapter(self_id=10001)
        await adapter.connect()
        server = OneBot11Server([adapter], port=0)
        await server.start()
        port = server.port
        try:
            def call(path: str, payload: dict | None = None) -> dict:
                data = json.dumps(payload).encode() if payload is not None else None
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST" if data is not None else "GET",
                )
                return json.loads(urllib.request.urlopen(req, timeout=10).read())

            universal = await asyncio.to_thread(
                call, "/", {"action": "get_group_list", "params": {}, "echo": 1}
            )
            assert universal["status"] == "ok"
            assert universal["data"][0]["group_id"] == 1

            direct = await asyncio.to_thread(call, "/send_group_msg", {"group_id": 9, "message": "hi"})
            assert direct["retcode"] == 0
            assert adapter.sent[-1][1]["group_id"] == 9

            get_like = await asyncio.to_thread(call, "/get_group_list")
            assert get_like["status"] == "ok"

            bad = await asyncio.to_thread(call, "/nope", {})
            assert bad["retcode"] == 1404
        finally:
            await server.stop()

    run(main())


def test_http_report_delivers_events():
    async def main():
        received: list[dict] = []

        async def handler(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.decode("iso-8859-1").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                body = await reader.readexactly(length) if length else b""
                received.append(json.loads(body))
                writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()

        server_side = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server_side.sockets[0].getsockname()[1]
        adapter = FakeAdapter(self_id=10001)
        await adapter.connect()
        server = OneBot11Server([adapter], port=0, report_urls=[f"http://127.0.0.1:{port}/"])
        await server.start()
        try:
            await adapter._emit_dict(_message("上报"))
            for _ in range(100):
                if received:
                    break
                await asyncio.sleep(0.02)
            assert received and received[0]["raw_message"] == "上报"
        finally:
            await server.stop()
            server_side.close()
            await server_side.wait_closed()

    run(main())


def test_multi_account_routing_by_self_id():
    async def main():
        a = FakeAdapter(self_id=1111)
        b = FakeAdapter(self_id=2222)
        await a.connect()
        await b.connect()
        server = OneBot11Server([a, b], port=0)
        await server.start()
        try:
            async with websockets.connect(f"ws://127.0.0.1:{server.port}/") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 1, "message": "x", "self_id": 2222}}))
                assert json.loads(await ws.recv())["retcode"] == 0
                assert not a.sent and len(b.sent) == 1
        finally:
            await server.stop()

    run(main())


def test_stop_sends_disable():
    async def main():
        adapter = FakeAdapter(self_id=1)
        await adapter.connect()
        server = OneBot11Server([adapter], port=0)
        await server.start()
        async with websockets.connect(f"ws://127.0.0.1:{server.port}/") as ws:
            await ws.recv()
            await ws.recv()
            await server.stop()
            tail = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            assert tail["meta_event_type"] == "lifecycle"
            assert tail["sub_type"] == "disable"

    run(main())


def test_self_id_learned_from_login_info_when_event_lacks_it():
    async def main():
        adapter = FakeAdapter(self_id=3333)
        await adapter.connect()
        server = OneBot11Server([adapter], port=0)
        await server.start()
        try:
            await asyncio.sleep(0.1)
            raw = _message("无 self_id")
            raw.pop("post_type")
            await adapter._emit_dict({k: v for k, v in raw.items() if k != "self_id"})
            for _ in range(100):
                if server.stats["events"]:
                    break
                await asyncio.sleep(0.01)
            assert server.stats["events"] == 1
        finally:
            await server.stop()

    run(main())
