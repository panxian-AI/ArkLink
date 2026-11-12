"""WebUI 测试：鉴权、状态、发送（演练）、多对象多条、体检。

只起临时端口、用 DryRunEngine，不碰真实窗口。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from arklink.ntqq.send import DryRunEngine, SendService
from arklink.ntqq.sendpolicy import SendPolicy
from arklink.webui import WebUI


@pytest.fixture()
def ui():
    service = SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象", "测试对象2")), DryRunEngine())
    server = WebUI(service, port=0, token="t")
    url = server.start()
    yield server, url, service
    server.stop()


def _get(url: str, path: str, token: str = "t"):
    req = urllib.request.Request(url.rstrip("/") + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode())


def _post(url: str, path: str, payload: dict, token: str = "t"):
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_page_served(ui):
    _server, url, _svc = ui
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = resp.read().decode()
    assert resp.status == 200 and "ArkLink" in body and "/api/status" in body


def test_auth_required(ui):
    _server, url, _svc = ui
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(url, "/api/status", token="")
    assert exc.value.code == 401
    # query token 也能过
    status, data = _get(url, "/api/status?token=t", token="")
    assert status == 200 and data["app"] == "arklink"


def test_status_shape(ui):
    _server, url, _svc = ui
    _status, data = _get(url, "/api/status")
    assert data["version"]
    assert data["send"]["engine"] == "dry-run"
    assert data["limits"]["allowed_uins"] == ["10001"]
    assert "processes" in data["qq"]
    assert data["client"] == {"supported": False}


def test_send_dry_run_default(ui):
    _server, url, svc = ui
    status, data = _post(url, "/api/send", {"to": "测试对象", "text": "hi"})
    assert status == 200 and data["ok"] and data["dry_run"] is True
    assert len(svc.history) == 1


def test_send_multiple_targets_and_lines(ui):
    _server, url, svc = ui
    status, data = _post(url, "/api/send", {"to": "测试对象，测试对象2", "text": "一\n二"})
    assert status == 200 and data["ok"]
    assert data["targets"] == ["测试对象", "测试对象2"] and data["lines"] == 2
    assert data["count"] == 4 and len(svc.history) == 4


def test_send_rejects_outsider(ui):
    _server, url, svc = ui
    status, data = _post(url, "/api/send", {"to": "陌生人", "text": "hi"})
    assert status == 400 and data["ok"] is False
    assert svc.history[-1].code == 1404


def test_history_endpoint(ui):
    _server, url, _svc = ui
    _post(url, "/api/send", {"to": "测试对象", "text": "hi"})
    _status, data = _get(url, "/api/history?limit=5")
    assert data["items"] and data["items"][0]["text"] == "hi"
    assert data["items"][0]["ts"]


def test_doctor_endpoint(ui):
    _server, url, _svc = ui
    _status, data = _get(url, "/api/doctor")
    assert data["checks"] and data["summary"]
    assert all("ok" in c for c in data["checks"])


def test_unknown_route(ui):
    _server, url, _svc = ui
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(url, "/api/nope")
    assert exc.value.code == 404


# ---------------------------------------------------------------- 口令登录


def _post_status(url: str, payload: dict, token: str = ""):
    return _post(url, "/api/login", payload, token=token)


def test_password_login_sets_session():
    service = SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), DryRunEngine())
    server = WebUI(service, port=0, password="s3cret")
    url = server.start()
    try:
        # 没登录 → 401
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(url, "/api/status", token="")
        assert exc.value.code == 401
        # 登录成功 → 拿到 cookie
        req = urllib.request.Request(
            url.rstrip("/") + "/api/login",
            data=json.dumps({"password": "s3cret"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
        assert cookie.startswith("arklink_session=")
        req2 = urllib.request.Request(url.rstrip("/") + "/api/status")
        req2.add_header("Cookie", cookie)
        with urllib.request.urlopen(req2, timeout=10) as resp:
            assert resp.status == 200
    finally:
        server.stop()


def test_password_login_rejects_wrong():
    service = SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), DryRunEngine())
    server = WebUI(service, port=0, password="right")
    url = server.start()
    try:
        status, data = _post_status(url, {"password": "wrong"})
        assert status == 401 and data["ok"] is False
    finally:
        server.stop()


def test_logs_and_auth_endpoints(ui):
    _server, url, _svc = ui
    _status, data = _get(url, "/api/logs?limit=10")
    assert isinstance(data["items"], list)
    _status, auth = _get(url, "/api/auth")
    assert auth["token"] is True
