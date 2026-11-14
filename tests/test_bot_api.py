"""bot 运行时：消息总线 / HTTP 接口 / SDK / 回调层。

这组测试守住「对外承诺」：
1. 接口形状稳定（别人照着写代码不会因为改版而断）；
2. 不支持的能力必须**说清缺什么**，而不是含糊报错；
3. 出厂空白名单 = 拒绝，而不是「不限」。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from arklink import Client
from arklink.api import ApiServer
from arklink.bot import Bot, load_bot
from arklink.bus import MessageBus
from arklink.caps import capabilities
from arklink.client import ApiError
from arklink.ntqq.send import DryRunEngine, SendService
from arklink.ntqq.sendpolicy import SendPolicy

TOKEN = "t0ken"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ARKLINK_POLICY", str(tmp_path / "policy.json"))
    monkeypatch.setenv("ARKLINK_PEERS", str(tmp_path / "peers.json"))
    monkeypatch.setenv("ARKLINK_API_STATE", str(tmp_path / "api.json"))
    return tmp_path


@pytest.fixture()
def service():
    policy = SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",))
    return SendService(policy, DryRunEngine())


@pytest.fixture()
def server(service):
    bus = MessageBus()
    srv = ApiServer(service, bus, port=0, token=TOKEN, state_file=False)
    url = srv.start()
    try:
        yield srv, url.rstrip("/"), bus
    finally:
        srv.stop()


def _req(url: str, path: str, *, method: str = "GET", payload: dict | None = None, token: str = TOKEN):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


# ---------------------------------------------------------------- 总线
def test_bus_normalizes_group_and_c2c():
    bus = MessageBus()
    c2c = bus.publish({"text": "你好", "user_id": "20002", "nickname": "测试对象"})
    assert c2c is not None and c2c.chat_type == 1 and c2c.peer_uid == "20002"
    group = bus.publish({"text": "群里", "group_id": "505", "group_name": "某群", "user_id": "20003"})
    assert group is not None and group.chat_type == 2 and group.peer_uid == "505"


def test_bus_dedupes_same_message_id():
    bus = MessageBus()
    assert bus.publish({"text": "a", "message_id": "x1"}) is not None
    assert bus.publish({"text": "a", "message_id": "x1"}) is None
    assert bus.stats()["duplicates"] == 1


def test_bus_ring_buffer_and_since():
    bus = MessageBus(maxlen=2)
    for i in range(4):
        bus.publish({"text": f"m{i}", "message_id": f"id{i}"})
    stats = bus.stats()
    assert stats["buffered"] == 2 and stats["dropped"] == 2 and stats["total"] == 4
    assert [m["text"] for m in bus.since(0)] == ["m2", "m3"]
    assert [m["text"] for m in bus.since(3)] == ["m3"]


def test_bus_segments_to_text():
    bus = MessageBus()
    msg = bus.publish({"message": [
        {"type": "at", "data": {"qq": "20002"}},
        {"type": "text", "data": {"text": " 看这个"}},
        {"type": "image", "data": {"file": "a.png"}},
    ]})
    assert msg is not None
    assert "@20002 看这个[图片]" == msg.text


# ---------------------------------------------------------------- HTTP
def test_health_needs_no_token(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/health", token="")
    assert status == 200 and data["ok"] and data["auth"] is True


def test_business_endpoints_require_token(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/status", token="")
    assert status == 401 and data["code"] == 401


def test_status_shape(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/status")
    assert status == 200
    for key in ("engine", "available", "limits", "queue", "bus", "policy"):
        assert key in data


def test_send_single_and_multi(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/send", method="POST",
                        payload={"to": "测试对象", "text": "你好"})
    assert status == 200 and data["count"] == 1 and data["items"][0]["ok"]

    status, data = _req(url, "/api/v1/send", method="POST",
                        payload={"to": ["测试对象"], "text": ["一", "二"]})
    assert status == 200 and data["count"] == 2 and data["lines"] == 2


def test_send_rejects_unsupported_segment_with_reason(service, isolated):
    """UI 通道发不了真 @：要回 1404 + 缺什么，而不是发出去一段「@名字」文字。

    这里让引擎自称 `host`（能力矩阵按通道名查表），但不真操作界面。
    """
    service.engine = _HostNamedEngine()
    bus = MessageBus()
    srv = ApiServer(service, bus, port=0, token=TOKEN, state_file=False)
    url = srv.start().rstrip("/")
    try:
        status, data = _req(url, "/api/v1/send", method="POST",
                            payload={"to": "测试对象", "message": [{"type": "at", "data": {"qq": "1"}}]})
    finally:
        srv.stop()
    assert status == 400 and data["code"] == 1404
    assert data["need"] and "真 @" in data["msg"]


def test_send_rejects_outsider(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/send", method="POST", payload={"to": "路人", "text": "hi"})
    assert status == 400 and data["code"] == 1404


def test_inbound_then_messages_and_stream(server):
    _srv, url, bus = server
    status, data = _req(url, "/api/v1/inbound", method="POST",
                        payload={"text": "来了", "user_id": "20002", "message_id": "a1"})
    assert status == 200 and data["accepted"] == 1 and data["last_seq"] == 1

    # 同一条再推一次 = 去重，不算新消息
    status, data = _req(url, "/api/v1/inbound", method="POST",
                        payload={"text": "来了", "user_id": "20002", "message_id": "a1"})
    assert data["accepted"] == 0 and data["duplicates"] == 1

    status, data = _req(url, "/api/v1/messages?since=0")
    assert status == 200 and data["count"] == 1 and data["items"][0]["text"] == "来了"

    # SSE：起一个线程收一条，再推一条
    got: list[dict] = []

    def read_one():
        req = urllib.request.Request(url + "/api/v1/stream?since=1",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        resp = urllib.request.urlopen(req, timeout=15)
        try:
            event, payload = "", []
            while True:
                line = resp.readline().decode().rstrip("\r\n")
                if not line:
                    if payload:
                        if event == "message":
                            got.append(json.loads("\n".join(payload)))
                            return
                        payload = []  # hello 之类的握手事件跳过，继续等真正的消息
                        continue
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    payload.append(line[5:].strip())
        finally:
            resp.close()

    thread = threading.Thread(target=read_one, daemon=True)
    thread.start()
    _req(url, "/api/v1/inbound", method="POST",
         payload={"text": "第二条", "user_id": "20002", "message_id": "a2"})
    thread.join(timeout=10)
    assert got and got[0]["text"] == "第二条"


def test_capabilities_reports_missing_with_need(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/capabilities")
    assert status == 200 and "send.text" in data["ok"]
    # 演练通道不真发，验证的是接口形状，所以什么都算走得通
    assert data["missing"] == []


def test_caps_on_ui_channel_are_honest():
    """UI 通道的真实能力：附件能发（剪贴板粘贴），真 @/撤回/历史不行。"""
    data = capabilities("host")
    assert "image" in data["ok"] and "file" in data["ok"]
    assert "at" in data["missing"] and "recall" in data["missing"]
    at = next(i for i in data["items"] if i["key"] == "at")
    assert at["need"]


def test_self_friends_groups(server, isolated):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/self")
    assert status == 200 and data["uin"] == "10001"

    status, data = _req(url, "/api/v1/friends")
    assert status == 200 and data["count"] == 0 and "roster --save-peers" in data["note"]

    (isolated / "peers.json").write_text(json.dumps({"peers": [
        {"chat_type": 2, "peer_uid": "505", "name": "某群"},
        {"chat_type": 1, "peer_uid": "u_1", "name": "测试对象"},
    ]}), encoding="utf-8")
    status, data = _req(url, "/api/v1/groups")
    assert status == 200 and data["count"] == 1 and data["items"][0]["name"] == "某群"


def test_history_recall_mute_are_explicitly_unsupported(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/history")
    assert status == 501 and data["code"] == 1404 and data["need"]

    for path, payload in (("/api/v1/recall", {"message_id": "1"}),
                          ("/api/v1/group/mute", {"group": "505", "user": "20002"}),
                          ("/api/v1/group/kick", {"group": "505", "user": "20002"})):
        status, data = _req(url, path, method="POST", payload=payload)
        assert status == 400 and data["code"] == 1404 and data["need"]


def test_policy_endpoint(server):
    _srv, url, _bus = server
    status, data = _req(url, "/api/v1/policy", method="POST",
                        payload={"action": "allow", "values": ["505"], "group": True})
    assert status == 200 and "505" in data["policy"]["allowed_peers"]


# ---------------------------------------------------------------- SDK
def test_client_roundtrip(server):
    _srv, url, _bus = server
    bot = Client(url, TOKEN)
    assert bot.health()["ok"]
    result = bot.send("测试对象", "你好")
    assert result["ok"] and result["count"] == 1
    assert bot.messages()["count"] == 0  # 发出去的消息不进收件箱
    bot.inbound({"text": "回来了", "user_id": "20002", "message_id": "s1"})
    assert bot.messages()["items"][0]["text"] == "回来了"
    assert "send.text" in bot.capabilities()["ok"]


def test_client_raises_with_actionable_message(server):
    _srv, url, _bus = server
    bot = Client(url, TOKEN)
    with pytest.raises(ApiError) as exc:
        bot.send("路人", "hi")
    assert exc.value.code == 1404 and "peers allow" in exc.value.msg


# ---------------------------------------------------------------- Bot 层
def test_bot_replies_on_string_return(service):
    bot = Bot(Client("http://127.0.0.1:1", ""))
    bot.client = _FakeClient()
    bot.on_message(lambda msg: f"收到：{msg['text']}")
    results = bot.handle({"text": "在吗", "chat_type": 1, "peer_uid": "测试对象", "sender_uid": "20002"})
    assert bot.stats["replied"] == 1
    assert results[0]["text"] == "收到：在吗"


def test_bot_skips_self_and_prefix(service):
    bot = Bot(_FakeClient(), prefix="/")
    bot._self_uins = {"10001"}
    bot.on_message(lambda msg: "不该被回复")
    assert bot.handle({"text": "/help", "sender_uid": "10001"}) == []
    assert bot.handle({"text": "help", "sender_uid": "20002"}) == []
    assert bot.stats["skipped"] == 2


def test_bot_survives_handler_exception(_=None):
    bot = Bot(_FakeClient())
    calls: list[str] = []

    def boom(msg):
        raise RuntimeError("坏了")

    def ok(msg):
        calls.append(msg["text"])
        return None

    bot.on_message(boom)
    bot.on_message(ok)
    bot.handle({"text": "hi", "sender_uid": "20002"})
    assert bot.stats["errors"] == 1 and calls == ["hi"]


def test_bot_reply_failure_does_not_crash():
    class Denying(_FakeClient):
        def send(self, *a, **kw):
            raise ApiError(400, 1404, "不在白名单里")

    bot = Bot(Denying())
    bot.on_message(lambda msg: "回复")
    out = bot.handle({"text": "hi", "sender_uid": "20002", "peer_uid": "路人"})
    assert out[0]["ok"] is False and bot.stats["errors"] == 1


def test_load_bot_reads_module_bot(tmp_path):
    path = tmp_path / "mybot.py"
    path.write_text(
        "from arklink.bot import Bot\nbot = Bot()\nbot.on_message(lambda m: 'x')\n",
        encoding="utf-8",
    )
    loaded = load_bot(str(path), client=_FakeClient())
    assert len(loaded.handlers) == 1


def test_caps_have_no_green_lies():
    """凡写 true 的都要有真实实现路径；剪贴板够不到的能力必须留在 missing。"""
    data = capabilities("host")
    assert "recall" in data["missing"] and "history" in data["missing"]
    assert "send.text" in data["ok"]


class _HostNamedEngine(DryRunEngine):
    """自称 `host` 的演练引擎：让能力矩阵按 UI 通道查表，但不碰真实界面。"""

    name = "host"


class _FakeClient:
    """替身：不连网络，只记录发了什么。"""

    base_url = "http://fake"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, to, text, *, group=False, dry_run=False):
        self.sent.append({"to": to, "text": text, "group": group})
        return {"ok": True, "text": text}

    def status(self):
        return {"limits": {"allowed_uins": ["10001"], "allowed_peers": ["测试对象"]}}

    def messages(self, since=0, limit=100):
        return {"items": [], "last_seq": 0}

    def stream(self, since=0, **kw):
        return iter(())


# ---------------------------------------------------------------- 附件

def test_attachment_must_be_local_file(tmp_path):
    """剪贴板只能粘本机文件：地址、缺字段、文件不存在都要在入口拦住。"""
    from arklink.api import attachment_problem

    real = tmp_path / "a.png"
    real.write_bytes(b"\x89PNG")
    assert attachment_problem([{"type": "image", "data": {"file": str(real)}}]) is None

    kind, why = attachment_problem([{"type": "image", "data": {"url": "https://x/a.png"}}])
    assert kind == "image" and "地址" in why

    kind, why = attachment_problem([{"type": "file", "data": {}}])
    assert kind == "file" and "缺少 file" in why

    kind, why = attachment_problem([{"type": "file", "data": {"file": str(tmp_path / "nope.pdf")}}])
    assert kind == "file" and "找不到" in why


def test_send_accepts_local_attachment_on_ui_channel(service, tmp_path):
    """UI 通道带本地图片应该被放行（不再一律 1404）。"""
    service.engine = _HostNamedEngine()
    real = tmp_path / "a.png"
    real.write_bytes(b"\x89PNG")
    bus = MessageBus()
    srv = ApiServer(service, bus, port=0, token=TOKEN, state_file=False)
    url = srv.start().rstrip("/")
    try:
        status, data = _req(url, "/api/v1/send", method="POST", payload={
            "to": "测试对象",
            "message": [
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"file": str(real)}},
            ],
        })
    finally:
        srv.stop()
    assert status == 200 and data["ok"] is True
    assert data["items"][0]["text"] == "看图"


# ---------------------------------------------------------------- 附件

def test_attachment_must_be_local_file(tmp_path):
    """剪贴板只能粘本机文件：地址、缺字段、文件不存在都要在入口拦住。"""
    from arklink.api import attachment_problem

    real = tmp_path / "a.png"
    real.write_bytes(b"\x89PNG")
    assert attachment_problem([{"type": "image", "data": {"file": str(real)}}]) is None

    kind, why = attachment_problem([{"type": "image", "data": {"url": "https://x/a.png"}}])
    assert kind == "image" and "地址" in why

    kind, why = attachment_problem([{"type": "file", "data": {}}])
    assert kind == "file" and "缺少 file" in why

    kind, why = attachment_problem([{"type": "file", "data": {"file": str(tmp_path / "nope.pdf")}}])
    assert kind == "file" and "找不到" in why


def test_send_accepts_local_attachment_on_ui_channel(service, tmp_path):
    """UI 通道带本地图片应该被放行（不再一律 1404）。"""
    service.engine = _HostNamedEngine()
    real = tmp_path / "a.png"
    real.write_bytes(b"\x89PNG")
    bus = MessageBus()
    srv = ApiServer(service, bus, port=0, token=TOKEN, state_file=False)
    url = srv.start().rstrip("/")
    try:
        status, data = _req(url, "/api/v1/send", method="POST", payload={
            "to": "测试对象",
            "message": [
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"file": str(real)}},
            ],
        })
    finally:
        srv.stop()
    assert status == 200 and data["ok"] is True
    assert data["items"][0]["text"] == "看图"
