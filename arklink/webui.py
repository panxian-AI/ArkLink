"""WebUI：一个页面管住整条链路。

为什么要有它：这个项目之前的每个动作都是一条命令（`hook --inject`、`send`、
`doctor`、`serve onebot`），排障时要来回切终端。WebUI 把它们收进一页：

    GET  /                 单页控制台（状态 / 发送 / 记录 / 体检）
    GET  /api/status       适配器 + QQ 进程 + 发送引擎 + 策略
    POST /api/send         {"to": "示例好友", "text": "hi", "dry_run": true}
    GET  /api/history      最近发送记录
    GET  /api/doctor       体检报告（缓存 60s，避免每次点都重扫）

三个刻意的设计：

* **只用标准库**：`http.server` + `json`，不引 web 框架 —— 这个项目的
  依赖已经够少了，不能让一个管理页面把安装体积翻倍。
* **默认只听 127.0.0.1**：它能让 QQ 发消息，暴露到公网等于把账号交出去。
  真要从别的机器访问，自己加 `--host 0.0.0.0 --token xxx`，并自己想清楚风险。
* **发送默认演练**：页面上要点两次（先「演练」看策略判定，再「真发」），
  和 CLI 的 `--yes` 是同一套规矩。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .ntqq.send import SendService
from .ntqq.sendpolicy import SendPolicy, load_peers

logger = logging.getLogger("arklink.webui")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6800


class _Cache:
    """极简 TTL 缓存：慢探针（扫进程、认账号）不该每 5 秒跑一次。"""

    def __init__(self, ttl: float = 20.0) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, factory: Callable[[], Any]) -> Any:
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = factory()
        with self._lock:
            self._data[key] = (now, value)
        return value

    def drop(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)


class WebUI:
    """一条链路的控制台。`service` 决定能不能发，`token` 决定谁能开。"""

    def __init__(
        self,
        service: SendService | None = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str = "",
        password: str = "",
        title: str = "ArkLink 控制台",
    ) -> None:
        self.service = service or SendService(SendPolicy.from_config(None))
        self.host = host
        self.port = port
        self.token = token or ""
        #: 口令登录（借鉴 SnowLuma 的 WebUI：浏览器记 cookie，不用把 token 塞进 URL）
        self.password = password or ""
        self.sessions: set[str] = set()
        self.title = title
        self.cache = _Cache(ttl=20.0)
        self.started_at = time.time()
        #: 环形日志缓冲：页面底部能直接看，不用回去翻终端
        self.logs: deque[str] = deque(maxlen=500)
        self._lock_logs = threading.Lock()
        self._install_log_handler()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 日志
    def _install_log_handler(self) -> None:
        ui = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    ui.logs.append(
                        f"{time.strftime('%H:%M:%S', time.localtime(record.created))} "
                        f"{record.levelname:<5} {record.name}: {record.getMessage()}"
                    )
                except Exception:  # 日志本身不能把服务搞崩
                    pass

        handler = _Handler()
        handler.setLevel(logging.INFO)
        root = logging.getLogger("arklink")
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        self._log_handler = handler

    def recent_logs(self, limit: int = 200) -> list[str]:
        with self._lock_logs:
            return list(self.logs)[-limit:]


    # ------------------------------------------------------------ 状态采集
    def _qq_status(self) -> dict[str, Any]:
        def scan() -> dict[str, Any]:
            try:
                from .ntqq import hookproc

                procs = hookproc.find_processes()
            except Exception as exc:  # 探不到也不算错，页面显示未知即可
                return {"ok": False, "error": str(exc), "processes": []}
            items = []
            for p in procs:
                if not p.is_main:
                    continue
                items.append(
                    {
                        "pid": p.pid,
                        "name": p.name,
                        "pipe": p.pipe or "",
                        "pipe_state": p.pipe_state,
                        "cmdline": (p.cmdline or "")[:160],
                    }
                )
            return {"ok": bool(items), "processes": items}

        return self.cache.get("qq", scan)

    def _client_status(self) -> dict[str, Any]:
        def probe() -> dict[str, Any]:
            host = getattr(getattr(self.service, "engine", None), "host", None)
            if host is None:
                return {"supported": False}
            try:
                info = host.detect()
            except Exception as exc:
                return {"supported": True, "ok": False, "error": str(exc)}
            return {
                "supported": True,
                "ok": bool(info.running),
                **{k: v for k, v in info.as_dict().items()},
            }

        return self.cache.get("client", probe)

    def status(self) -> dict[str, Any]:
        from . import __version__

        engine = getattr(self.service, "engine", None)
        return {
            "app": "arklink",
            "version": __version__,
            "uptime": int(time.time() - self.started_at),
            "send": self.service.status(),
            "engine": getattr(engine, "name", "?"),
            "policy": self.service.policy.describe(),
            "limits": {
                "allowed_uins": list(self.service.policy.allowed_uins),
                "allowed_peers": list(self.service.policy.allowed_peers),
                "allow_group": self.service.policy.allow_group,
                "allowed_groups": list(self.service.policy.allowed_groups),
                "max_length": self.service.policy.max_length,
            },
            "qq": self._qq_status(),
            "client": self._client_status(),
            "peers": [p.as_dict() for p in load_peers()][:50],
        }

    def update_policy(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """白名单自助修改：页面上就能加人/加群，不用去命令行。"""
        action = str(payload.get("action") or "").strip().lower()
        raw = payload.get("values", payload.get("value", []))
        if isinstance(raw, str):
            values = [v.strip() for v in raw.replace("，", ",").split(",") if v.strip()]
        elif isinstance(raw, (list, tuple)):
            values = [str(v).strip() for v in raw if str(v).strip()]
        else:
            values = []
        group = bool(payload.get("group"))
        policy = self.service.policy

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
                return HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "code": 1400,
                    "msg": "重置会恢复默认边界，需要 yes=true",
                }
            policy.reset()
            msg = "已恢复默认：只发示例好友、群聊关闭"
        else:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "action 只能是 allow/deny/reset"}

        self.cache.drop()
        return HTTPStatus.OK, {
            "ok": True,
            "msg": msg,
            "policy": policy.to_dict(),
            "describe": policy.describe(),
        }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        items = list(getattr(self.service, "history", []))[-max(1, limit) :]
        return [r.as_dict() for r in reversed(items)]

    def doctor(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            try:
                from . import doctor as doc

                report = doc.run_checks(offline=True, with_server=False)
            except Exception as exc:
                return {"ok": False, "error": str(exc), "checks": []}
            checks = [
                {
                    "name": check.name,
                    "status": check.status,
                    "ok": check.status != doc.FAIL,
                    "detail": check.detail,
                    "hint": getattr(check, "hint", ""),
                }
                for check in report.checks
            ]
            return {"ok": all(c["ok"] for c in checks), "checks": checks, "summary": doc.summary(report)}

        return self.cache.get("doctor", run)

    def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        raw_to = payload.get("to") or payload.get("user_id") or payload.get("group_id") or ""
        targets = [t.strip() for t in str(raw_to).replace("，", ",").split(",") if t.strip()]
        text = payload.get("text") or payload.get("message") or ""
        dry_run = bool(payload.get("dry_run", True))
        if not targets:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "code": 1400, "msg": "缺少收件人"}
        uin = str(payload.get("uin") or (self.service.policy.allowed_uins[0] if self.service.policy.allowed_uins else ""))
        chat_type = 2 if payload.get("group_id") else 1
        # 多条：一行一条，按顺序发（突发场景；顺序由引擎的队列保证）
        lines = [ln for ln in str(text).splitlines() if ln.strip()] or [str(text)]
        if payload.get("single_line"):
            lines = [str(text)]

        async def run() -> list[Any]:
            out = []
            for line in lines:
                for target in targets:
                    out.append(await self.service.send(target, line, uin=uin, chat_type=chat_type))
            return out

        engine = self.service.engine
        if dry_run and getattr(engine, "name", "") != "dry-run":
            from .ntqq.send import DryRunEngine

            self.service.engine = DryRunEngine()
            try:
                results = asyncio.run(run())
            finally:
                self.service.engine = engine
        else:
            results = asyncio.run(run())
        self.cache.drop("client")
        ok = all(r.ok for r in results)
        payload_out = {
            "ok": ok,
            "count": len(results),
            "targets": targets,
            "lines": len(lines),
            "items": [r.as_dict() for r in results],
            "msg": "全部成功" if ok else f"{sum(1 for r in results if not r.ok)} 条失败",
        }
        if len(results) == 1:
            payload_out.update(results[0].as_dict())
        return (HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST), payload_out

    # ------------------------------------------------------------ HTTP
    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ArkLinkWebUI"
            protocol_version = "HTTP/1.1"

            # ---- 工具 ----
            def _cookie_session(self) -> str:
                raw = self.headers.get("Cookie") or ""
                for part in raw.split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "arklink_session":
                        return value
                return ""

            def _auth_ok(self) -> bool:
                if not ui.token and not ui.password:
                    return True
                auth = self.headers.get("Authorization") or ""
                if ui.token and auth.lower().startswith("bearer ") and auth[7:].strip() == ui.token:
                    return True
                query = parse_qs(urlparse(self.path).query)
                if ui.token and (query.get("token") or [""])[0] == ui.token:
                    return True
                sid = self._cookie_session()
                return bool(sid) and sid in ui.sessions

            def _send_json(self, status: int, data: Any) -> None:
                body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, status: int, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:  # 别把访问日志刷进主日志
                logger.debug("%s - %s", self.address_string(), fmt % args)

            # ---- 路由 ----
            def _login(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
                except (ValueError, OSError):
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "msg": "body 不是合法 JSON"})
                given = str((payload or {}).get("password") or "")
                if not ui.password:
                    ui.sessions.add(secrets.token_urlsafe(16))
                    sid = next(iter(ui.sessions))
                    return self._send_json(HTTPStatus.OK, {"ok": True, "session": sid})
                if not hmac.compare_digest(given, ui.password):
                    logger.warning("WebUI 登录失败：口令不对")
                    return self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "msg": "口令不对"})
                sid = secrets.token_urlsafe(24)
                ui.sessions.add(sid)
                body = json.dumps({"ok": True}, ensure_ascii=False).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", f"arklink_session={sid}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path == "/":
                    return self._send_html(HTTPStatus.OK, _PAGE.replace("{{TITLE}}", ui.title))
                if not self._auth_ok():
                    return self._send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"ok": False, "msg": "需要登录", "password": bool(ui.password)},
                    )
                if path == "/api/status":
                    return self._send_json(HTTPStatus.OK, ui.status())
                if path == "/api/history":
                    query = parse_qs(urlparse(self.path).query)
                    limit = int((query.get("limit") or ["20"])[0])
                    return self._send_json(HTTPStatus.OK, {"items": ui.history(limit)})
                if path == "/api/doctor":
                    return self._send_json(HTTPStatus.OK, ui.doctor())
                if path == "/api/peers":
                    return self._send_json(HTTPStatus.OK, {"items": [p.as_dict() for p in load_peers()]})
                if path == "/api/logs":
                    query = parse_qs(urlparse(self.path).query)
                    limit = int((query.get("limit") or ["200"])[0])
                    return self._send_json(HTTPStatus.OK, {"items": ui.recent_logs(limit)})
                if path == "/api/auth":
                    return self._send_json(
                        HTTPStatus.OK,
                        {"password": bool(ui.password), "token": bool(ui.token), "authed": self._auth_ok()},
                    )
                return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "msg": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path == "/api/login":
                    return self._login()
                if path == "/api/logout":
                    ui.sessions.clear()
                    return self._send_json(HTTPStatus.OK, {"ok": True})
                if not self._auth_ok():
                    return self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "msg": "需要 token"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, OSError):
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "msg": "body 不是合法 JSON"})
                if path == "/api/send":
                    status, data = ui.send(payload if isinstance(payload, dict) else {})
                    return self._send_json(status, data)
                if path == "/api/policy":
                    status, data = ui.update_policy(payload if isinstance(payload, dict) else {})
                    return self._send_json(status, data)
                if path == "/api/cache/clear":
                    ui.cache.drop()
                    return self._send_json(HTTPStatus.OK, {"ok": True})
                return self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "msg": "not found"})

        return Handler

    # ------------------------------------------------------------ 生命周期
    def build(self) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = httpd.server_address[1]
        return httpd

    def start(self) -> str:
        """起在后台线程里（测试与嵌入式用）。返回访问地址。"""
        httpd = self._httpd or self.build()
        self._thread = threading.Thread(target=httpd.serve_forever, name="arklink-webui", daemon=True)
        self._thread.start()
        return self.url()

    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    def serve_forever(self) -> None:
        httpd = self._httpd or self.build()
        print(f"WebUI 已启动：{self.url()}（Ctrl+C 停止）", flush=True)
        if not self.token and self.host not in ("127.0.0.1", "localhost"):
            print("⚠️ 没有设置 token，而且不是只听本机：任何能访问这个端口的人都能用你的 QQ 发消息", flush=True)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


_PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--txt:#e6e9ef;--dim:#8b93a7;
        --ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#4c8dff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
  .pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
  main{max-width:1040px;margin:0 auto;padding:18px 20px 60px;display:grid;gap:16px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:0 0 10px}
  .kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:13px}
  .kv span:first-child{color:var(--dim)}
  .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
  input,textarea,button,select{font:inherit;color:var(--txt);background:#0d1016;border:1px solid var(--line);border-radius:8px;padding:8px 10px;outline:none}
  input:focus,textarea:focus{border-color:var(--accent)}
  textarea{min-height:76px;resize:vertical;width:100%}
  button{background:var(--accent);border-color:transparent;color:#fff;cursor:pointer;font-weight:600}
  button.ghost{background:transparent;border-color:var(--line);color:var(--txt);font-weight:500}
  button:disabled{opacity:.5;cursor:not-allowed}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .row>*{flex:1}
  .row .shrink{flex:0 0 auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--dim);font-weight:500;font-size:12px}
  code{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#a8b3cf}
  #toast{position:fixed;right:18px;bottom:18px;background:#1d2330;border:1px solid var(--line);
         border-radius:10px;padding:10px 14px;opacity:0;transition:.25s;max-width:420px}
  #toast.show{opacity:1}
  .muted{color:var(--dim);font-size:12px}
</style></head><body>
<header>
  <h1>{{TITLE}}</h1>
  <span class="pill" id="ver">-</span>
  <span class="pill" id="engine">-</span>
  <span style="flex:1"></span>
  <button class="ghost shrink" onclick="loadAll()">刷新</button>
</header>
<main>
  <section class="grid">
    <div class="card"><h2>发送引擎</h2><div id="sendbox"></div></div>
    <div class="card"><h2>官方客户端</h2><div id="clientbox"></div></div>
    <div class="card"><h2>QQ 进程 / 管道</h2><div id="qqbox"></div></div>
    <div class="card"><h2>策略闸门</h2><div id="policybox"></div></div>
  </section>

  <section class="card">
    <h2>发一条</h2>
    <div class="row" style="margin-bottom:10px">
      <input id="to" class="shrink" style="flex:0 0 180px" placeholder="收件人（备注 / QQ号）" value="示例好友">
      <label class="muted row shrink" style="gap:6px;flex:0 0 auto">
        <input type="checkbox" id="dry" checked style="width:auto"> 演练（不真发）
      </label>
    </div>
    <textarea id="text" placeholder="要发送的文本…"></textarea>
    <div class="row" style="margin-top:10px">
      <button id="sendbtn" onclick="doSend()">发送</button>
      <span class="muted" id="sendhint"></span>
    </div>
  </section>

  <section class="card">
    <h2>发送记录</h2>
    <table><thead><tr><th style="width:70px">时间</th><th style="width:110px">引擎</th><th style="width:120px">对象</th><th>内容</th><th style="width:80px">结果</th></tr></thead>
    <tbody id="hist"><tr><td colspan="5" class="muted">还没有记录</td></tr></tbody></table>
  </section>

  <section class="card">
    <h2>配置</h2>
    <div id="configbox"><span class="muted">加载中…</span></div>
  </section>

  <section class="card">
    <h2>日志 <span class="muted">(最近 200 行)</span></h2>
    <pre id="logs" style="margin:0;max-height:260px;overflow:auto;font:12px/1.6 ui-monospace,Consolas,monospace;color:#a8b3cf;white-space:pre-wrap"></pre>
  </section>

  <section class="card">
    <h2>体检 <span class="muted" id="docsum"></span></h2>
    <div id="doc"><span class="muted">点“刷新”后加载</span></div>
  </section>
</main>
<div id="login" style="position:fixed;inset:0;background:rgba(8,10,14,.82);display:none;align-items:center;justify-content:center;z-index:20">
  <div class="card" style="width:320px">
    <h2>请输入口令</h2>
    <input id="pw" type="password" style="width:100%" placeholder="WebUI 口令" onkeydown="if(event.key==='Enter')doLogin()">
    <div class="row" style="margin-top:10px"><button onclick="doLogin()">登录</button></div>
    <div class="muted" id="pwerr" style="margin-top:8px"></div>
  </div>
</div>
<div id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
const cls = (v) => v ? "ok" : "bad";
function kv(box, rows){
  box.innerHTML = rows.map(([k,v,c]) =>
    `<div class="kv"><span>${k}</span><span class="${c||''}">${v}</span></div>`).join("");
}
function toast(msg, bad){
  const t = $("toast"); t.textContent = msg;
  t.style.borderColor = bad ? "var(--bad)" : "var(--line)";
  t.classList.add("show"); clearTimeout(t._h);
  t._h = setTimeout(()=>t.classList.remove("show"), 2600);
}
async function api(path, opts){
  const r = await fetch(path, opts);
  const data = await r.json().catch(()=>({}));
  if(r.status === 401){ $("login").style.display = "flex"; }
  return {ok:r.ok, status:r.status, data};
}
async function doLogin(){
  const pw = $("pw").value;
  const {ok, data} = await api("/api/login", {method:"POST", headers:{"Content-Type":"application/json"},
                          body: JSON.stringify({password: pw})});
  if(ok){ $("login").style.display = "none"; $("pwerr").textContent = ""; loadAll(); }
  else { $("pwerr").textContent = (data && data.msg) || "登录失败"; }
}
async function loadAll(){
  const {data:s} = await api("/api/status");
  if(!s || !s.send){ toast("拿不到状态", true); return; }
  $("ver").textContent = "v" + s.version;
  $("engine").textContent = "engine: " + s.engine;
  kv($("sendbox"), [
    ["可用", s.send.available ? "是" : "否", cls(s.send.available)],
    ["已发", s.send.sent],
    ["最近", s.send.last ? (s.send.last.ok ? "成功" : "失败") : "-", s.send.last ? cls(s.send.last.ok) : ""],
    ["最近说明", s.send.last ? (s.send.last.msg || "") : "-"],
  ]);
  const c = s.client || {};
  kv($("clientbox"), c.supported ? [
    ["在线", c.ok ? "是" : "否", cls(c.ok)],
    ["账号", c.uin || "认不出", c.uin ? "ok" : "warn"],
    ["窗口", c.hwnd || "-"],
    ["进程", c.pid || "-"],
  ] : [["通道", "当前引擎不操作客户端"]]);
  const q = s.qq || {};
  kv($("qqbox"), (q.processes||[]).length ? (q.processes||[]).map(p => [
      "pid " + p.pid,
      (p.pipe ? p.pipe_state : "无管道"),
      p.pipe_state === "free" ? "ok" : (p.pipe_state === "busy" ? "warn" : ""),
    ]) : [["状态", q.ok ? "有进程" : "没找到 QQ 进程", q.ok ? "ok" : "bad"]]);
  const limits = s.limits || {};
  kv($("policybox"), [
    ["账号", (limits.allowed_uins||[]).join("、") || "（空）"],
    ["收件人", (limits.allowed_peers||[]).join("、") || "（空）"],
    ["群聊", limits.allow_group ? ("允许 " + ((limits.allowed_groups||[]).join("、") || "(不限群)")) : "关闭",
     limits.allow_group ? "warn" : ""],
    ["上限", (limits.max_length || 0) + " 字"],
  ]);
  // 白名单自助编辑：加人/加群不用回到命令行
  $("policybox").insertAdjacentHTML("beforeend",
    `<div class="row" style="margin-top:10px;gap:6px">
       <input id="pname" style="flex:1;min-width:80px" placeholder="名字 / 群号（多个用逗号）">
       <label class="muted row shrink" style="gap:4px;flex:0 0 auto">
         <input type="checkbox" id="pgroup" style="width:auto"> 群
       </label>
       <button class="ghost shrink" onclick="pol('allow')">允许</button>
       <button class="ghost shrink" onclick="pol('deny')">移除</button>
       <button class="ghost shrink" onclick="pol('reset')">重置</button>
     </div>
     <div class="muted" style="margin-top:6px;font-size:12px">改完立刻生效，存到 ~/.arklink/policy.json</div>`);
  const {data:h} = await api("/api/history?limit=20");
  const rows = (h.items||[]);
  $("hist").innerHTML = rows.length ? rows.map(r => {
    return `<tr><td class="muted">${r.ts || ""}</td><td>${r.engine||""}${r.dry_run?" (演练)":""}</td>` +
           `<td>${r.echo || r.peer_name || (r.peer && r.peer.name) || ""}</td><td>${(r.text||"").slice(0,80)}</td>` +
           `<td class="${cls(r.ok)}">${r.ok?"成功":"失败 " + (r.code||"")}</td></tr>`;
  }).join("") : `<tr><td colspan="5" class="muted">还没有记录</td></tr>`;
  kv($("configbox"), [
    ["引擎", s.engine],
    ["账号", (s.limits.allowed_uins||[]).join("、") || "（空）"],
    ["收件人", (s.limits.allowed_peers||[]).join("、") || "（空）"],
    ["群聊", s.limits.allow_group ? "允许" : "关闭"],
    ["单条上限", s.limits.max_length + " 字"],
    ["排队中", (s.send.pending||0) + " 条 / 队列 " + (s.send.queue_depth||0)],
    ["运行时长", s.uptime + " 秒"],
  ]);
  const {data:lg} = await api("/api/logs?limit=200");
  const box = $("logs");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  box.textContent = (lg.items||[]).join("\n") || "（还没有日志）";
  if(atBottom) box.scrollTop = box.scrollHeight;
  const {data:d} = await api("/api/doctor");
  $("docsum").textContent = d.summary || "";
  $("doc").innerHTML = `<table><tbody>` + (d.checks||[]).map(c =>
    `<tr><td style="width:150px">${c.name}</td><td class="${cls(c.ok)}" style="width:50px">${c.ok?"✓":"✗"}</td>` +
    `<td><code>${c.detail||""}</code> ${c.hint?`<span class="muted">${c.hint}</span>`:""}</td></tr>`
  ).join("") + `</tbody></table>`;
}
async function pol(action){
  const raw = ($("pname").value || "").trim();
  if(action !== "reset" && !raw){ toast("先填名字或群号", true); return; }
  if(action === "reset" && !confirm("恢复默认：只发示例好友、群聊关闭？")) return;
  const body = {
    action,
    values: raw.split(/[,，]/).map(x => x.trim()).filter(Boolean),
    group: $("pgroup").checked,
  };
  if(action === "reset") body.yes = true;
  const {ok, data} = await api("/api/policy", {method:"POST", headers:{"Content-Type":"application/json"},
                          body: JSON.stringify(body)});
  toast((data && data.msg) || (ok ? "已更新" : "失败"), !ok);
  if(ok) loadAll();
}
async function doSend(){
  const to = $("to").value.trim(), text = $("text").value;
  if(!to || !text){ toast("收件人和内容都要填", true); return; }
  const lines = text.split("\n").filter(x=>x.trim()).length || 1;
  const targets = to.split(/[,，]/).filter(x=>x.trim()).length || 1;
  $("sendbtn").disabled = true;
  $("sendhint").textContent = `发送中… ${targets} 个对象 × ${lines} 条`;
  try{
    const {data} = await api("/api/send", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({to, text, dry_run: $("dry").checked}),
    });
    $("sendhint").textContent = (data.ok ? "已发送：" : "未发送：") + (data.msg || "");
    toast(data.ok ? ("成功：" + (data.msg||"")) : ("失败：" + (data.msg||"")), !data.ok);
    if(data.ok && !$("dry").checked){ $("text").value = ""; }
    loadAll();
  } finally { $("sendbtn").disabled = false; }
}
loadAll(); setInterval(loadAll, 5000);
</script></body></html>
"""
