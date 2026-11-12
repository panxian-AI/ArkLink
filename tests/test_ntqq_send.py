"""发送链路测试：闸门（策略）、解析（元素）、编排（SendService / 引擎）。

不触碰真实窗口：引擎用 DryRunEngine / 假 host 打桩。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from arklink.ntqq import elements as el
from arklink.ntqq import send as S
from arklink.ntqq.sendpolicy import (
    DEFAULT_PEERS,
    DEFAULT_UINS,
    Peer,
    SendPolicy,
    save_peers,
)


# ---------------------------------------------------------------- 策略闸门


def _policy(tmp_path, **cfg) -> SendPolicy:
    policy = SendPolicy.from_config(cfg, load=False)
    policy.peers_path_override = None  # 不读用户真实文件
    return policy


def test_defaults_are_empty_on_purpose():
    """出厂空白名单：仓库里不含任何人的账号，别人 clone 下来不会误发。"""
    assert DEFAULT_UINS == ()
    assert DEFAULT_PEERS == ()
    assert SendPolicy().allow_group is False
    # 空 = 拒绝，不是「不限」
    assert not SendPolicy().check_account("10001").ok
    assert not SendPolicy().resolve("测试对象").ok


def test_account_gate():
    policy = SendPolicy(allowed_uins=("10001",))
    assert policy.check_account("10001").ok
    d = policy.check_account("1000000002")
    assert not d.ok and d.code == 1403
    d = policy.check_account("")
    assert not d.ok and d.code == 1403


def test_peer_gate_whitelist_only():
    policy = SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",))
    ok = policy.resolve("测试对象")
    assert ok.ok and ok.peer is not None
    # 白名单自举：没扫花名册也能放行，但只放行白名单里点名的
    assert ok.peer.peer_uid == "测试对象" and ok.peer.chat_type == 1
    d = policy.resolve("随便谁")
    assert not d.ok and d.code == 1404


def test_peer_gate_uses_peers_file(tmp_path, monkeypatch):
    path = tmp_path / "peers.json"
    save_peers([Peer(chat_type=1, peer_uid="u_abc", uin="10000", name="测试对象")], path)
    monkeypatch.setenv("ARKLINK_PEERS", str(path))
    policy = SendPolicy.from_config({"allowed_peers": ("测试对象",)})
    d = policy.resolve("测试对象")
    assert d.ok and d.peer.peer_uid == "u_abc"
    d2 = policy.resolve("10000")
    assert d2.ok and d2.peer.peer_uid == "u_abc"


def test_group_gate_closed_by_default():
    policy = SendPolicy(allowed_uins=("10001",), allowed_peers=("505群",))
    d = policy.resolve(Peer(chat_type=2, peer_uid="505群"))
    assert not d.ok and d.code == 1403 and "群聊" in d.reason


def test_describe_mentions_boundaries():
    text = SendPolicy().describe()
    assert "未配置" in text and "关闭" in text
    configured = SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)).describe()
    assert "10001" in configured and "测试对象" in configured


# ---------------------------------------------------------------- 消息段解析


def test_parse_plain_string():
    els = el.parse_message("你好")
    assert [e.kind for e in els] == ["text"] and els[0].text == "你好"


def test_parse_cq_and_segments():
    cq = el.parse_message("[CQ:at,qq=123] 在吗")
    assert cq[0].kind == "at" and cq[0].qq == "123"
    segs = el.parse_message([{"type": "text", "data": {"text": "hi"}}])
    assert segs[0].kind == "text"


def test_validate_rejects_unsupported_and_empty():
    with pytest.raises(el.MessageError):
        el.validate([])
    with pytest.raises(el.MessageError):
        el.validate([el.Element(kind="image", file="x.png")])
    with pytest.raises(el.MessageError):
        el.validate([el.Element(kind="text", text="  ")])


def test_validate_passes_text_at_face():
    el.validate([el.Element(kind="text", text="hi"), el.Element(kind="at", qq="1")])


# ---------------------------------------------------------------- SendService


class RecordingEngine:
    name = "recording"

    def __init__(self) -> None:
        self.requests: list[S.SendRequest] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, request: S.SendRequest) -> S.SendResult:
        self.requests.append(request)
        return S.SendResult(ok=True, engine=self.name, text=request.text)


def test_service_checks_account_before_engine():
    engine = RecordingEngine()
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), engine)
    r = asyncio.run(service.send("测试对象", "hi", uin="1000000002"))
    assert not r.ok and r.code == 1403 and not engine.requests


def test_service_rejects_unsupported_segments():
    engine = RecordingEngine()
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), engine)
    r = asyncio.run(service.send("测试对象", [{"type": "image", "data": {"file": "x"}}], uin="10001"))
    assert not r.ok and r.code == 1400 and not engine.requests


def test_service_happy_path_carries_peer_name():
    engine = RecordingEngine()
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), engine)
    r = asyncio.run(service.send("测试对象", "hello", uin="10001"))
    assert r.ok
    req = engine.requests[0]
    assert req.search_key == "测试对象" and req.chat_type == S.CHAT_C2C
    assert r.extra["elapsed_ms"] >= 0


def test_service_group_without_allow_is_blocked():
    engine = RecordingEngine()
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("123",)), engine)
    r = asyncio.run(service.send("123", "hi", uin="10001", chat_type=S.CHAT_GROUP))
    assert not r.ok and r.code == 1403 and not engine.requests


# ---------------------------------------------------------------- 引擎


def test_dry_run_engine_records():
    engine = S.DryRunEngine()
    req = S.SendRequest(chat_type=1, peer_uid="测试对象", elements=[el.Element(kind="text", text="hi")])
    r = asyncio.run(engine.send(req))
    assert r.ok and r.dry_run and len(engine.sent) == 1


def test_host_engine_wraps_client():
    class FakeInfo:
        running = True

    class FakeHost:
        uin = "10001"
        last_action = "ok"

        def detect(self):
            i = FakeInfo()
            i.uin = "10001"
            return i

        def open_and_send(self, peer, text):
            self.called = (peer, text)
            return True

    host = FakeHost()
    engine = S.HostEngine(host=host)
    req = S.SendRequest(
        chat_type=1, peer_uid="u_x", elements=[el.Element(kind="text", text="hi")],
        peer_name="测试对象",
    )
    r = asyncio.run(engine.send(req))
    assert r.ok and host.called == ("测试对象", "hi")


def test_host_engine_refuses_wrong_account():
    class FakeInfo:
        running = True
        uin = "1000000002"

    class FakeHost:
        uin = "10001"
        last_action = ""

        def detect(self):
            return FakeInfo()

    engine = S.HostEngine(host=FakeHost())
    req = S.SendRequest(chat_type=1, peer_uid="u_x", elements=[el.Element(kind="text", text="hi")])
    r = asyncio.run(engine.send(req))
    assert not r.ok and r.code == 1403 and "1000000002" in r.msg


def test_host_engine_refuses_unidentified_account():
    class FakeInfo:
        running = True
        uin = ""

    class FakeHost:
        uin = "10001"
        last_action = ""

        def detect(self):
            return FakeInfo()

    engine = S.HostEngine(host=FakeHost())
    req = S.SendRequest(chat_type=1, peer_uid="u_x", elements=[el.Element(kind="text", text="hi")])
    r = asyncio.run(engine.send(req))
    assert not r.ok and r.code == 1403 and "认不出" in r.msg


def test_engine_from_config_names():
    assert isinstance(S.engine_from_config("dry-run"), S.DryRunEngine)
    assert isinstance(S.engine_from_config("host"), S.HostEngine)
    assert isinstance(S.engine_from_config("unknown"), S.HostEngine)  # auto 兜底
    assert isinstance(S.engine_from_config("pipe", client=None), S.PipeEngine)


def test_pipe_engine_unavailable_without_client():
    engine = S.PipeEngine(None)
    req = S.SendRequest(chat_type=1, peer_uid="x", elements=[])
    r = asyncio.run(engine.send(req))
    assert not r.ok and r.code == 1404


def test_send_result_dict_is_jsonable():
    r = S.SendResult(ok=True, message_id=1, engine="host", extra={"peer": "测试对象"})
    json.dumps(r.as_dict(), ensure_ascii=False)


# ---------------------------------------------------------------- 并发 / 突发


def test_send_many_keeps_order():
    engine = RecordingEngine()
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("a", "b", "c")), engine)
    results = asyncio.run(service.send_many(["a", "b", "c"], "hi", uin="10001"))
    assert [r.ok for r in results] == [True, True, True]
    assert [req.search_key for req in engine.requests] == ["a", "b", "c"]


def test_failure_between_batch_items_does_not_stop_others():
    engine = RecordingEngine()
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("a", "c")), engine)
    results = asyncio.run(service.send_many(["a", "泄露", "c"], "hi", uin="10001"))
    assert [r.ok for r in results] == [True, False, True]
    assert results[1].code == 1404


def test_host_engine_serializes_burst():
    """突发 6 条同时进来，窗口操作必须一条一条来（并发度恒为 1）。"""
    import threading

    class FakeHost:
        uin = "10001"
        last_action = "ok"

        def __init__(self):
            self.active = 0
            self.peak = 0
            self.calls = []
            self.guard = threading.Lock()

        def detect(self):
            class Info:
                running = True
                uin = "10001"

            return Info()

        def open_and_send(self, peer, text):
            with self.guard:
                self.active += 1
                self.peak = max(self.peak, self.active)
            import time as _t

            _t.sleep(0.03)
            with self.guard:
                self.active -= 1
                self.calls.append((peer, text))
            return True

    host = FakeHost()
    engine = S.HostEngine(host=host, min_gap=0.0)
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), engine)

    async def burst():
        return await asyncio.gather(*[service.send("测试对象", f"第{i}条", uin="10001") for i in range(6)])

    results = asyncio.run(burst())
    assert all(r.ok for r in results)
    assert host.peak == 1, f"窗口操作被并发执行了：peak={host.peak}"
    assert len(host.calls) == 6


def test_pending_and_queue_depth_reported():
    class SlowEngine(RecordingEngine):
        name = "slow"
        depth = 0

        async def send(self, request):
            await asyncio.sleep(0.01)
            return await super().send(request)

    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), SlowEngine())
    asyncio.run(service.send("测试对象", "hi", uin="10001"))
    st = service.status()
    assert st["pending"] == 0 and st["queue_depth"] == 0 and st["sent"] == 1


def test_history_is_bounded():
    service = S.SendService(SendPolicy(allowed_uins=("10001",), allowed_peers=("测试对象",)), RecordingEngine())
    for i in range(520):
        service._record(S.SendResult(ok=True, engine="x", text=str(i)))
    assert len(service.history) == 500
