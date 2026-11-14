"""给别人调用的 HTTP 接口（v1）。

设计目标只有一条：**任何语言、任何机器，用 curl 就能收发**。

    curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:6900/api/v1/status
    curl -X POST .../api/v1/send   -d '{"to":"备注名","text":"hi"}'
    curl -N .../api/v1/stream                     # 收消息（SSE，长连接）

几个刻意的选择：

* **只依赖标准库**。别人拿到这个仓库、`pip install -e .` 之后能直接跑，
  不因为「装不上 aiohttp/fastapi」而卡住。
* **发送默认是真发**。接口名就叫 send，调用方的预期是「发了就发出去」；
  要演练得显式传 `"dry_run": true`。真正的安全阀是发送白名单（SendPolicy），
  它在任何路径上都不绕过。
* **收消息走 SSE 而不是 WebSocket**。SSE 用浏览器/`requests`/`curl -N` 都能消费，
  中间还能过绝大多数反向代理；断线用 `since=seq` 续传，不丢消息。
* **token 默认必填**。没 token 的接口只留 `/api/v1/health`，那一个不返回任何业务数据。
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .bus import MessageBus
from . import caps as caps_mod
from .ntqq import elements as el
from .ntqq.send import SendService
from .ntqq.sendpolicy import SendPolicy, policy_path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6900

#: token 与地址落盘位置。别人接的时候 `cat ~/.arklink/api.json` 就能拿到。
DEFAULT_STATE = "~/.arklink/api.json"

#: 需要伴随文件的段 → 查哪条能力。语音/视频按「文件」算：剪贴板粘过去
#: 就是一个文件附件，客户端不会替你转成语音条或视频预览。
ATTACHMENT_SEGMENTS = (
    ("image", "image"),
    ("file", "file"),
    ("video", "file"),
    ("record", "file"),
)


def attachment_problem(segments: list[dict[str, Any]]) -> tuple[str, str] | None:
    """找出「拿不到本地字节」的附件段，返回 (段类型, 原因)。

    为什么在 HTTP 层就拦：剪贴板只能粘本机上真实存在的文件。等到引擎里才发现
    要下载，调用方拿到的会是一句含糊的发送失败，还得自己去猜是路径写错了
    还是格式不对。这里直接说清楚，改起来只要一步。
    """
    for seg in segments:
        kind = str(seg.get("type") or "")
        if kind not in el.ATTACHMENT_KINDS:
            continue
        data = seg.get("data")
        target = el.file_of(data if isinstance(data, dict) else {})
        if not target:
            return kind, "缺少 file 字段（要么给本机路径，要么给能下载的地址）"
        if el.looks_remote(target):
            return kind, f"给的是地址而不是本机文件：{target[:60]}"
        # 文件不存在时剪贴板照样「写入成功」，回车后只把文字发出去——静默失败。
        # 所以在这里就拦住，别让调用方拿到一个 ok:true 却什么附件都没发。
        if not Path(target).expanduser().is_file():
            return kind, f"本机找不到这个文件：{target}"
    return None


def state_path() -> Path:
    override = os.environ.get("ARKLINK_API_STATE")
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_STATE).expanduser()


def load_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(data: dict[str, Any]) -> Path:
    target = state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def new_token() -> str:
    import secrets

    return secrets.token_urlsafe(24)


class ApiServer:
    """把 SendService + MessageBus 暴露成 REST + SSE。"""

    def __init__(
        self,
        service: SendService,
        bus: MessageBus | None = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str = "",
        cors: bool = False,
        state_file: bool = True,
    ) -> None:
        self.service = service
        self.bus = bus or MessageBus()
        self.host = host
        self.port = port
        self.token = token
        self.cors = cors
        self.state_file = state_file
        self.started = time.time()
        self._httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ 生命周期
    def build(self) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = httpd.server_address[1]
        return httpd

    def start(self) -> str:
        httpd = self._httpd or self.build()
        threading.Thread(target=httpd.serve_forever, name="arklink-api", daemon=True).start()
        if self.state_file:
            save_state({"base_url": self.url().rstrip("/"), "token": self.token, "port": self.port})
        return self.url()

    def serve_forever(self) -> None:
        httpd = self._httpd or self.build()
        if self.state_file:
            save_state({"base_url": self.url().rstrip("/"), "token": self.token, "port": self.port})
        httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    # ------------------------------------------------------------ 业务
    def status(self) -> dict[str, Any]:
        send = self.service.status()
        return {
            "app": "arklink",
            "version": __version__,
            "uptime": round(time.time() - self.started, 1),
            "engine": send.get("engine"),
            "available": send.get("available"),
            "policy": send.get("policy"),
            "limits": self.service.policy.to_dict(),
            "queue": {"pending": send.get("pending", 0), "depth": send.get("queue_depth", 0)},
            "sends": {"count": len(self.service.history), "last": send.get("last")},
            "bus": self.bus.stats(),
        }

    def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        raw_to = payload.get("to") or payload.get("targets") or payload.get("target") or ""
        if isinstance(raw_to, (list, tuple)):
            targets = [str(x).strip() for x in raw_to if str(x).strip()]
        else:
            targets = [t.strip() for t in str(raw_to).replace("，", ",").split(",") if t.strip()]
        if not targets:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "缺少收件人（to / targets）"}

        channel = getattr(self.service.engine, "name", "")
        raw_msg = payload.get("message")
        if isinstance(raw_msg, dict) and "type" in raw_msg:
            raw_msg = [raw_msg]

        if isinstance(raw_msg, (list, tuple)) and raw_msg:
            # 结构化消息（OneBot 段数组）：文本之外的段先看通道支不支持，
            # 不支持就直接告诉调用方缺什么，而不是让它发出去一段 [图片] 占位符。
            segs = [seg for seg in raw_msg if isinstance(seg, dict)]
            kinds = {str(seg.get("type")) for seg in segs}
            for kind, key in ATTACHMENT_SEGMENTS + (
                ("face", "face"), ("at", "at"), ("reply", "reply"),
            ):
                if kind in kinds:
                    missing = caps_mod.require(channel, key)
                    if missing is not None:
                        return HTTPStatus.BAD_REQUEST, {
                            "ok": False, "code": 1404, "msg": missing.reason, "need": missing.need,
                        }
            problem = attachment_problem(segs)
            if problem is not None:
                kind, why = problem
                return HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "code": 1404,
                    "msg": f"{kind} 段发不出去：{why}",
                    "need": "剪贴板只能粘本机已有的文件，先把文件下载到磁盘再把路径放进 file 字段",
                }
            bodies: list[Any] = [list(raw_msg)]
        else:
            raw_text = payload.get("text", raw_msg if isinstance(raw_msg, str) else "")
            if isinstance(raw_text, (list, tuple)):
                bodies = [str(x) for x in raw_text if str(x).strip()]
            else:
                bodies = [ln for ln in str(raw_text).splitlines() if ln.strip()] or [str(raw_text)]
            if not any(str(b).strip() for b in bodies):
                return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "缺少内容（text / message）"}
        lines = bodies

        dry_run = bool(payload.get("dry_run", False))
        uin = str(payload.get("uin") or (self.service.policy.allowed_uins[0] if self.service.policy.allowed_uins else ""))
        chat_type = 2 if (payload.get("group") or payload.get("group_id")) else 1
        echo = str(payload.get("echo") or "")
        if payload.get("group_id") and not payload.get("to"):
            targets = [str(payload["group_id"])]

        import asyncio

        engine = self.service.engine
        if dry_run and getattr(engine, "name", "") != "dry-run":
            from .ntqq.send import DryRunEngine

            self.service.engine = DryRunEngine()
            try:
                results = [asyncio.run(self.service.send(t, body, uin=uin, echo=echo, chat_type=chat_type))
                           for body in bodies for t in targets]
            finally:
                self.service.engine = engine
        else:
            results = [asyncio.run(self.service.send(t, body, uin=uin, echo=echo, chat_type=chat_type))
                       for body in bodies for t in targets]

        ok = all(r.ok for r in results)
        out: dict[str, Any] = {
            "ok": ok,
            "count": len(results),
            "targets": targets,
            "lines": len(lines),
            "dry_run": dry_run,
            "items": [r.as_dict() for r in results],
        }
        if not ok:
            # 顶层也放一份 code/msg：调用方不必遍历 items 才知道为什么失败。
            first = next((r for r in results if not r.ok), results[0])
            out["code"] = first.code
            out["msg"] = first.msg
        return (HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST), out

    def messages(self, query: dict[str, list[str]]) -> dict[str, Any]:
        since = int((query.get("since") or ["0"])[0] or 0)
        limit = int((query.get("limit") or ["100"])[0] or 100)
        items = self.bus.since(since, limit)
        return {"items": items, "count": len(items), "last_seq": self.bus.last_seq()}

    def inbound(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """采集器推消息进来。可以是一条 dict，也可以 {"items": [...]}。"""
        items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
        published = self.bus.publish_many(items, source=str(payload.get("source") or "inbound"))
        return HTTPStatus.OK, {
            "ok": True,
            "accepted": len(published),
            "duplicates": len(items) - len(published),
            "items": [m.as_dict() for m in published],
            "last_seq": self.bus.last_seq(),
        }

    def policy(self, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        policy: SendPolicy = self.service.policy
        if payload is None:
            return HTTPStatus.OK, {
                "ok": True,
                "policy": policy.to_dict(),
                "describe": policy.describe(),
                "path": str(policy_path()),
            }
        action = str(payload.get("action") or "").strip().lower()
        raw = payload.get("values", payload.get("value", []))
        if isinstance(raw, str):
            values = [v.strip() for v in raw.replace("，", ",").split(",") if v.strip()]
        elif isinstance(raw, (list, tuple)):
            values = [str(v).strip() for v in raw if str(v).strip()]
        else:
            values = []
        group = bool(payload.get("group"))

        if action == "allow":
            if not values:
                return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "要给对象"}
            added = policy.allow(*values, group=group)
            policy.save()
            msg = f"已允许：{'、'.join(added)}" if added else "本来就在白名单里"
        elif action == "deny":
            if not values:
                return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "要给对象"}
            removed = policy.deny(*values)
            policy.save()
            msg = f"已移除：{'、'.join(removed)}" if removed else "本来就不在白名单里"
        elif action == "reset":
            if not payload.get("yes"):
                return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "重置需要 yes=true"}
            policy.reset()
            msg = "已恢复出厂：白名单清空、群聊关闭"
        else:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "action 只能是 allow/deny/reset"}
        return HTTPStatus.OK, {"ok": True, "msg": msg, "policy": policy.to_dict(), "describe": policy.describe()}

    # ------------------------------------------------------------ 能力与查询
    @property
    def channel(self) -> str:
        return getattr(self.service.engine, "name", "")

    def capabilities(self) -> dict[str, Any]:
        return caps_mod.capabilities(self.channel)

    def self_info(self) -> dict[str, Any]:
        policy = self.service.policy
        uin = policy.allowed_uins[0] if policy.allowed_uins else ""
        return {
            "ok": True,
            "uin": uin,
            "allowed_uins": list(policy.allowed_uins),
            "source": "policy.json",
            "note": "" if uin else "还没配置账号：arklink peers uin <你的QQ号>",
        }

    def roster(self, chat_type: int) -> dict[str, Any]:
        from .ntqq.sendpolicy import load_peers

        items = [p.as_dict() for p in load_peers() if p.chat_type == chat_type]
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "source": "peers.json",
            "note": "" if items else "peers.json 为空：先 arklink roster --save-peers，或 arklink peers allow <备注>",
        }

    def unsupported(self, key: str, *, status: int = HTTPStatus.BAD_REQUEST) -> tuple[int, dict[str, Any]]:
        """能力不支持时统一的返回：说清原因 + 缺什么。"""
        missing = caps_mod.require(self.channel, key)
        if missing is None:
            return status, {"ok": False, "code": 1404, "msg": f"{key} 的引擎实现还没接上", "need": "等 DLL 发送实现"}
        return status, {"ok": False, "code": 1404, "msg": missing.reason, "need": missing.need}

    def sends(self, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = int((query.get("limit") or ["50"])[0] or 50)
        items = [r.as_dict() for r in self.service.history[-limit:]]
        return {"items": items, "count": len(items)}

    # ------------------------------------------------------------ HTTP
    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"ArkLink/{__version__}"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # 静音：日志走 logging
                return

            # ---------------- 基础设施 ----------------
            def _cors(self) -> None:
                if ui.cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

            def _json(self, status: int, data: dict[str, Any]) -> None:
                body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> bool:
                if not ui.token:
                    return True
                header = self.headers.get("Authorization") or ""
                token = header[7:].strip() if header.lower().startswith("bearer ") else ""
                if not token:
                    token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
                if not token:
                    token = self.headers.get("X-ArkLink-Token") or ""
                return bool(token) and hmac.compare_digest(token, ui.token)

            def _body(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    data = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, OSError):
                    return None
                return data if isinstance(data, dict) else None

            # ---------------- 路由 ----------------
            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(HTTPStatus.NO_CONTENT)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)

                if path in ("/", "/api/v1/health"):
                    return self._json(
                        HTTPStatus.OK,
                        {"ok": True, "app": "arklink", "version": __version__, "auth": bool(ui.token)},
                    )
                if not self._auth():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "code": 401, "msg": "需要 token"})
                if path == "/api/v1/status":
                    return self._json(HTTPStatus.OK, ui.status())
                if path == "/api/v1/messages":
                    return self._json(HTTPStatus.OK, ui.messages(query))
                if path == "/api/v1/sends":
                    return self._json(HTTPStatus.OK, ui.sends(query))
                if path == "/api/v1/policy":
                    status, data = ui.policy()
                    return self._json(status, data)
                if path == "/api/v1/capabilities":
                    return self._json(HTTPStatus.OK, ui.capabilities())
                if path == "/api/v1/self":
                    return self._json(HTTPStatus.OK, ui.self_info())
                if path == "/api/v1/friends":
                    return self._json(HTTPStatus.OK, ui.roster(1))
                if path == "/api/v1/groups":
                    return self._json(HTTPStatus.OK, ui.roster(2))
                if path == "/api/v1/history":
                    status, data = ui.unsupported("history", status=HTTPStatus.NOT_IMPLEMENTED)
                    return self._json(status, data)
                if path == "/api/v1/stream":
                    return self._stream(query)
                return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "code": 404, "msg": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path.rstrip("/") or "/"
                if not self._auth():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "code": 401, "msg": "需要 token"})
                payload = self._body()
                if payload is None:
                    return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "body 不是合法 JSON 对象"})
                if path == "/api/v1/send":
                    status, data = ui.send(payload)
                    return self._json(status, data)
                if path == "/api/v1/inbound":
                    status, data = ui.inbound(payload)
                    return self._json(status, data)
                if path == "/api/v1/policy":
                    status, data = ui.policy(payload)
                    return self._json(status, data)
                if path in ("/api/v1/recall", "/api/v1/group/mute", "/api/v1/group/kick"):
                    key = {"recall": "recall", "mute": "group.mute", "kick": "group.kick"}[path.rsplit("/", 1)[-1]]
                    status, data = ui.unsupported(key)
                    return self._json(status, data)
                return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "code": 404, "msg": "not found"})

            # ---------------- SSE ----------------
            def _stream(self, query: dict[str, list[str]]) -> None:
                since = int((query.get("since") or ["0"])[0] or 0)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self._cors()
                self.end_headers()

                def emit(event: str, data: dict[str, Any]) -> bool:
                    chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                    try:
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                        return True
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return False

                sid, q = ui.bus.subscribe()
                try:
                    if not emit("hello", {"last_seq": ui.bus.last_seq(), "since": since}):
                        return
                    # 续传：先把 since 之后积压的发出去，再接实时流
                    for item in ui.bus.since(since, 500):
                        if not emit("message", item):
                            return
                    import queue as _queue

                    while True:
                        try:
                            msg = q.get(timeout=10)
                        except _queue.Empty:
                            try:  # 心跳，防止中间代理掐掉空闲连接
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                return
                            continue
                        if not emit("message", msg.as_dict()):
                            return
                finally:
                    ui.bus.unsubscribe(sid)

        return Handler
