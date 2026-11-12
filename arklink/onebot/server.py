"""独立 OneBot 11 服务端：让 ArkLink 直接扮演 NapCat 的平替。

为什么需要它：NapCat / SnowLuma 被封之后，用户的上层软件（NoneBot2、Koishi、
AstrBot、自己写的脚本）不变，只改一个地址就能切过来 —— 这比「换一个框架重写
插件」的迁移成本低一个数量级。

```
NoneBot2 / Koishi / 自研脚本
        │  OneBot 11（正向 WS + HTTP API + HTTP 上报，同一端口）
        ▼
   OneBot11Server            ← 本文件（标准库，无第三方依赖）
        │  LinkAdapter 契约
        ▼
   ntqq-hook / milky / satori / ntqq-native
```

服务端可以挂多个适配器（多入口并行），事件里的 `self_id` 区分来源，
API 调用带 `self_id` 即可指定走哪个；单账号场景下 `self_id` 由服务端自动补齐。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Sequence

from ..base import LinkAdapter
from .ws import HTTPRequest, HTTPResponse, WSConnection, WebSocketServer

logger = logging.getLogger("arklink.onebot")

#: 这些路径都当正向 WS 接（不同框架的默认值不一样，全都认）
WS_PATHS = ("/", "/onebot/v11/ws", "/api/ws", "/ws")


class OneBot11Server:
    """把任意 `LinkAdapter` 暴露成 OneBot 11 实现。"""

    def __init__(
        self,
        adapters: Sequence[LinkAdapter] | LinkAdapter,
        host: str = "127.0.0.1",
        port: int = 6700,
        access_token: str = "",
        report_urls: Iterable[str] = (),
        heartbeat: float = 30.0,
    ) -> None:
        if isinstance(adapters, LinkAdapter):
            adapters = [adapters]
        self.adapters: list[LinkAdapter] = list(adapters)
        if not self.adapters:
            raise ValueError("至少要有一个适配器")
        self.host = host
        self.port = port
        self.access_token = access_token
        self.report_urls = [u for u in report_urls if u]
        self.heartbeat = heartbeat

        self._ws_server: WebSocketServer | None = None
        self._clients: set[WSConnection] = set()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._self_ids: dict[int, int] = {}
        self._started_at = 0.0
        self.stats = {"events": 0, "dropped": 0, "api_calls": 0}

    # ---------------- 生命周期 ----------------
    async def start(self) -> "OneBot11Server":
        for adapter in self.adapters:
            adapter.on_raw_event(self.push)
            if not getattr(adapter, "_running", False):
                # 调用方负责 run_forever 时不要再起一遍；这里只兜底
                pass
        self._ws_server = WebSocketServer(
            on_ws=self._handle_ws,
            on_http=self._handle_http,
            host=self.host,
            port=self.port,
            server_header="ArkLink/OneBot11",
        )
        await self._ws_server.start()
        self.port = self._ws_server.port
        self._started_at = time.time()
        for adapter in self.adapters:
            self._spawn(self._refresh_identity(adapter), "onebot-identity")
        self._spawn(self._pump(), "onebot-push")
        if self.heartbeat > 0:
            self._spawn(self._heartbeat_loop(), "onebot-heartbeat")
        logger.info("OneBot 11 已就绪：ws://%s:%d/  http://%s:%d/", self.host, self.port, self.host, self.port)
        return self

    async def serve_forever(self) -> None:
        await self.start()
        logger.info("OneBot 11 服务中，Ctrl-C 退出")
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        await self._broadcast(self._meta("lifecycle", "disable"))
        # 先把客户端连接关干净，再停监听：顺序反了的话，内层任务被 cancel 时
        # close 帧发不出去，对端会一直挂着等（Windows 上尤其明显）。
        for conn in list(self._clients):
            try:
                await asyncio.wait_for(conn.close(), timeout=2)
            except Exception:
                pass
        self._clients.clear()
        if self._ws_server is not None:
            try:
                await asyncio.wait_for(self._ws_server.stop(), timeout=5)
            except Exception:
                pass
            self._ws_server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), timeout=5
                )
            except Exception:
                pass
        self._tasks.clear()

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------- 账号 ----------------
    def _self_id_of(self, adapter: LinkAdapter) -> int:
        cached = self._self_ids.get(id(adapter))
        if cached:
            return cached
        value = getattr(adapter, "self_id", 0) or 0
        if not value:
            uin = str(getattr(adapter, "uin", "") or "")
            value = int(uin) if uin.isdigit() else 0
        return int(value)

    def _pick(self, params: dict[str, Any]) -> LinkAdapter:
        want = params.get("self_id")
        if want:
            for adapter in self.adapters:
                if self._self_id_of(adapter) == int(want):
                    return adapter
        for adapter in self.adapters:
            if adapter.is_connected:
                return adapter
        return self.adapters[0]

    async def _refresh_identity(self, adapter: LinkAdapter) -> None:
        """问一次 `get_login_info` 把 self_id 学到手（学不到就等事件里带）。"""
        for _ in range(5):
            try:
                info = await adapter.call_api("get_login_info", {})
            except Exception:
                info = None
            if isinstance(info, dict):
                uid = info.get("user_id") or info.get("uin")
                if uid:
                    self._self_ids[id(adapter)] = int(uid)
                    return
            await asyncio.sleep(2.0)

    # ---------------- 事件推送 ----------------
    def push(self, raw: dict[str, Any]) -> None:
        """适配器回调入口（同步）：入队即返回，绝不阻塞协议栈。"""
        try:
            self._queue.put_nowait(raw)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            logger.warning("OneBot 事件队列已满，丢弃一条（下游处理不过来）")

    async def _pump(self) -> None:
        while True:
            raw = await self._queue.get()
            try:
                await self._push_one(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("推送事件失败")

    async def _push_one(self, raw: dict[str, Any]) -> None:
        event = dict(raw)
        event.setdefault("time", int(time.time()))
        if not event.get("self_id"):
            self_id = self._self_id_of(self._pick({}))
            if self_id:
                event["self_id"] = self_id
        elif int(event["self_id"]):
            for adapter in self.adapters:
                if self._self_ids.get(id(adapter)) in (0, None):
                    self._self_ids[id(adapter)] = int(event["self_id"])
                    break
        self.stats["events"] += 1
        await self._broadcast(event)
        for url in self.report_urls:
            self._spawn(self._post_report(url, event), "onebot-report")

    async def _broadcast(self, event: dict[str, Any]) -> None:
        if not self._clients:
            return
        await asyncio.gather(
            *[self._safe_send(conn, event) for conn in list(self._clients)],
            return_exceptions=True,
        )

    async def _safe_send(self, conn: WSConnection, payload: dict[str, Any]) -> None:
        try:
            await conn.send_json(payload)
        except Exception:
            self._clients.discard(conn)

    async def _post_report(self, url: str, event: dict[str, Any]) -> None:
        import json
        import urllib.request

        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        except Exception as exc:
            logger.debug("HTTP 上报失败 %s: %s", url, exc)

    # ---------------- 元事件 ----------------
    def _meta(self, meta_type: str, sub_type: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "time": int(time.time()),
            "self_id": self._self_id_of(self._pick({})),
            "post_type": "meta_event",
            "meta_event_type": meta_type,
            "sub_type": sub_type,
        }
        if extra:
            payload.update(extra)
        return payload

    def _status(self) -> dict[str, Any]:
        online = any(a.is_connected for a in self.adapters)
        return {
            "online": online,
            "good": online,
            "app_initialized": True,
            "app_enabled": True,
            "plugins_good": True,
            "stat": {"packet_received": self.stats["events"], "packet_sent": 0},
        }

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            if not self._clients:
                continue
            await self._broadcast(
                self._meta("heartbeat", "heartbeat", {"status": self._status(), "interval": int(self.heartbeat * 1000)})
            )

    # ---------------- WS ----------------
    async def _handle_ws(self, conn: WSConnection) -> None:
        if not self._auth_ok(conn.headers, conn.query):
            await conn.close(1008, "token 不对")
            return
        self._clients.add(conn)
        logger.info("上层客户端接入：%s %s", conn.remote, conn.path)
        await conn.send_json(self._meta("lifecycle", "connect"))
        await conn.send_json(self._meta("heartbeat", "heartbeat", {"status": self._status()}))
        try:
            while True:
                text = await conn.recv()
                if text is None:
                    break
                try:
                    import json

                    req = json.loads(text)
                except Exception:
                    await conn.send_json({"status": "failed", "retcode": 1400, "data": None, "msg": "不是 JSON"})
                    continue
                if not isinstance(req, dict) or "action" not in req:
                    continue
                await self._answer(conn, req)
        finally:
            self._clients.discard(conn)
            logger.info("上层客户端断开：%s", conn.remote)

    async def _answer(self, conn: Any, req: dict[str, Any]) -> None:
        action = str(req.get("action") or "")
        params = req.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        result = await self.dispatch(action, params)
        result["echo"] = req.get("echo")
        try:
            await conn.send_json(result)
        except Exception:
            self._clients.discard(conn)

    # ---------------- HTTP ----------------
    def _auth_ok(self, headers: dict[str, str], query: dict[str, str]) -> bool:
        if not self.access_token:
            return True
        token = query.get("access_token") or ""
        if not token:
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            elif auth:
                token = auth.strip()
        return token == self.access_token

    async def _handle_http(self, req: HTTPRequest) -> HTTPResponse:
        if not self._auth_ok(req.headers, req.query):
            return HTTPResponse.json({"status": "failed", "retcode": 1403, "data": None, "msg": "token 不对"}, 401)

        action = ""
        params: dict[str, Any] = {}
        if req.method == "POST":
            body = req.json()
            if body.get("action"):
                action = str(body["action"])
                params = body.get("params") or {}
                echo = body.get("echo")
            elif req.path.strip("/"):
                action = req.path.strip("/").split("/")[-1]
                params = body
                echo = None
            else:
                return HTTPResponse.json({"status": "failed", "retcode": 1404, "data": None, "msg": "缺少 action"})
        elif req.method == "GET":
            path = req.path.strip("/")
            if not path:
                return HTTPResponse.json(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"app_name": "ArkLink", "app_version": _version(), "protocol": "onebot11"},
                    }
                )
            action = path.split("/")[-1]
            params = dict(req.query)
            echo = req.query.get("echo")
        else:
            return HTTPResponse.json({"status": "failed", "retcode": 1405, "data": None, "msg": "只支持 GET/POST"}, 405)

        # query 里的参数合并（HTTP GET 常见做法）
        for key, value in req.query.items():
            if key in ("access_token", "echo"):
                continue
            params.setdefault(key, _coerce(value))
        result = await self.dispatch(action, params)
        result["echo"] = echo if echo is not None else req.query.get("echo")
        return HTTPResponse.json(result)

    # ---------------- API ----------------
    async def dispatch(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.stats["api_calls"] += 1
        if action in ("get_version_info", "get_version"):
            return {"status": "ok", "retcode": 0, "data": {"app_name": "ArkLink", "app_version": _version()}}
        if action == "get_status":
            adapter = self._pick(params)
            try:
                data = await adapter.call_api("get_status", params)
            except Exception:
                data = self._status()
            if not isinstance(data, dict):
                data = self._status()
            return {"status": "ok", "retcode": 0, "data": {**self._status(), **data}}
        if not action:
            return {"status": "failed", "retcode": 1404, "data": None, "msg": "缺少 action"}

        adapter = self._pick(params)
        try:
            data = await adapter.call_api(action, _strip(params))
        except Exception as exc:
            retcode = getattr(exc, "retcode", None)
            message = getattr(exc, "message", "") or str(exc)
            if retcode is None:
                retcode = 500
                logger.warning("API %s 异常：%s", action, exc)
            return {"status": "failed", "retcode": int(retcode), "data": None, "msg": message}
        return {"status": "ok", "retcode": 0, "data": data}


def _strip(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    out.pop("self_id", None)
    out.pop("access_token", None)
    return out


def _coerce(value: str) -> Any:
    """query 里的一切都是字符串，尽量还原数字/布尔，否则适配器会因为类型报错。"""
    if value in ("true", "false"):
        return value == "true"
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:
        return "0"


def serve(adapters: Sequence[LinkAdapter] | LinkAdapter, **kwargs: Any) -> OneBot11Server:
    return OneBot11Server(adapters, **kwargs)


def run(adapters: Sequence[LinkAdapter] | LinkAdapter, **kwargs: Any) -> None:
    """阻塞式入口（CLI 用）。"""
    server = OneBot11Server(adapters, **kwargs)
    asyncio.run(server.serve_forever())
