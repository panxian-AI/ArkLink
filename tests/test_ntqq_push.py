"""推送消息 → OneBot 事件的映射。全部用自己拼的 protobuf，不联网。"""

from __future__ import annotations

from arklink.ntqq import pb, push

GROUP_ID = 7200123
SENDER = 10086


def elem_text(text: str) -> bytes:
    return pb.message_field(1, pb.string_field(1, text))


def elem_at(uin: int, name: str = "") -> bytes:
    body = pb.uint_field(1, 1) + pb.uint_field(2, uin)
    if name:
        body += pb.string_field(6, name)
    return pb.message_field(53, pb.uint_field(1, 1) + pb.message_field(2, body))


def elem_image(url: str, md5: bytes = b"\x01\x02") -> bytes:
    pic = pb.string_field(15, url) + pb.bytes_field(7, md5)
    return pb.message_field(4, pic)


def elem_reply(seq: int) -> bytes:
    return pb.message_field(45, pb.uint_field(1, seq))


def elem_face(index: int) -> bytes:
    return pb.message_field(2, pb.uint_field(1, index))


def build_push(
    *,
    elems: list[bytes],
    group_id: int = GROUP_ID,
    from_uin: int = SENDER,
    msg_type: int = 1,
    msg_id: int = 7001,
    seq: int = 42,
    group_name: str = "测试群",
    card: str = "",
) -> bytes:
    rich = b"".join(pb.message_field(2, elem) for elem in elems)
    body = pb.message_field(1, rich)
    group = pb.uint_field(1, group_id) + pb.string_field(7, group_name)
    if card:
        group += pb.string_field(4, card)
    routing = pb.uint_field(1, from_uin) + pb.message_field(8, group)
    content = (
        pb.uint_field(1, msg_type)
        + pb.uint_field(5, seq)
        + pb.uint_field(6, 1700000000)
        + pb.uint_field(12, msg_id)
    )
    common = pb.message_field(1, routing) + pb.message_field(2, content) + pb.message_field(3, body)
    return pb.message_field(1, common)


def build_private(elems: list[bytes], from_uin: int = 20001) -> bytes:
    rich = b"".join(pb.message_field(2, elem) for elem in elems)
    routing = pb.uint_field(1, from_uin) + pb.message_field(7, pb.uint_field(1, 2) + pb.string_field(6, "小明"))
    content = pb.uint_field(1, 1) + pb.uint_field(5, 7) + pb.uint_field(6, 1700000001)
    common = (
        pb.message_field(1, routing)
        + pb.message_field(2, content)
        + pb.message_field(3, pb.message_field(1, rich))
    )
    return pb.message_field(1, common)


def test_group_text_message():
    event = push.to_onebot(build_push(elems=[elem_text("你好，世界")]), self_id=1)
    assert event["post_type"] == "message"
    assert event["message_type"] == "group"
    assert event["group_id"] == GROUP_ID
    assert event["group_name"] == "测试群"
    assert event["user_id"] == SENDER
    assert event["message_id"] == 7001
    assert event["message"] == [{"type": "text", "data": {"text": "你好，世界"}}]
    assert event["raw_message"] == "你好，世界"


def test_group_with_at_image_reply():
    body = build_push(
        elems=[elem_reply(99), elem_at(12345, "老王"), elem_text(" 看图"), elem_image("https://gchat.qpic.cn/x.jpg")],
        card="机器人甲",
    )
    event = push.to_onebot(body, self_id=1)
    kinds = [segment["type"] for segment in event["message"]]
    assert kinds == ["reply", "at", "text", "image"]
    assert event["message"][0]["data"]["id"] == "99"
    assert event["message"][1]["data"]["qq"] == "12345"
    assert event["message"][1]["data"]["name"] == "老王"
    assert event["message"][3]["data"]["url"].startswith("https://gchat.qpic.cn")
    assert event["sender"]["card"] == "机器人甲"
    assert "[图片]" in event["raw_message"]


def test_at_all_when_uin_zero():
    at_all = pb.message_field(53, pb.uint_field(1, 1) + pb.message_field(2, pb.uint_field(1, 1)))
    event = push.to_onebot(build_push(elems=[at_all]), self_id=1)
    assert event["message"][0] == {"type": "at", "data": {"qq": "all", "name": "全体成员"}}


def test_face_segment():
    event = push.to_onebot(build_push(elems=[elem_face(178)]), self_id=1)
    assert event["message"][0]["type"] == "face"
    assert event["message"][0]["data"]["id"] == "178"
    assert event["raw_message"] == "[表情]"


def test_private_message():
    event = push.to_onebot(build_private([elem_text("私聊")]), self_id=1)
    assert event["message_type"] == "private"
    assert event["user_id"] == 20001
    assert event["raw_message"] == "私聊"


def test_recall_becomes_notice():
    body = build_push(elems=[], msg_type=push.MSG_TYPE_RECALL, seq=88, msg_id=0)
    event = push.to_onebot(body, self_id=1)
    assert event["post_type"] == "notice"
    assert event["notice_type"] == "group_recall"
    assert event["group_id"] == GROUP_ID
    assert event["message_id"] == 88


def test_unknown_elem_is_skipped_not_fatal():
    unknown = pb.message_field(60, pb.string_field(1, "未来新增的类型"))
    event = push.to_onebot(build_push(elems=[unknown, elem_text("还在")]), self_id=1)
    assert event["message"] == [{"type": "text", "data": {"text": "还在"}}]


def test_empty_payload_returns_none():
    assert push.to_onebot(b"") is None
    assert push.to_onebot(pb.uint_field(5, 1)) is None


def test_decode_segments_helper():
    event = push.to_onebot(build_push(elems=[elem_at(1, "甲"), elem_text("hi")]), self_id=1)
    assert push.decode_segments(event) == "@甲hi"
    assert '"message_type"' in push.json_of(event)
