"""Python 客户端：三行代码接进你自己的程序。

    from arklink import Client
    bot = Client.from_env()               # 读 ~/.arklink/api.json（地址 + token）
    bot.send("备注名", "你好")
    for msg in bot.stream():              # 收消息（长连接，自动续传）
        print(msg["sender_name"], msg["text"])

刻意只依赖标准库：别人的环境里往往没有 `requests`，装依赖本身就会劝退。
需要异步/高并发时，把 `stream()` 放到线程里跑即可，不需要改这个类。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Iterator

DEFAULT_BASE = "http://127.0.0.1:6900"


class ApiError(RuntimeError):
    """服务端返回了非 2xx。带上 code，便于调用方区分「策略拒绝」和「参数错」。"""

    def __init__(self, status: int, code: int, msg: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(f"[{status}/{code}] {msg}")
        self.status = status
        self.code = code
        self.msg = msg
        self.payload = payload or {}


class Client:
    def __init__(self, base_url: str = DEFAULT_BASE, token: str = "", timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_env(cls, base_url: str = "", token: str = "") -> "Client":
        """地址与 token 的取值顺序：参数 → 环境变量 → ~/.arklink/api.json → 默认。"""
        state: dict[str, Any] = {}
        if not (base_url and token):
            from .api import load_state

            state = load_state()
        base = base_url or os.environ.get("ARKLINK_API") or state.get("base_url") or DEFAULT_BASE
        key = token or os.environ.get("ARKLINK_TOKEN") or state.get("token") or ""
        return cls(str(base), str(key))

    # ------------------------------------------------------------ 底层
    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *, stream: bool = False
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"msg": raw}
            raise ApiError(exc.code, int(body.get("code") or exc.code), str(body.get("msg") or raw), body) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, 0, f"连不上 {url}：{exc.reason}") from None

        if stream:
            return resp
        with resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(resp.status, 0, f"服务端返回的不是 JSON：{raw[:120]}") from None

    # ------------------------------------------------------------ 业务
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/health")

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/status")

    def send(
        self,
        to: str | Iterable[str],
        text: str | Iterable[str],
        *,
        dry_run: bool = False,
        group: bool = False,
        echo: str = "",
        uin: str = "",
    ) -> dict[str, Any]:
        """发消息。`to` 和 `text` 都可以是列表（多对象 × 多条，顺序执行）。"""
        payload: dict[str, Any] = {
            "to": list(to) if isinstance(to, (list, tuple, set)) else to,
            "text": list(text) if isinstance(text, (list, tuple, set)) else text,
            "dry_run": dry_run,
            "group": group,
        }
        if echo:
            payload["echo"] = echo
        if uin:
            payload["uin"] = uin
        return self._request("POST", "/api/v1/send", payload)

    def messages(self, since: int = 0, limit: int = 100) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/messages?since={int(since)}&limit={int(limit)}")

    def sends(self, limit: int = 50) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/sends?limit={int(limit)}")

    def inbound(self, message: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
        """把一条消息推进总线（自建采集器用）。"""
        payload = dict(message or {})
        payload.update(fields)
        return self._request("POST", "/api/v1/inbound", payload)

    # ------------------------------------------------------------ 查询 / 能力
    def capabilities(self) -> dict[str, Any]:
        """这张表告诉你当前通道能做什么、缺什么。上层据此决定降级策略。"""
        return self._request("GET", "/api/v1/capabilities")

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/self")

    def friends(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/friends")

    def groups(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/groups")

    def history(self, **_: Any) -> dict[str, Any]:
        """历史消息：当前通道拿不到（本地库加密），会抛 ApiError(1404) 并说明原因。"""
        return self._request("GET", "/api/v1/history")

    def recall(self, message_id: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/recall", {"message_id": message_id})

    def mute(self, group: str, user: str, seconds: int = 600) -> dict[str, Any]:
        return self._request("POST", "/api/v1/group/mute",
                             {"group": group, "user": user, "seconds": seconds})

    def kick(self, group: str, user: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/group/kick", {"group": group, "user": user})

    def policy(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/policy")

    def allow(self, *names: str, group: bool = False) -> dict[str, Any]:
        return self._request("POST", "/api/v1/policy", {"action": "allow", "values": list(names), "group": group})

    def deny(self, *names: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/policy", {"action": "deny", "values": list(names)})

    def reset_policy(self) -> dict[str, Any]:
        return self._request("POST", "/api/v1/policy", {"action": "reset", "yes": True})

    # ------------------------------------------------------------ 收消息
    def stream(self, since: int = 0, *, reconnect: bool = True, idle_timeout: float = 0.0) -> Iterator[dict[str, Any]]:
        """订阅消息流，逐条 yield。

        * `since`：从哪个 seq 之后开始（断线续传，不丢不重）。
        * `reconnect=True`：连接断了自动重连，从最后一条的 seq 继续。
        * `idle_timeout>0`：多久没消息就结束（脚本里跑「收一会儿」很方便）。
        """
        last = int(since)
        deadline = time.time() + idle_timeout if idle_timeout else 0.0
        while True:
            try:
                resp = self._request("GET", f"/api/v1/stream?since={last}", stream=True)
            except ApiError:
                if not reconnect:
                    raise
                time.sleep(1.0)
                continue
            try:
                for event, data in _iter_sse(resp):
                    if event == "message":
                        last = int(data.get("seq") or last)
                        yield data
                        deadline = time.time() + idle_timeout if idle_timeout else 0.0
                    elif deadline and time.time() > deadline:
                        return
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            if not reconnect or (deadline and time.time() > deadline):
                return
            time.sleep(1.0)


def _iter_sse(resp: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """最小 SSE 解析：`event:` + `data:`，空行分隔。"""
    event = "message"
    data: list[str] = []
    while True:
        raw = resp.readline()
        if not raw:
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except ValueError:
                    payload = {}
                yield event, payload if isinstance(payload, dict) else {"data": payload}
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
