"""好友列表 / 群列表 —— 扫码登录后第一批「只读」能力。

两个接口都在 OIDB 里，外层是统一的信封：

```
Oidb { 1: command, 2: service, 3: result, 4: body, 5: message, 12: reserved }
```

| 能力 | 服务名 | 命令 | 说明 |
|------|--------|------|------|
| 好友 | `OidbSvcTrpcTcp.0xfd4_1` | 0xfd4 / 1 | 增量拉取，带 cookie 翻页（`IncPull`） |
| 群   | `OidbSvcTrpcTcp.0xfe5_2` | 0xfe5 / 2 | 一次全量，返回群资料 |

这两个接口的共同点：**只读**。不会改任何服务端状态，也不触发风控里的「行为异常」，
所以是验证「签名 / 加密 / ticket 是否真的对」的最短路径 —— 能拉出好友列表，
说明 keystore 里的 A2/D2 是好的，再去做发消息才有意义。

`RosterClient` 只依赖一个 `call(service, body) -> bytes`，所以：
- 单测里塞假实现，不联网；
- 真机上由 `native.NativeQQAdapter.call_service()` 接 SSO。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import pb

FRIEND_SERVICE = "OidbSvcTrpcTcp.0xfd4_1"
FRIEND_COMMAND = 0xFD4
FRIEND_SUB = 1

GROUP_SERVICE = "OidbSvcTrpcTcp.0xfe5_2"
GROUP_COMMAND = 0xFE5
GROUP_SUB = 2

#: 好友扩展字段里几个有用的 biz key
BIZ_REMARK = 103        # 备注
BIZ_NICK = 20002        # 昵称
BIZ_SIGN = 102          # 个性签名
BIZ_QID = 27394         # QID
BIZ_GENDER = 20009      # 性别
BIZ_AGE = 20037         # 年龄

CallFn = Callable[[str, bytes], Awaitable[bytes]]


# ------------------------------------------------------------------ protobuf 小工具

def packed_uint(values: list[int] | tuple[int, ...]) -> bytes:
    """repeated uint32 的 packed 编码（proto3 默认行为）。"""
    body = b"".join(pb.varint(int(v)) for v in values)
    return body


def _packed_field(field_no: int, values: list[int] | tuple[int, ...]) -> bytes:
    return pb.bytes_field(field_no, packed_uint(values))


def _map_int_int(field_no: int, mapping: dict[int, int]) -> bytes:
    """`map<int32,int32>` 在线上是 repeated {1: key, 2: value}。"""
    out = b""
    for key, value in mapping.items():
        entry = pb.uint_field(1, key) + pb.uint_field(2, value)
        out += pb.bytes_field(field_no, entry)
    return out


def _map_int_bytes(field_no: int, mapping: dict[int, bytes]) -> bytes:
    out = b""
    for key, value in mapping.items():
        entry = pb.uint_field(1, key) + pb.bytes_field(2, value)
        out += pb.bytes_field(field_no, entry)
    return out


# ------------------------------------------------------------------ OIDB 信封

def oidb(command: int, service: int, body: bytes = b"", *, result: int = 0) -> bytes:
    return (
        pb.uint_field(1, command)
        + pb.uint_field(2, service)
        + pb.uint_field(3, result)
        + pb.bytes_field(4, body)
    )


def parse_oidb(buf: bytes) -> dict[str, Any]:
    fields = pb.decode(buf)
    body = fields.get(4, [b""])[0]
    return {
        "command": int(fields.get(1, [0])[0]),
        "service": int(fields.get(2, [0])[0]),
        "result": int(fields.get(3, [0])[0]),
        "body": body if isinstance(body, bytes) else b"",
        "message": (fields.get(5, [b""])[0] or b"").decode("utf-8", "replace")
        if isinstance(fields.get(5, [b""])[0], bytes)
        else "",
        "raw": buf,
    }


# ------------------------------------------------------------------ 好友

def build_friend_request(cookie: bytes = b"", *, req_count: int = 300, local_seq: int = 13) -> bytes:
    """IncPullRequest：拉好友。`cookie` 为空表示第一页。"""
    # 两组 biz 各自是一个 10001 条目（repeated 字段要出现两次，不能合并成一坨）
    biz = pb.bytes_field(
        10001,
        pb.uint_field(1, 1) + pb.bytes_field(2, _packed_field(1, [103, 102, 20002, 27394, 20009, 20037])),
    )
    biz += pb.bytes_field(
        10001,
        pb.uint_field(1, 4) + pb.bytes_field(2, _packed_field(1, [100, 101, 102])),
    )
    body = (
        pb.uint_field(2, req_count)
        + pb.uint_field(4, local_seq)
        + pb.bytes_field(5, cookie)
        + pb.uint_field(6, 1)
        + pb.uint_field(7, 0x7FFFFFFF)
        + biz
        + _packed_field(10002, [13578, 13579, 13573, 13572, 13568])
        + _packed_field(10003, [4051])
    )
    return oidb(FRIEND_COMMAND, FRIEND_SUB, body)


def _parse_sub_biz(blob: bytes) -> tuple[dict[int, int], dict[int, bytes]]:
    nums: dict[int, int] = {}
    texts: dict[int, bytes] = {}
    fields = pb.decode(blob)
    for entry in fields.get(1, []):
        pair = pb.decode(entry) if isinstance(entry, bytes) else {}
        key = pair.get(1, [0])[0]
        if isinstance(key, int):
            nums[int(key)] = int(pair.get(2, [0])[0])
    for entry in fields.get(2, []):
        pair = pb.decode(entry) if isinstance(entry, bytes) else {}
        key = pair.get(1, [0])[0]
        value = pair.get(2, [b""])[0]
        if isinstance(key, int) and isinstance(value, bytes):
            texts[int(key)] = value
    return nums, texts


def parse_friends(body: bytes) -> dict[str, Any]:
    """IncPullResponse → `{friends, categories, cookie, is_end}`。"""
    fields = pb.decode(body)
    friends: list[dict[str, Any]] = []
    for blob in fields.get(101, []):
        if not isinstance(blob, bytes):
            continue
        entry = pb.decode(blob)
        uid = (entry.get(1, [b""])[0] or b"").decode("utf-8", "replace")
        uin = int(entry.get(3, [0])[0])
        category = int(entry.get(2, [0])[0])
        nums: dict[int, int] = {}
        texts: dict[int, bytes] = {}
        for sub in entry.get(10001, []):
            if not isinstance(sub, bytes):
                continue
            pair = pb.decode(sub)
            biz_type = int(pair.get(1, [0])[0])
            biz_body = pair.get(2, [b""])[0]
            if biz_type == 1 and isinstance(biz_body, bytes):
                nums, texts = _parse_sub_biz(biz_body)
        friends.append(
            {
                "user_id": uin,
                "uid": uid,
                "nickname": (texts.get(BIZ_NICK, b"") or b"").decode("utf-8", "replace"),
                "remark": (texts.get(BIZ_REMARK, b"") or b"").decode("utf-8", "replace"),
                "signature": (texts.get(BIZ_SIGN, b"") or b"").decode("utf-8", "replace"),
                "qid": (texts.get(BIZ_QID, b"") or b"").decode("utf-8", "replace"),
                "age": nums.get(BIZ_AGE, 0),
                "gender": nums.get(BIZ_GENDER, 0),
                "category_id": category,
            }
        )
    categories = []
    for blob in fields.get(102, []):
        if not isinstance(blob, bytes):
            continue
        entry = pb.decode(blob)
        categories.append(
            {
                "id": int(entry.get(1, [0])[0]),
                "name": (entry.get(2, [b""])[0] or b"").decode("utf-8", "replace"),
                "count": int(entry.get(3, [0])[0]),
            }
        )
    cookie = fields.get(2, [b""])[0]
    return {
        "friends": friends,
        "categories": categories,
        "cookie": cookie if isinstance(cookie, bytes) else b"",
        "is_end": bool(fields.get(3, [0])[0]),
    }


# ------------------------------------------------------------------ 群

def _all_true(fields: list[int]) -> bytes:
    return b"".join(pb.uint_field(f, 1) for f in fields)


def build_group_request() -> bytes:
    """FetchGroupsRequest：三个 config 里把要的字段全打开（值为 true 的 varint）。"""
    config1 = _all_true([1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 5001, 5002, 5003])
    config2 = _all_true([1, 2, 3, 4, 5, 6, 7, 8])
    config3 = _all_true([5, 6])
    config = (
        pb.bytes_field(1, config1) + pb.bytes_field(2, config2) + pb.bytes_field(3, config3)
    )
    return oidb(GROUP_COMMAND, GROUP_SUB, pb.bytes_field(1, config))


def parse_groups(body: bytes) -> list[dict[str, Any]]:
    """FetchGroupsResponse → 群列表。"""
    groups: list[dict[str, Any]] = []
    for blob in pb.decode(body).get(2, []):
        if not isinstance(blob, bytes):
            continue
        entry = pb.decode(blob)
        info = pb.decode(entry.get(4, [b""])[0]) if isinstance(entry.get(4, [b""])[0], bytes) else {}
        custom = pb.decode(entry.get(5, [b""])[0]) if isinstance(entry.get(5, [b""])[0], bytes) else {}
        groups.append(
            {
                "group_id": int(entry.get(3, [0])[0]),
                "group_name": (info.get(5, [b""])[0] or b"").decode("utf-8", "replace"),
                "member_count": int(info.get(4, [0])[0]),
                "max_member_count": int(info.get(3, [0])[0]),
                "created_time": int(info.get(2, [0])[0]),
                "description": (info.get(18, [b""])[0] or b"").decode("utf-8", "replace"),
                "question": (info.get(19, [b""])[0] or b"").decode("utf-8", "replace"),
                "announcement": (info.get(30, [b""])[0] or b"").decode("utf-8", "replace"),
                "remark": (custom.get(3, [b""])[0] or b"").decode("utf-8", "replace"),
            }
        )
    return groups


# ------------------------------------------------------------------ 客户端


@dataclass
class RosterClient:
    """只读花名册。`call` 负责把 (service, body) 发出去并拿回 OIDB 响应体。"""

    call: CallFn
    max_pages: int = 20
    log: list[str] = field(default_factory=list)

    async def friends(self) -> list[dict[str, Any]]:
        cookie = b""
        out: list[dict[str, Any]] = []
        for page in range(self.max_pages):
            raw = await self.call(FRIEND_SERVICE, build_friend_request(cookie))
            parsed = parse_friends(parse_oidb(raw)["body"])
            out.extend(parsed["friends"])
            self.log.append(f"好友第 {page + 1} 页：{len(parsed['friends'])} 条")
            if parsed["is_end"] or not parsed["cookie"] or parsed["cookie"] == cookie:
                break
            cookie = parsed["cookie"]
        return out

    async def groups(self) -> list[dict[str, Any]]:
        raw = await self.call(GROUP_SERVICE, build_group_request())
        groups = parse_groups(parse_oidb(raw)["body"])
        self.log.append(f"群：{len(groups)} 个")
        return groups

    async def overview(self) -> dict[str, Any]:
        """一次拿两边，顺便给出「能读到多少」的体检结果。"""
        friends = await self.friends()
        groups = await self.groups()
        return {
            "friends": friends,
            "groups": groups,
            "friend_count": len(friends),
            "group_count": len(groups),
            "log": list(self.log),
        }
