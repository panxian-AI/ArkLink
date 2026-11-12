"""脱离开发者的自助层：策略落盘 + `peers` 命令 + `start` 入口。

这一组测试守住三件事：
1. 改白名单只写 `~/.arklink/policy.json`，不碰代码，重启后仍在；
2. 默认边界（只发示例好友、群聊关闭）不会被「文件不存在」或「拼错键名」意外放宽；
3. `arklink start` 打印的地址就是浏览器打开的地址（不用记参数）。
"""

from __future__ import annotations

import json
import os

import pytest

from arklink.__main__ import main
from arklink.ntqq import sendpolicy as sp


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path, monkeypatch):
    """每个用例一份独立策略文件，绝不碰真实 ~/.arklink。"""
    path = tmp_path / "policy.json"
    monkeypatch.setenv("ARKLINK_POLICY", str(path))
    monkeypatch.setenv("ARKLINK_PEERS", str(tmp_path / "peers.json"))
    return path


# ---------------------------------------------------------------- 落盘
def test_defaults_when_no_file(isolated_policy):
    policy = sp.SendPolicy.from_config(None)
    assert policy.allowed_uins == ("1000000001",)
    assert policy.allowed_peers == ("示例好友",)
    assert policy.allow_group is False
    assert not isolated_policy.exists()


def test_allow_and_save_roundtrip(isolated_policy):
    policy = sp.SendPolicy.from_config(None)
    added = policy.allow("505", group=True)
    assert added == ["505"]
    policy.save()

    reloaded = sp.SendPolicy.from_config(None)
    assert "505" in reloaded.allowed_peers
    assert reloaded.allow_group is True
    assert "505" in reloaded.allowed_groups
    assert isolated_policy.exists()


@pytest.mark.skipif(os.name != "posix", reason="Windows 上 chmod 不生效")
def test_save_is_owner_only(isolated_policy):
    sp.SendPolicy.from_config(None).save()
    assert isolated_policy.stat().st_mode & 0o777 == 0o600


def test_deny_removes_from_both_lists(isolated_policy):
    policy = sp.SendPolicy.from_config(None)
    policy.allow("505", group=True)
    policy.save()
    removed = policy.deny("505")
    policy.save()
    assert removed == ["505"]
    reloaded = sp.SendPolicy.from_config(None)
    assert "505" not in reloaded.allowed_peers
    assert reloaded.allowed_groups == ()
    # 群白名单空了，群聊开关必须一起关掉，不能留下「允许不限群」的缝
    assert reloaded.allow_group is False


def test_config_overrides_file(isolated_policy):
    sp.save_policy_file({"allowed_peers": ["505"], "allow_group": True, "allowed_groups": ["505"]})
    policy = sp.SendPolicy.from_config({"allowed_peers": ["示例好友"]})
    assert policy.allowed_peers == ("示例好友",)
    assert policy.allow_group is True  # 文件里的其余字段仍然生效


def test_unknown_keys_ignored(isolated_policy):
    isolated_policy.write_text(
        json.dumps({"allowed_peer": ["所有人"], "allow_groups": True}), encoding="utf-8"
    )
    policy = sp.SendPolicy.from_config(None)
    assert policy.allowed_peers == ("示例好友",)
    assert policy.allow_group is False


def test_broken_file_falls_back_to_defaults(isolated_policy):
    isolated_policy.write_text("{ not json", encoding="utf-8")
    policy = sp.SendPolicy.from_config(None)
    assert policy.allowed_peers == ("示例好友",)


def test_reset_restores_defaults_and_deletes_file(isolated_policy):
    policy = sp.SendPolicy.from_config(None)
    policy.allow("505", group=True)
    policy.save()
    assert isolated_policy.exists()

    policy.reset()
    assert policy.allowed_peers == ("示例好友",)
    assert policy.allow_group is False
    assert not isolated_policy.exists()


# ---------------------------------------------------------------- 群聊语义
def test_group_name_resolves_as_group(isolated_policy):
    policy = sp.SendPolicy.from_config(None)
    policy.allow("505", group=True)
    decision = policy.resolve("505")
    assert decision.ok
    assert decision.peer is not None and decision.peer.chat_type == 2


def test_group_still_blocked_until_allowed(isolated_policy, tmp_path):
    # 花名册里 505 是个群；只把它加进收件人、不开群开关 → 仍然要拒
    (tmp_path / "peers.json").write_text(
        json.dumps({"peers": [{"chat_type": 2, "peer_uid": "505", "name": "505"}]}),
        encoding="utf-8",
    )
    policy = sp.SendPolicy.from_config(None)
    policy.allow("505")
    decision = policy.resolve("505")
    assert not decision.ok
    assert decision.code == sp.CODE_FORBIDDEN
    assert "群聊" in decision.reason


# ---------------------------------------------------------------- CLI
def test_cli_peers_allow_group(isolated_policy, capsys):
    code = main(["peers", "allow", "505", "--group"])
    assert code == 0
    out = capsys.readouterr().out
    assert "已允许" in out and "群聊允许" in out
    data = json.loads(isolated_policy.read_text(encoding="utf-8"))
    assert data["allowed_peers"] == ["示例好友", "505"]
    assert data["allow_group"] is True


def test_cli_peers_allow_is_idempotent(isolated_policy, capsys):
    main(["peers", "allow", "505", "--group"])
    capsys.readouterr()
    assert main(["peers", "allow", "505", "--group"]) == 0
    assert "本来就在" in capsys.readouterr().out


def test_cli_peers_list_json(isolated_policy, capsys):
    assert main(["peers", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["allowed_peers"] == ["示例好友"]
    assert payload["allow_group"] is False
    assert "describe" in payload


def test_cli_peers_deny(isolated_policy, capsys):
    main(["peers", "allow", "505", "--group"])
    assert main(["peers", "deny", "505"]) == 0
    assert "已移除" in capsys.readouterr().out
    assert json.loads(isolated_policy.read_text(encoding="utf-8"))["allow_group"] is False


def test_cli_peers_reset_needs_yes(isolated_policy, capsys):
    main(["peers", "allow", "505", "--group"])
    assert main(["peers", "reset"]) == 1
    assert "--yes" in capsys.readouterr().err
    assert isolated_policy.exists()

    assert main(["peers", "reset", "--yes"]) == 0
    assert not isolated_policy.exists()


def test_cli_send_to_group_after_allow(isolated_policy, capsys):
    main(["peers", "allow", "505", "--group"])
    capsys.readouterr()
    assert main(["send", "--to", "505", "--text", "自检"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "505" in out


def test_cli_send_group_blocked_by_default(isolated_policy, capsys):
    assert main(["send", "--to", "505", "--text", "自检"]) == 2
    assert "没有「505」" in capsys.readouterr().out


# ---------------------------------------------------------------- start
def test_start_prints_url_and_opens_browser(monkeypatch, capsys):
    import arklink.webui as webui

    opened: list[str] = []

    class FakeServer:
        def serve_forever(self):
            return None

        def server_close(self):
            return None

    class FakeUI(webui.WebUI):
        def build(self):
            self.port = 12345
            self._httpd = FakeServer()
            return self._httpd

    monkeypatch.setattr(webui, "WebUI", FakeUI)
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url) or True)

    assert main(["start", "--port", "12345", "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:12345/" in out
    assert "策略" in out
    assert opened == []  # --no-browser 时不该开


def test_start_reports_unavailable_engine(monkeypatch, capsys):
    import arklink.webui as webui

    class FakeServer:
        def serve_forever(self):
            return None

        def server_close(self):
            return None

    class FakeUI(webui.WebUI):
        def build(self):
            self.port = 12346
            self._httpd = FakeServer()
            return self._httpd

    monkeypatch.setattr(webui, "WebUI", FakeUI)
    # dry-run 引擎永远可用（行为上等价于「不真发」），所以借用 x11 造一个不可用场景
    monkeypatch.setattr(
        "arklink.ntqq.x11.X11Engine.available", property(lambda self: False), raising=False
    )
    assert main(["start", "--port", "12346", "--engine", "x11", "--no-browser"]) == 0
    assert "不可用" in capsys.readouterr().out


# ---------------------------------------------------------------- 页面内改白名单
def _post(url: str, path: str, payload: dict):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


@pytest.fixture()
def live_ui():
    from arklink.ntqq.send import DryRunEngine, SendService
    from arklink.webui import WebUI

    server = WebUI(SendService(sp.SendPolicy.from_config(None), DryRunEngine()), port=0, token="t")
    url = server.start()
    try:
        yield server, url
    finally:
        server.stop()


def test_api_policy_allow_group(live_ui, isolated_policy):
    _server, url = live_ui
    status, data = _post(url, "/api/policy", {"action": "allow", "values": ["505"], "group": True})
    assert status == 200 and data["ok"]
    assert "505" in data["policy"]["allowed_peers"]
    assert data["policy"]["allow_group"] is True
    saved = json.loads(isolated_policy.read_text(encoding="utf-8"))
    assert saved["allowed_groups"] == ["505"]


def test_api_policy_deny_and_reset(live_ui, isolated_policy):
    _server, url = live_ui
    _post(url, "/api/policy", {"action": "allow", "values": ["505"], "group": True})
    status, data = _post(url, "/api/policy", {"action": "deny", "values": ["505"]})
    assert status == 200 and "505" not in data["policy"]["allowed_peers"]

    status, data = _post(url, "/api/policy", {"action": "reset"})
    assert status == 400 and not data["ok"]  # 没带 yes 不许重置
    status, data = _post(url, "/api/policy", {"action": "reset", "yes": True})
    assert status == 200 and data["policy"]["allowed_peers"] == ["示例好友"]
    assert not isolated_policy.exists()


def test_api_policy_rejects_unknown_action(live_ui):
    _server, url = live_ui
    status, data = _post(url, "/api/policy", {"action": "开个口子"})
    assert status == 400 and data["code"] == 1400


def test_page_script_keeps_js_escapes():
    """模板必须是 raw 字符串：`"\\n"` 被 Python 提前转义，整段 <script> 会语法错误。

    这个 bug 的表现很阴——页面 HTTP 200，但所有卡片都是空的（脚本第一行就抛）。
    """
    from arklink.webui import _PAGE

    assert 'join("\\n")' in _PAGE
    assert 'split("\\n")' in _PAGE
    assert "{{TITLE}}" in _PAGE


def test_page_script_keeps_js_escapes():
    """模板必须是 raw 字符串：`"\n"` 被 Python 提前转义，整段 <script> 会语法错误。

    这个 bug 的表现很阴——页面 HTTP 200，但所有卡片都是空的（脚本第一行就抛）。
    """
    from arklink.webui import _PAGE

    nl = chr(92) + "n"  # 字面量「反斜杠 + n」，不是换行
    assert f'join("{nl}")' in _PAGE
    assert f'split("{nl}")' in _PAGE
    assert "{{TITLE}}" in _PAGE
