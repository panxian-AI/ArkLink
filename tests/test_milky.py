"""Milky 协议映射测试：段、事件、API 调用（对假协议端）。"""

from __future__ import annotations

import asyncio

import pytest

from arklink.milky import (
    MilkyAdapter,
    MilkyClient,
    event_to_raw,
    onebot_to_segments,
    segments_to_onebot,
)
from fakes.fake_milky import GROUP_ID, SELF_ID, USER_ID, FakeMilky


# ---------------------------------------------------------------- 段映射


def test_incoming_segments_full_coverage():
    segments = [
        {"type": "mention", "data": {"user_id": SELF_ID, "name": "bot"}},
        {"type": "text", "data": {"text": " 你好"}},
        {"type": "mention_all", "data": {}},
        {"type": "face", "data": {"face_id": 14}},
        {"type": "reply", "data": {"message_seq": 77, "sender_id": USER_ID, "sender_name": "阿鱼"}},
        {"type": "image", "data": {"resource_id": "res1", "temp_url": "https://x/1.png", "width": 10}},
        {"type": "record", "data": {"resource_id": "res2", "temp_url": "", "duration": 3}},
        {"type": "video", "data": {"resource_id": "res3", "temp_url": "https://x/3.mp4"}},
        {"type": "file", "data": {"file_id": "f1", "file_name": "a.txt", "file_size": 9}},
        {"type": "forward", "data": {"forward_id": "fw1", "title": "聊天记录"}},
        {"type": "market_face", "data": {"summary": "[表情]", "url": "https://x/e.gif"}},
        {"type": "light_app", "data": {"app_name": "x", "json_payload": "{}"}},
        {"type": "xml", "data": {"service_id": 1, "xml_payload": "<xml/>"}},
        {"type": "markdown", "data": {"content": "# hi"}},
    ]
    out = segments_to_onebot(segments)
    types = [s["type"] for s in out]
    assert types == [
        "at", "text", "at", "face", "reply", "image", "record",
        "video", "file", "forward", "mface", "json", "xml", "text",
    ]
    assert out[0]["data"]["qq"] == str(SELF_ID)
    assert out[2]["data"]["qq"] == "all"
    assert out[4]["data"]["id"] == "77"
    assert out[5]["data"]["file"] == "https://x/1.png"
    assert out[5]["data"]["resource_id"] == "res1"
    assert out[6]["data"]["file"] == "res2"       # 无 temp_url 时退回 resource_id
    assert out[13]["data"]["text"] == "# hi"


def test_outgoing_segments_mapping():
    message = [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "at", "data": {"qq": str(USER_ID)}},
        {"type": "at", "data": {"qq": "all"}},
        {"type": "face", "data": {"id": "14"}},
        {"type": "reply", "data": {"id": "77"}},
        {"type": "image", "data": {"file": "https://x/1.png"}},
        {"type": "record", "data": {"file": "a.amr"}},
        {"type": "video", "data": {"file": "a.mp4"}},
        {"type": "unknown_seg", "data": {}},
    ]
    out = onebot_to_segments(message)
    assert out == [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "mention", "data": {"user_id": USER_ID}},
        {"type": "mention_all", "data": {}},
        {"type": "face", "data": {"face_id": 14}},
        {"type": "reply", "data": {"message_seq": 77}},
        {"type": "image", "data": {"uri": "https://x/1.png"}},
        {"type": "record", "data": {"uri": "a.amr"}},
        {"type": "video", "data": {"uri": "a.mp4"}},
    ]


def test_outgoing_accepts_plain_string():
    assert onebot_to_segments("你好") == [{"type": "text", "data": {"text": "你好"}}]


# ---------------------------------------------------------------- 事件映射


def test_group_message_event():
    raw = event_to_raw(FakeMilky.group_message("/echo hi"))
    assert raw["post_type"] == "message"
    assert raw["message_type"] == "group"
    assert raw["group_id"] == GROUP_ID
    assert raw["user_id"] == USER_ID
    assert raw["message_id"] == 111
    assert raw["raw_message"].strip() == "/echo hi"
    assert raw["sender"]["role"] == "admin"
    assert raw["sender"]["card"] == "鱼"
    assert raw["native"]["event_type"] == "message_receive"


def test_friend_message_event():
    raw = event_to_raw(FakeMilky.friend_message("hi"))
    assert raw["message_type"] == "private"
    assert raw["sub_type"] == "friend"
    assert raw["user_id"] == USER_ID
    assert raw["sender"]["remark"] == "鱼"


def test_recall_event():
    raw = event_to_raw({
        "time": 1, "self_id": SELF_ID, "event_type": "message_recall",
        "data": {"message_scene": "group", "peer_id": GROUP_ID, "message_seq": 5,
                 "sender_id": USER_ID, "operator_id": 999, "display_suffix": ""},
    })
    assert raw["post_type"] == "notice"
    assert raw["notice_type"] == "group_recall"
    assert raw["group_id"] == GROUP_ID and raw["operator_id"] == 999


def test_unknown_event_passes_through():
    raw = event_to_raw({"time": 1, "self_id": SELF_ID, "event_type": "group_name_change",
                        "data": {"group_id": GROUP_ID, "new_group_name": "新名"}})
    assert raw["notice_type"] == "group_name_change"
    assert raw["native"]["data"]["new_group_name"] == "新名"


# ---------------------------------------------------------------- API


def test_send_and_recall_against_fake_server():
    async def run():
        fake = await FakeMilky().start()
        try:
            adapter = MilkyAdapter(base_url=fake.base_url, access_token=fake.token, event_url=fake.event_url)
            result = await adapter.call_api("send_group_msg", {
                "group_id": GROUP_ID,
                "message": [{"type": "reply", "data": {"id": "111"}},
                            {"type": "text", "data": {"text": "收到"}}],
            })
            assert result["message_id"] and result["message_id"] == result["message_seq"]

            endpoint, params = fake.calls[-1]
            assert endpoint == "send_group_message"
            assert params["group_id"] == GROUP_ID
            assert params["message"][1] == {"type": "text", "data": {"text": "收到"}}

            recall = await adapter.call_api("delete_msg", {"message_id": result["message_id"]})
            assert recall == {}
            assert fake.calls[-1][0] == "recall_group_message"
            assert fake.calls[-1][1]["message_seq"] == result["message_id"]
        finally:
            await fake.stop()

    asyncio.run(run())


def test_login_info_normalized():
    async def run():
        fake = await FakeMilky().start()
        try:
            adapter = MilkyAdapter(base_url=fake.base_url, access_token=fake.token)
            info = await adapter.call_api("get_login_info")
            assert info["user_id"] == SELF_ID and info["nickname"] == "ArkLink"
        finally:
            await fake.stop()

    asyncio.run(run())


def test_unknown_action_is_1404():
    async def run():
        fake = await FakeMilky().start()
        try:
            adapter = MilkyAdapter(base_url=fake.base_url, access_token=fake.token)
            with pytest.raises(Exception) as ei:
                await adapter.call_api("set_group_sign", {"group_id": 1})
            assert getattr(ei.value, "retcode", None) == 1404
        finally:
            await fake.stop()

    asyncio.run(run())


def test_auth_failure_and_missing_api():
    async def run():
        fake = await FakeMilky().start()
        try:
            bad = MilkyClient(fake.base_url, "wrong-token")
            with pytest.raises(Exception) as ei:
                await bad.call("get_login_info")
            assert getattr(ei.value, "retcode", None) == -401

            client = MilkyClient(fake.base_url, fake.token)
            with pytest.raises(Exception) as ei2:
                await client.call("no_such_api")
            assert getattr(ei2.value, "retcode", None) == 1404
        finally:
            await fake.stop()

    asyncio.run(run())
