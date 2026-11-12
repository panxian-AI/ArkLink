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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
