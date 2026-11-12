"""登录链路的密码学与帧封装：ECDH、NT 帧、适配器收推送。"""

from __future__ import annotations

import asyncio

import pytest

from arklink.ntqq import ecdh, native, ntframe, pb, push


def test_ecdh_selfcheck():
    report = ecdh.selftest()
    assert report["on_curve"] is True
    assert report["shared_equal"] is True
    assert report["key_len"] == 16
    assert report["order_ok"] is True


def test_ecdh_public_key_sizes_and_roundtrip():
    pair = ecdh.generate(0x1234567890ABCDEF)
    compressed = pair.pack(compress=True)
    assert len(compressed) == 25 and compressed[0] in (0x02, 0x03)
    assert len(pair.pack(compress=False)) == 49
    restored = ecdh.unpack_public(compressed)
    assert restored == pair.public
    assert ecdh.is_on_curve(restored)


def test_ecdh_rejects_off_curve_key():
    with pytest.raises(ValueError):
        ecdh.unpack_public(b"\x04" + b"\x01" * 48)


def test_ecdh_shared_key_is_symmetric_and_deterministic():
    alice = ecdh.generate(11111)
    bob = ecdh.generate(22222)
    key = alice.exchange(bob.pack())
    assert key == bob.exchange(alice.pack())
    assert key == alice.exchange(bob.pack())


def test_ntframe_selftest():
    report = ntframe.selftest()
    assert report["payload_ok"] and report["service_ok"] and report["empty_ok"]


def test_ntframe_requires_key_for_d2():
    with pytest.raises(ValueError):
        ntframe.pack(b"\x08\x01", service="OidbSvcTrpcTcp.0xfd4_1")


def test_ntframe_empty_encryption_roundtrip():
    frame = ntframe.pack(b"hello", service="wtlogin.trans_emp", encrypt_type=ntframe.ENC_EMPTY)
    parsed = ntframe.unpack(frame)
    assert parsed.payload == b"hello"
    assert parsed.service == "wtlogin.trans_emp"


def test_adapter_consumes_push_frame_and_emits_event():
    """推送帧 → OneBot 事件 → 交给框架，整条链路。"""
    elems = pb.message_field(2, pb.message_field(1, pb.string_field(1, "收到")))
    routing = pb.uint_field(1, 10086) + pb.message_field(8, pb.uint_field(1, 90001))
    content = pb.uint_field(1, 1) + pb.uint_field(5, 3) + pb.uint_field(6, 1700000002)
    common = pb.message_field(1, routing) + pb.message_field(2, content) + pb.message_field(3, pb.message_field(1, elems))
    body = pb.message_field(1, common)

    key = bytes(range(16))
    frame = ntframe.pack(body, service="trpc.msg.olpush.OlPushService.MsgPush", uin=1, key=key)

    adapter = native.NativeQQAdapter(uin=1, transport=native.LoopbackTransport())
    adapter.keystore.tickets[0x10D] = b""      # d2 为空时不走 D2 解密
    adapter.d2_key  # 触发属性（空）
    adapter.session_key = key

    async def run():
        received: list[object] = []

        async def handler(event):
            received.append(event)

        adapter.on_event(handler)
        # 用 EMPTY 加密的帧不需要 D2 密钥，直接走 unpack 分支
        adapter._consume(ntframe.pack(body, service="trpc.msg.olpush.OlPushService.MsgPush",
                                      encrypt_type=ntframe.ENC_EMPTY))
        await asyncio.sleep(0.05)
        return received

    events = asyncio.run(run())
    assert events, "推送没有被翻译成事件"
    event = events[0]
    assert event.group_id == 90001
    assert event.message_type == "group"
    assert event.text == "收到" and event.raw_message == "收到"


def test_adapter_readonly_rejects_send(monkeypatch):
    adapter = native.NativeQQAdapter(uin=1)

    async def run():
        with pytest.raises(Exception) as excinfo:
            await adapter.call_api("send_private_msg", {"user_id": 1, "message": "hi"})
        return excinfo.value

    error = asyncio.run(run())
    assert getattr(error, "retcode", 0) == 1404


def test_push_to_onebot_type_alias():
    """`push.to_onebot` 对垃圾字节不抛异常，只是没有事件。"""
    assert push.to_onebot(b"\xff\xff\xff") is None
