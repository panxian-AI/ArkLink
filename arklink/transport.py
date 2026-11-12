"""极简 HTTP 客户端：只用标准库，在线程里跑，不引入 aiohttp/httpx。

设计取舍和 ArkBot 一致：整个项目只依赖 `websockets`，HTTP 一律
`urllib` + `asyncio.to_thread`，避免事件循环阻塞，也方便在
「协议端只监听 127.0.0.1」的场景里做超时/重试。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("arklink.http")


class HTTPError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} {url} {body[:200]}")


def post_json(
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    method: str = "POST",
) -> dict[str, Any]:
    """同步 POST/GET JSON，返回解析后的 dict（4xx/5xx 抛 HTTPError）。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise HTTPError(e.code, url, body) from e
    except urllib.error.URLError as e:
        raise HTTPError(0, url, str(e.reason)) from e
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise HTTPError(200, url, "响应不是合法 JSON") from None
    return parsed if isinstance(parsed, dict) else {"data": parsed}


async def apost_json(
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    method: str = "POST",
) -> dict[str, Any]:
    """异步版：丢到线程池，失败原样抛出 HTTPError。"""
    return await asyncio.to_thread(post_json, url, payload, headers, timeout, method)


def with_query(url: str, **params: Any) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if not clean:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urllib.parse.urlencode(clean)}"
