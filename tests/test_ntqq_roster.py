"""好友 / 群列表：OIDB 信封、map 字段解析、cookie 翻页。"""

from __future__ import annotations

import asyncio

import pytest

from arklink.ntqq import native, pb, roster

FRIEND_UIN = 30001
GROUP_ID = 8001234


def sub_biz(nums: dict[int, int], texts: dict[int, str]) -> bytes:
    body = b""
    for key, value in nums.items():
        body += pb.bytes_field(1, pb.uint_field(1, key) + pb.uint_field(2, value))
    for key, value in texts.items():
        body += pb.bytes_field(2, pb.uint_field(1, key) + pb.string_field(2, value))
    return body


def friend_entry(uin: int, uid: str, nickname: str, remark: str = "") -> bytes:
    texts = {roster.BIZ_NICK: nickname}
    if remark:
        texts[roster.BIZ_REMARK] = remark
    biz = pb.uint_field(1, 1) + pb.message_field(2, sub_biz({roster.BIZ_AGE: 21}, texts))
    return (
        pb.string_field(1, uid)
        + pb.uint_field(2, 3)
        + pb.uint_field(3, uin)
        + pb.message_field(10001, biz)
    )


def friend_response(friends: list[bytes], *, cookie: bytes = b"", is_end: bool = True) -> bytes:
    category = pb.uint_field(1, 3) + pb.string_field(2, "同学") + pb.uint_field(3, len(friends))
    body = b"".join(pb.message_field(101, friend) for friend in friends)
    body += pb.message_field(102, category)
    if cookie:
        body += pb.bytes_field(2, cookie)
    body += pb.uint_field(3, 1 if is_end else 0)
    return roster.oidb(roster.FRIEND_COMMAND, roster.FRIEND_SUB, body)


def group_response() -> bytes:
    info = (
        pb.uint_field(2, 1600000000)
        + pb.uint_field(3, 500)
        + pb.uint_field(4, 42)
        + pb.string_field(5, "ArkBot 交流群")
        + pb.string_field(18, "一个群简介")
    )
    custom = pb.string_field(3, "备注群")
    entry = pb.uint_field(3, GROUP_ID) + pb.message_field(4, info) + pb.message_field(5, custom)
    return roster.oidb(roster.GROUP_COMMAND, roster.GROUP_SUB, pb.message_field(2, entry))


def test_oidb_envelope_roundtrip():
    raw = roster.oidb(0xFD4, 1, b"\x08\x01")
    parsed = roster.parse_oidb(raw)
    assert parsed["command"] == 0xFD4
    assert parsed["service"] == 1
    assert parsed["body"] == b"\x08\x01"


def test_friend_request_shape():
    raw = roster.build_friend_request(b"", req_count=300)
    parsed = roster.parse_oidb(raw)
    body = pb.decode(parsed["body"])
    assert body[2] == [300]
    assert body[6] == [1]
    assert 10001 in body  # ext_busi 两条
    assert len(body[10001]) == 2


def test_friend_request_carries_cookie():
    body = pb.decode(roster.parse_oidb(roster.build_friend_request(b"abc"))["body"])
    assert body[5] == [b"abc"]


def test_parse_friends_maps_names_and_remark():
    parsed = roster.parse_friends(
        roster.parse_oidb(friend_response([friend_entry(FRIEND_UIN, "u_abc", "阿伟", "伟哥")]))["body"]
    )
    assert len(parsed["friends"]) == 1
    friend = parsed["friends"][0]
    assert friend["user_id"] == FRIEND_UIN
    assert friend["uid"] == "u_abc"
    assert friend["nickname"] == "阿伟"
    assert friend["remark"] == "伟哥"
    assert friend["age"] == 21
    assert friend["category_id"] == 3
    assert parsed["categories"][0]["name"] == "同学"
    assert parsed["is_end"] is True


def test_parse_groups():
    groups = roster.parse_groups(roster.parse_oidb(group_response())["body"])
    assert groups[0]["group_id"] == GROUP_ID
    assert groups[0]["group_name"] == "ArkBot 交流群"
    assert groups[0]["member_count"] == 42
    assert groups[0]["max_member_count"] == 500
    assert groups[0]["description"] == "一个群简介"
    assert groups[0]["remark"] == "备注群"


def test_roster_client_paginates_on_cookie():
    calls: list[str] = []

    async def fake_call(service: str, body: bytes) -> bytes:
        calls.append(service)
        if service == roster.FRIEND_SERVICE:
            inner = pb.decode(roster.parse_oidb(body)["body"])
            if len(calls) == 1:
                assert inner.get(5) in (None, [b""])
                return friend_response([friend_entry(1, "u1", "甲")], cookie=b"page2", is_end=False)
            assert inner[5] == [b"page2"]      # 第二页要带上服务端给的 cookie
            return friend_response([friend_entry(2, "u2", "乙")], is_end=True)
        return group_response()

    client = roster.RosterClient(call=fake_call)
    friends = asyncio.run(client.friends())
    assert [friend["user_id"] for friend in friends] == [1, 2]
    assert len(calls) == 2

    groups = asyncio.run(client.groups())
    assert groups[0]["group_id"] == GROUP_ID


def test_roster_client_stops_when_cookie_repeats():
    calls = 0

    async def fake_call(service: str, body: bytes) -> bytes:
        nonlocal calls
        calls += 1
        return friend_response([friend_entry(1, "u1", "甲")], cookie=b"same", is_end=False)

    client = roster.RosterClient(call=fake_call, max_pages=5)
    asyncio.run(client.friends())
    assert calls == 2  # 第二页 cookie 没变就停，不会无限翻


def test_adapter_exposes_roster_via_call_api(monkeypatch):
    """`get_friend_list` / `get_group_list` 走只读花名册，而不是 1404。"""
    adapter = native.NativeQQAdapter(uin=1, transport=native.LoopbackTransport())

    async def fake_call_service(service: str, body: bytes) -> bytes:
        if service == roster.FRIEND_SERVICE:
            return friend_response([friend_entry(FRIEND_UIN, "u", "阿伟")])
        return group_response()

    monkeypatch.setattr(adapter, "call_service", fake_call_service)
    friends = asyncio.run(adapter.call_api("get_friend_list"))
    groups = asyncio.run(adapter.call_api("get_group_list"))
    assert friends[0]["user_id"] == FRIEND_UIN
    assert groups[0]["group_id"] == GROUP_ID

    with pytest.raises(Exception) as excinfo:
        asyncio.run(adapter.call_api("send_group_msg", {"group_id": 1, "message": "hi"}))
    assert "1404" in str(excinfo.value) or getattr(excinfo.value, "retcode", 0) == 1404


def test_read_only_channel_is_unhealthy_before_login():
    adapter = native.NativeQQAdapter(uin=1)
    assert adapter.is_connected is False
    assert adapter.supports_receive is False
