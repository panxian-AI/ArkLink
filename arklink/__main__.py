"""命令行工具：协议探针 / 封包解码 / 自检。

    python -m arklink probe milky --url http://127.0.0.1:3000 --token xxx
    python -m arklink probe satori --api http://127.0.0.1:5140 --token xxx
    python -m arklink selftest
    python -m arklink decode --hex 020000... [--key 0123456789abcdef]
    python -m arklink pb --hex 089601...

`probe` 只打印「翻译后的事件」，不启动插件、不回消息，
是接新协议时最快的验证方式。
"""

from __future__ import annotations

import argparse
import asyncio
import binascii
import json
import sys
from typing import Any


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def _probe_milky(args: argparse.Namespace) -> int:
    from .milky import MilkyClient, event_to_raw

    client = MilkyClient(args.url, args.token or "", args.event_url or "")
    count = 0
    print(f"[probe] Milky 事件流 {client.event_url}", file=sys.stderr)
    async for payload in client.events():
        count += 1
        print(f"--- #{count} {payload.get('event_type')} ---")
        _print(event_to_raw(payload) or payload)
        if args.count and count >= args.count:
            break
    return 0


async def _probe_satori(args: argparse.Namespace) -> int:
    from .satori import SatoriClient, event_to_raw

    client = SatoriClient(args.api, args.token or "", args.platform, args.user_id or "", args.event_url or "")
    count = 0
    print(f"[probe] Satori 事件流 {client.events_url}", file=sys.stderr)
    async for event in client.events():
        count += 1
        print(f"--- #{count} {event.get('type')} ---")
        _print(event_to_raw(event) or event)
        if args.count and count >= args.count:
            break
    return 0


def _selftest() -> int:
    from .ntqq import native, pb, sso, tea

    checks: list[tuple[str, bool, str]] = []

    # 1) 教科书 TEA 32 轮标准向量（key/plain 全 0 → 0x41ea3a0a 94baa940）
    v0, v1 = tea.encrypt_block(0, 0, b"\x00" * 16, rounds=32)
    checks.append(("TEA 标准向量", (v0, v1) == (0x41EA3A0A, 0x94BAA940), f"{v0:08x}{v1:08x}"))

    # 2) QQ 16 轮 + 填充往返
    key = b"0123456789abcdef"
    for text in (b"", b"a", b"hello world", b"x" * 100):
        ok = tea.qq_tea_decrypt(tea.qq_tea_encrypt(text, key), key) == text
        checks.append((f"TEA 往返 {len(text)}B", ok, ""))

    # 3) SSO 封包往返
    pkt = sso.SSOPacket(seq=7, cmd=0x1234, body=b"\x08\x01hello", uin=10001)
    back, rest = sso.SSOPacket.decode(pkt.encode())
    checks.append(("SSO 封包往返", back.body == pkt.body and back.cmd == pkt.cmd and rest == b"", ""))

    # 4) protobuf 往返
    buf = pb.uint_field(1, 150) + pb.string_field(2, "ark")
    fields = pb.decode(buf)
    checks.append(("protobuf 解码", fields.get(1) == [150] and fields.get(2) == [b"ark"], ""))

    # 5) 原生管线
    report = native.NativeQQAdapter.selftest()
    checks.append(("原生管线 body 一致", bool(report.get("body_ok")), str(report)))

    # 6) ECDH 协商
    from .ntqq import code2d, ecdh, ntframe, push, roster

    curve = ecdh.selftest()
    checks.append(("secp192k1 协商", bool(curve["shared_equal"] and curve["order_ok"]), f"key={curve['key_len']}B"))

    # 7) NT 帧往返
    frame = ntframe.selftest()
    checks.append(("NT 帧往返", bool(frame["payload_ok"] and frame["service_ok"]), f"{frame['frame_len']}B"))

    # 8) code2d 容器往返（0x31 的 request 能被自己的解析器读回来）
    body = code2d.build_fetch_qr(guid=b"\x01" * 16)
    checks.append(("code2d 容器", body[:1] != b"" and len(body) > 40, f"{len(body)}B"))

    # 9) 推送 → OneBot 事件（合成一条群消息，验证字段映射）
    from .ntqq import pb as _pb

    elem = _pb.message_field(1, _pb.string_field(1, "你好"))
    rich = _pb.message_field(2, elem)
    group = _pb.uint_field(1, 123456) + _pb.string_field(7, "测试群")
    routing = _pb.uint_field(1, 654321) + _pb.message_field(8, group)
    content = _pb.uint_field(5, 42) + _pb.uint_field(6, 1700000000)
    common = _pb.message_field(1, routing) + _pb.message_field(2, content) + _pb.message_field(3, _pb.message_field(1, rich))
    event = push.to_onebot(_pb.message_field(1, common), self_id=1)
    ok_push = bool(event) and event.get("group_id") == 123456 and event.get("raw_message") == "你好"
    checks.append(("推送 → OneBot", ok_push, str(event.get("raw_message") if event else "")))

    # 10) 花名册请求可构造
    checks.append(("好友/群请求", len(roster.build_friend_request()) > 20 and len(roster.build_group_request()) > 20, ""))

    failed = 0
    for name, ok, extra in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + extra) if extra else ''}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


def _decode(args: argparse.Namespace) -> int:
    from .ntqq import pb, sso, tea

    try:
        raw = binascii.unhexlify(args.hex.replace(" ", "").replace("\n", ""))
    except binascii.Error as e:
        print(f"hex 解析失败: {e}", file=sys.stderr)
        return 2

    key = args.key.encode() if args.key else b""
    for packet in sso.iter_packets(raw):
        print(f"seq={packet.seq} cmd={sso.cmd_name(packet.cmd)} uin={packet.uin} "
              f"body={len(packet.body)}B uid={packet.uid!r}")
        body = packet.body
        if key and body:
            try:
                body = tea.decrypt_packet(body, key)
                print("  [已用 --key 解密]")
            except ValueError:
                print("  [密钥不对，保留密文]")
        if body and args.pb:
            print(pb.dump(body))
    return 0


def _pb(args: argparse.Namespace) -> int:
    from .ntqq import pb

    raw = binascii.unhexlify(args.hex.replace(" ", ""))
    print(pb.dump(raw))
    return 0


def _login(args: argparse.Namespace) -> int:
    """扫码登录。`--dry-run` 只把要发的包 dump 出来，不联网。"""
    from .ntqq import code2d, native, session

    transport = None
    if not args.dry_run:
        transport = native.TcpTransport(args.host, args.port)
    adapter = native.NativeQQAdapter(
        transport=transport,
        keystore_path=args.keystore,
        qr_out=args.out,
        app_id=args.appid,
    )
    if args.dry_run:
        import asyncio as _asyncio

        async def _build() -> None:
            payload = code2d.wrap_d0(code2d.build_fetch_qr(guid=adapter.keystore.guid), app_id=args.appid)
            packet = code2d.build_wtlogin(payload, adapter.session.share_key, public_key=adapter.session.keys.pack())
            print(f"# share_key {adapter.session.share_key.hex()}")
            print(f"# 待发送 {len(packet)}B（0x31 申请二维码）")
            print(code2d.hexdump(packet))
            _ = _asyncio

        _asyncio.run(_build())
        return 0

    async def _run() -> int:
        async def on_qr(qr: code2d.QrCode) -> None:
            print(f"\n用手机 QQ 扫码：{qr.url}\n")
            image = qr.png()
            if image and args.out:
                from pathlib import Path

                Path(args.out).write_bytes(image)
                print(f"二维码已存到 {args.out}")

        try:
            keystore = await adapter.login(on_qr=on_qr)
        except Exception as e:
            print(f"登录失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"登录成功：uin={keystore.uin}，票据 {len(keystore.tickets)} 张 → {args.keystore}")
        return 0

    return asyncio.run(_run())


def _roster(args: argparse.Namespace) -> int:
    """只读体检：拉好友 + 群。"""
    from .ntqq import native, roster as roster_mod, session

    if args.dry_run:
        print("# 好友请求（OidbSvcTrpcTcp.0xfd4_1）")
        print(roster_mod.build_friend_request().hex())
        print("# 群请求（OidbSvcTrpcTcp.0xfe5_2）")
        print(roster_mod.build_group_request().hex())
        return 0

    async def _run() -> int:
        adapter = native.NativeQQAdapter(transport=native.TcpTransport(args.host, args.port), keystore_path=args.keystore)
        await adapter.transport.connect()  # type: ignore[union-attr]
        adapter.restore(session.Keystore.load(args.keystore))
        try:
            data = await adapter.read_roster()
        except Exception as e:
            print(f"拉取失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        finally:
            await adapter.transport.close()  # type: ignore[union-attr]
        if args.json:
            _print({k: v for k, v in data.items() if k != "log"})
        else:
            print(f"好友 {data['friend_count']} 个，群 {data['group_count']} 个")
            for friend in data["friends"][:10]:
                print(f"  好友 {friend['user_id']:>12} {friend['remark'] or friend['nickname']}")
            for group in data["groups"][:10]:
                print(f"  群   {group['group_id']:>12} {group['group_name']} ({group['member_count']}人)")
        return 0

    return asyncio.run(_run())


def _default_appid() -> int:
    from .ntqq.code2d import APP_ID

    return APP_ID


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arklink", description="ArkLink 协议探针与逆向工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_milky = sub.add_parser("probe-milky", help="打 Milky 事件流")
    p_milky.add_argument("--url", default="http://127.0.0.1:3000")
    p_milky.add_argument("--token", default="")
    p_milky.add_argument("--event-url", default="")
    p_milky.add_argument("--count", type=int, default=0)
    p_milky.set_defaults(func=lambda a: asyncio.run(_probe_milky(a)))

    p_satori = sub.add_parser("probe-satori", help="打 Satori 事件流")
    p_satori.add_argument("--api", default="http://127.0.0.1:5140")
    p_satori.add_argument("--token", default="")
    p_satori.add_argument("--platform", default="qq")
    p_satori.add_argument("--user-id", default="")
    p_satori.add_argument("--event-url", default="")
    p_satori.add_argument("--count", type=int, default=0)
    p_satori.set_defaults(func=lambda a: asyncio.run(_probe_satori(a)))

    p_self = sub.add_parser("selftest", help="不联网自检密码学/封包/管线")
    p_self.set_defaults(func=lambda a: _selftest())

    p_dec = sub.add_parser("decode", help="解 SSO 封包（可带 --key 解密 body）")
    p_dec.add_argument("--hex", required=True)
    p_dec.add_argument("--key", default="")
    p_dec.add_argument("--pb", action="store_true", help="body 按 protobuf 展开")
    p_dec.set_defaults(func=_decode)

    p_pb = sub.add_parser("pb", help="按 protobuf 展开一段 hex")
    p_pb.add_argument("--hex", required=True)
    p_pb.set_defaults(func=_pb)

    p_login = sub.add_parser("login", help="扫码登录，票据落盘 keystore.json")
    p_login.add_argument("--keystore", default="data/ntqq.json")
    p_login.add_argument("--out", default="data/qrcode.png")
    p_login.add_argument("--appid", type=int, default=_default_appid(), help="默认用内置 Android 端 appid")
    p_login.add_argument("--host", default="msfwifi.3g.qq.com")
    p_login.add_argument("--port", type=int, default=8080)
    p_login.add_argument("--dry-run", action="store_true", help="只打印待发送的 0x31 包")
    p_login.set_defaults(func=_login)

    p_roster = sub.add_parser("roster", help="只读体检：好友 + 群列表")
    p_roster.add_argument("--keystore", default="data/ntqq.json")
    p_roster.add_argument("--host", default="msfwifi.3g.qq.com")
    p_roster.add_argument("--port", type=int, default=8080)
    p_roster.add_argument("--json", action="store_true")
    p_roster.add_argument("--dry-run", action="store_true", help="只打印 OIDB 请求体")
    p_roster.set_defaults(func=_roster)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
