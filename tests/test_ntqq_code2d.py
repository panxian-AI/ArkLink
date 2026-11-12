"""code2d / wtlogin 封包层：TLV、容器偏移、状态机应答。"""

from __future__ import annotations

import asyncio
import struct

import pytest

from arklink.ntqq import code2d, session, tea


def make_response(payload: bytes, key: bytes, *, command: int = 0x812, uin: int = 0) -> bytes:
    """按服务端响应布局造一帧：`02|len|8001|cmd|seq|uin|00(flag)|00(encType)|00(state)|TEA|03`。"""
    cipher = tea.encrypt_packet(payload, key)
    inner = struct.pack(">HHHH", 8001, command, 0, 0)  # ver, cmd, seq, uin 低 16 位占位
    inner = struct.pack(">HHH", 8001, command, 0) + struct.pack(">I", uin)
    inner += b"\x00" + b"\x00" + b"\x00"          # flag / encType=0 / state
    inner += cipher + b"\x03"
    return b"\x02" + struct.pack(">H", len(inner)) + inner


def make_code2d_response(sub: int, body: bytes) -> bytes:
    """造一个能被 `parse_code2d` 读回的容器。"""
    inner = bytearray(struct.pack(">H", sub))
    inner += b"\x00" * 21
    inner += b"\x03" + struct.pack(">H", 0) + struct.pack(">H", 0x32)
    inner += struct.pack(">i", 1) + (0).to_bytes(8, "big")
    inner += body + b"\x03"
    span = b"\x02" + struct.pack(">H", len(inner) + 1) + bytes(inner)
    return b"\x00" + b"\x00" + struct.pack(">H", len(span)) + b"\x00" + span


def test_tlv_roundtrip():
    blob = code2d.pack_tlvs([(0x16, b"abc"), (0x1E, b"\x30\x01")])
    block = struct.pack(">H", 2) + blob
    parsed = code2d.unpack_tlvs(block)
    assert parsed[0x16] == b"abc"
    assert parsed[0x1E] == b"\x30\x01"


def test_tlv_truncated_is_tolerated():
    block = struct.pack(">H", 2) + struct.pack(">HH", 0x16, 10) + b"ab"
    parsed = code2d.unpack_tlvs(block)
    assert parsed[0x16] == b"ab"  # 短读也不抛异常，抓包常有半个 TLV


def test_code2d_container_layout():
    body = code2d.build_fetch_qr(guid=b"\x01" * 16)
    assert body[4] == 0x02                      # 容器标记
    assert struct.unpack(">H", body[7:9])[0] == code2d.CMD_FETCH_QR
    assert body[9:30] == b"\x00" * 21           # 客户端信息占位
    assert body[30] == 0x03                     # flag = code_2d


def test_wrap_d0_layout():
    inner = b"\xaa" * 10
    wrapped = code2d.wrap_d0(inner, app_id=1234)
    assert wrapped[0] == 0
    assert struct.unpack(">H", wrapped[1:3])[0] == len(inner)
    assert struct.unpack(">I", wrapped[3:7])[0] == 1234
    assert wrapped.endswith(inner)


def test_parse_fetch_qr_reads_url_from_tlv_d1():
    from arklink.ntqq import pb

    qr_ext = pb.string_field(2, "https://q.qq.com/x?k=abc")
    body = struct.pack(">H", 4) + b"SIG!" + struct.pack(">H", 1)
    body += struct.pack(">HH", 0xD1, len(qr_ext)) + qr_ext
    qr = code2d.parse_fetch_qr(body)
    assert qr.sig == b"SIG!"
    assert qr.url.startswith("https://q.qq.com/x")


def test_qr_state_names():
    assert code2d.QR_STATES[48] == "waiting_scan"
    assert code2d.QR_STATES[53] == "waiting_confirm"
    waiting = struct.pack(">H", 1) + struct.pack(">HH", 0x1E, 2) + b"\x30\x00"
    assert code2d.parse_query_qr((1).to_bytes(8, "big") + b"\x00" * 4 + waiting).name == "waiting_scan"
    assert code2d.QR_STATES[0] == "confirmed"


def test_parse_query_confirmed_and_tickets():
    tlv_block = struct.pack(">H", 2)
    tlv_block += struct.pack(">HH", 0x1E, 3) + b"\x00\xaa\xbb"
    tlv_block += struct.pack(">HH", 0x19, 2) + b"TT"
    state = code2d.parse_query_qr((10001).to_bytes(8, "big") + b"\x00" * 4 + tlv_block)
    assert state.uin == 10001
    assert state.name == "confirmed"
    assert state.tickets[0x19] == b"TT"
    assert state.final


def test_parse_verify_extracts_platform_and_tlvs():
    body = (10001).to_bytes(8, "big") + b"\x00" + struct.pack(">H", 3) + b"Pad"
    body += struct.pack(">HH", 0x106, 2) + b"tgt"[0:2]
    result = code2d.parse_verify(body)
    assert result["uin"] == 10001
    assert result["platform"] == "Pad"
    assert result["tlvs"][0x106] == b"tg"


def test_login_state_machine_full_flow(tmp_path):
    """0x31 → 0x12(待扫) → 0x12(已确认) → 0x13 全链路，用回环传输跑。"""
    loop = session.LoopbackWtLogin()
    path = tmp_path / "keystore.json"
    sess = session.QrLoginSession(loop, poll_interval=0, timeout=5, keystore_path=path)

    key = sess.share_key

    def pack_fetch() -> bytes:
        from arklink.ntqq import pb

        qr_ext = pb.string_field(2, "https://q.qq.com/demo")
        body = struct.pack(">H", 3) + b"SIG" + struct.pack(">H", 1)
        body += struct.pack(">HH", 0xD1, len(qr_ext)) + qr_ext
        return make_response(make_code2d_response(0x31, body), key)

    def query(state_byte: int, sig_tail: bytes = b"") -> bytes:
        tlvs = struct.pack(">H", 2)
        tlvs += struct.pack(">HH", 0x1E, 3) + bytes([state_byte]) + sig_tail + b"\x01"
        tlvs += struct.pack(">HH", 0x19, 1) + b"T"
        return make_response(make_code2d_response(0x12, (10001).to_bytes(8, "big") + b"\x00" * 4 + tlvs), key)

    verify_body = (10001).to_bytes(8, "big") + b"\x00" + struct.pack(">H", 3) + b"Pad"
    verify_body += struct.pack(">HH", 0x106, 3) + b"tgt"

    loop.replies = [pack_fetch(), query(48), query(0), make_response(make_code2d_response(0x13, verify_body), key)]

    seen: list[str] = []

    async def run():
        qr = await sess.fetch()
        assert qr.url == "https://q.qq.com/demo"
        states = [await sess.poll(), await sess.poll()]
        seen.extend(state.name for state in states)
        assert states[-1].name == "confirmed"
        await sess.verify()
        return sess.keystore

    keystore = asyncio.run(run())
    assert seen == ["waiting_scan", "confirmed"]
    assert keystore.uin == 10001
    assert keystore.platform == "Pad"
    assert path.exists()
    restored = session.Keystore.load(path)
    assert restored.tickets[0x106] == b"tgt"
    assert restored.a2  # 0x106 也能当 A2 的兜底
    assert len(loop.sent) == 4


def test_login_aborts_on_expired():
    loop = session.LoopbackWtLogin()
    sess = session.QrLoginSession(loop, poll_interval=0, timeout=5)
    key = sess.share_key

    tlvs = struct.pack(">H", 1) + struct.pack(">HH", 0x1E, 2) + b"\x11\x00"
    expired = make_response(make_code2d_response(0x12, (1).to_bytes(8, "big") + b"\x00" * 4 + tlvs), key)
    from arklink.ntqq import pb

    qr_ext = pb.string_field(2, "https://q.qq.com/expired")
    fetch_body = struct.pack(">H", 0) + struct.pack(">H", 1)
    fetch_body += struct.pack(">HH", 0xD1, len(qr_ext)) + qr_ext
    loop.replies = [make_response(make_code2d_response(0x31, fetch_body), key), expired]

    async def run():
        with pytest.raises(RuntimeError, match="expired"):
            await sess.login()
        return sess.state.name

    # 过期是终态：login 应当立刻抛出，而不是空转到 timeout
    assert asyncio.run(run()) == "expired"


def test_hexdump_is_readable():
    text = code2d.hexdump(b"\x02\x00abc")
    assert "02 00 61 62 63" in text
    assert text.startswith("00000000")
