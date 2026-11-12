"""SSO 封包 / protobuf / 原生管线的字节级测试。"""

from __future__ import annotations

import struct

import pytest

from arklink.ntqq import native, pb, sso


def test_packet_layout_offsets():
    pkt = sso.SSOPacket(seq=0x01020304, cmd=0x1234, body=b"abc", uin=10001)
    wire = pkt.encode()
    assert wire[0] == sso.MAGIC
    length = struct.unpack(">I", wire[1:5])[0]
    assert length == len(wire) - 5, "length 从 seq 起算，不含 magic 与自身"
    assert struct.unpack(">I", wire[5:9])[0] == 0x01020304
    assert struct.unpack(">I", wire[9:13])[0] == 0x1234
    assert struct.unpack(">H", wire[13:15])[0] == 1
    assert struct.unpack(">I", wire[15:19])[0] == 10001
    assert wire[19:-1] == b"abc"
    assert wire[-1] == sso.TAIL


def test_decode_roundtrip_and_leftover():
    pkt = sso.SSOPacket(seq=5, cmd=0x2222, body=b"\x08\x01", uin=42)
    decoded, rest = sso.SSOPacket.decode(pkt.encode() + b"tail")
    assert (decoded.seq, decoded.cmd, decoded.uin, decoded.body) == (5, 0x2222, 42, b"\x08\x01")
    assert rest == b"tail"


def test_uid_variant():
    pkt = sso.SSOPacket(seq=9, cmd=0x3333, body=b"body", uin=7, uid="u_abcdefABCDEF-012345")
    back, _ = sso.SSOPacket.decode(pkt.encode())
    assert back.uid == "u_abcdefABCDEF-012345"
    assert back.body == b"body"


def test_decode_rejects_bad_input():
    with pytest.raises(ValueError):
        sso.SSOPacket.decode(b"\x00" * 30)
    good = sso.SSOPacket(seq=1, cmd=2, body=b"x" * 8).encode()
    with pytest.raises(ValueError):
        sso.SSOPacket.decode(good[:12])
    broken = bytearray(good)
    broken[-1] = 0x00
    with pytest.raises(ValueError):
        sso.SSOPacket.decode(bytes(broken))


def test_iter_packets_handles_sticky_stream():
    stream = b"".join(
        sso.SSOPacket(seq=i, cmd=0x10 + i, body=b"b" * i).encode() for i in range(1, 4)
    )
    packets = list(sso.iter_packets(stream))
    assert [p.seq for p in packets] == [1, 2, 3]


def test_protobuf_roundtrip():
    buf = pb.uint_field(1, 150) + pb.string_field(2, "ark") + pb.fixed32_field(3, 7)
    fields = pb.decode(buf)
    assert fields[1] == [150]
    assert fields[2] == [b"ark"]
    assert fields[3] == [7]


def test_protobuf_truncated_is_tolerant():
    buf = pb.string_field(2, "hello")
    assert pb.decode(buf[:-2]) == {} or 2 in pb.decode(buf[:-2])
    assert pb.dump(pb.uint_field(1, 1)) == "1: 1"


def test_cmd_registry():
    sso.register_cmd(0x1234, "wtlogin.login")
    assert sso.cmd_name(0x1234) == "wtlogin.login"
    assert sso.cmd_name(0x99999999).startswith("0x")


def test_native_pipeline_selftest():
    report = native.NativeQQAdapter.selftest()
    assert report["body_ok"] is True
    assert report["seq"] == 7 and report["cmd"] == 0x1234


def test_loopback_probe():
    async def run():
        transport = native.LoopbackTransport(replies=[sso.SSOPacket(seq=1, cmd=0x42, body=b"pong").encode()])
        adapter = native.NativeQQAdapter(uin=10001, transport=transport)
        packets = await adapter.probe(0x42, b"ping")
        assert transport.sent and packets[0].body == b"pong"

    import asyncio
    asyncio.run(run())
