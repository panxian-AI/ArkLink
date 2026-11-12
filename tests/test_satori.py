"""Satori 协议映射测试：XML 内容编解码、事件、API。"""

from __future__ import annotations

import asyncio

import pytest

from arklink.satori import (
    SatoriAdapter,
    encode_content,
    event_to_raw,
    parse_content,
    _stable_id,
)
from fakes.fake_satori import FakeSatori, GROUP_ID, SELF_ID, USER_ID


# ---------------------------------------------------------------- 内容编解码


def test_parse_content_elements():
    content = f'<quote id="90001"/><at id="{SELF_ID}"/> 你好 <img src="https://x/a.png"/>'
    segments = parse_content(content)
    assert [s["type"] for s in segments] == ["reply", "at", "text", "image"]
    assert segments[1]["data"]["qq"] == str(SELF_ID)
    assert segments[2]["data"]["text"] == " 你好 "
    assert segments[3]["data"]["file"] == "https://x/a.png"


def test_parse_content_escapes():
    assert parse_content("a &lt; b &amp;&amp; c &#91;x&#93;") == [
        {"type": "text", "data": {"text": "a < b && c [x]"}}
    ]


def test_encode_content():
    message = [
        {"type": "reply", "data": {"id": "90001"}},
        {"type": "at", "data": {"qq": str(USER_ID)}},
        {"type": "text", "data": {"text": "1 < 2 & ok"}},
        {"type": "image", "data": {"file": "https://x/a.png"}},
        {"type": "record", "data": {"file": "a.amr"}},
    ]
    content = encode_content(message)
    assert content.startswith('<quote id="90001"/><at id="30003"/>1 &lt; 2 &amp; ok')
    assert '<img src="https://x/a.png"/>' in content
    assert "<audio" in content


def test_content_roundtrip():
    original = [{"type": "text", "data": {"text": "hello"}}]
    assert parse_content(encode_content(original)) == original


def test_stable_id():
    assert _stable_id("12345") == 12345
    assert _stable_id("abc") == _stable_id("abc") != 0


# ---------------------------------------------------------------- 事件


def test_group_message_event():
    raw = event_to_raw(FakeSatori.group_message("hello"))
    assert raw["message_type"] == "group"
    assert raw["group_id"] == _stable_id(GROUP_ID)
    assert raw["user_id"] == _stable_id(USER_ID)
    assert raw["message"][0]["type"] == "at"
    assert raw["sender"]["nickname"] == "阿鱼"
    assert raw["satori_channel_id"] == GROUP_ID


def test_direct_message_event():
    raw = event_to_raw(FakeSatori.direct_message("hi"))
    assert raw["message_type"] == "private"
    assert raw["message_id"] == 90002
    assert "group_id" not in raw


def test_deleted_event():
    event = {
        "id": 3, "type": "message-deleted", "self_id": SELF_ID, "timestamp": 1,
        "channel": {"id": GROUP_ID, "type": 0}, "message": {"id": "90001"},
        "user": {"id": USER_ID},
    }
    raw = event_to_raw(event)
    assert raw["notice_type"] == "group_recall"
    assert raw["message_id"] == 90001


# ---------------------------------------------------------------- API


def test_send_and_recall_against_fake_sdk():
    async def run():
        fake = await FakeSatori().start()
        try:
            adapter = SatoriAdapter(
                api_base=fake.api_base, token=fake.token, events_url=fake.events_url
            )
            result = await adapter.call_api("send_group_msg", {
                "group_id": _stable_id(GROUP_ID),
                "message": [{"type": "text", "data": {"text": "收到"}}],
            })
            assert result["message_id"] > 0
            path, body = fake.calls[-1]
            assert path.endswith("message.create")
            assert body["channel_id"] == GROUP_ID
            assert body["content"] == "收到"

            await adapter.call_api("delete_msg", {"message_id": result["message_id"]})
            assert fake.calls[-1][0].endswith("message.delete")
            assert fake.calls[-1][1]["message_id"]
        finally:
            await fake.stop()

    asyncio.run(run())


def test_unsupported_api_is_1404():
    async def run():
        fake = await FakeSatori().start()
        try:
            adapter = SatoriAdapter(api_base=fake.api_base, token=fake.token)
            with pytest.raises(Exception) as ei:
                await adapter.call_api("set_group_kick", {"group_id": 1, "user_id": 2})
            assert getattr(ei.value, "retcode", None) == 1404
        finally:
            await fake.stop()

    asyncio.run(run())
