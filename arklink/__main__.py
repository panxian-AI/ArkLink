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
import os
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


def _hook(args: argparse.Namespace) -> int:
    """注入通道的四个动作：看进程 / 注入 / 探事件 / 卸载。"""
    import time

    from .ntqq import hookproc

    def _list() -> int:
        processes = hookproc.find_processes()
        if not processes:
            print("没有 QQ.exe 在跑（先手动登录 QQ 客户端）")
            return 0
        for item in processes:
            tag = "主" if item.is_main else "  "
            pipe = item.pipe or "-"
            print(f"[{tag}] pid={item.pid:<7} 管道={pipe:<26} {item.pipe_state:<7} {item.cmdline[:64]}")
        main = hookproc.pick_main(processes)
        if main is not None:
            print(f"\n建议注入：pid={main.pid}（管道 {main.pipe or '无'} / {main.pipe_state}）")
        return 0

    if args.install:
        return _install(args)

    if args.list or not (args.inject or args.probe or args.unload):
        return _list()

    processes = hookproc.find_processes()
    target = next((p for p in processes if p.pid == args.pid), None) if args.pid else hookproc.pick_main(processes)
    if target is None:
        print(f"找不到目标进程（pid={args.pid}）" if args.pid else "找不到 QQ 主进程", file=sys.stderr)
        return 1
    scanner = hookproc.ProcessScanner()

    if args.inject:
        dll = args.dll or hookproc.default_hook_dll()
        if not dll:
            print("找不到 hook DLL：编译 hookdll/ 或用 --dll 指定", file=sys.stderr)
            return 1
        try:
            result = hookproc.Injector(scanner).inject(target.pid, dll, args.method)
        except hookproc.HookError as exc:
            print(f"注入失败：{exc}", file=sys.stderr)
            return 1
        print(f"已注入 pid={result.pid} → {result.dll}（base=0x{result.handle:x}）")
        if result.note:
            print(f"注意：{result.note}")
        for _ in range(50):
            if scanner.probe_pipe(target.pid) != "missing":
                print("管道已就绪")
                break
            time.sleep(0.2)
        else:
            print("等了 10s 管道还没出现：DLL 可能没实现 control 管道，或 DllMain 里拒绝了加载", file=sys.stderr)
            return 2
        return 0

    if args.unload:
        base = hookproc.Injector(scanner).module_loaded(target.pid, os.path.basename(args.dll or "snowluma-win32-x64.dll"))
        if not base:
            print("目标进程里没找到该模块，可能本来就是 manual map 注入的", file=sys.stderr)
            return 1
        ok = hookproc.Injector(scanner).unload(target.pid, base)
        print("已卸载" if ok else "卸载失败")
        return 0 if ok else 1

    # --probe
    return asyncio.run(_probe_hook(target.pid, args))


async def _probe_hook(pid: int, args: argparse.Namespace) -> int:
    import time as _time

    from .ntqq import hookproc, push
    from .ntqq.hookpipe import HookClient, PipeError

    scanner = hookproc.ProcessScanner()
    prefix = args.prefix
    if not prefix:
        name = scanner.pipe_name(pid)
        prefix = name.split(".")[0] if name else "mojo"
    client = HookClient(pid, prefix=prefix)
    seen = 0

    def on_packet(frame) -> None:
        nonlocal seen
        seen += 1
        try:
            event = push.to_onebot(frame.body, self_id=int(client.uin or 0))
        except Exception as exc:  # 解析失败也要能看到原始包
            event = {"parse_error": str(exc), "hex": frame.body[:64].hex()}
        print(f"--- #{seen} {frame.cmd} seq={frame.value0} ---")
        _print(event or {"hex": frame.body[:64].hex()})
        if args.count and seen >= args.count:
            raise KeyboardInterrupt

    client.on_packet = on_packet
    client.on_login_state = lambda f: print(f"[登录态] logged_in={f.logged_in} uin={f.uin}")
    try:
        hello_c, hello_r = await client.connect(timeout=5)
    except (PipeError, OSError) as exc:
        print(f"连接失败：{exc}", file=sys.stderr)
        print("提示：先跑 `python -m arklink hook --inject`，或用 --prefix 指定管道前缀", file=sys.stderr)
        return 1
    print(f"已连接 pid={pid} control={hello_c.pipe_name} recv={hello_r.pipe_name}")
    status = await client.request("arklink.ping", b"", want_reply=True, timeout=3) if args.ping else None
    if status is not None:
        print(f"ping → status={status.status} {status.msg} body={status.body[:120]!r}")
    deadline = _time.monotonic() + args.seconds
    try:
        while _time.monotonic() < deadline:
            await asyncio.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()
    print(f"共收到 {seen} 条消息包")
    return 0


def _default_appid() -> int:
    from .ntqq.code2d import APP_ID

    return APP_ID


def _root() -> str:
    import pathlib

    return str(pathlib.Path(__file__).resolve().parents[1])


def _try_build_hook() -> str:
    """没现成 DLL 就现场编译（用户机器上有 mingw 才行）。"""
    import subprocess

    from .ntqq import hookproc

    script = os.path.join(_root(), "tools", "build_hook.py")
    if not os.path.exists(script):
        return ""
    print("没有现成的 hook DLL，尝试现场编译…")
    result = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        print("编译失败：" + " / ".join(tail), file=sys.stderr)
        print("需要 mingw-w64（x86_64-w64-mingw32-g++）或 MSVC，也可以 --dll 指定现成 DLL", file=sys.stderr)
        return ""
    dll = hookproc.default_hook_dll()
    if dll:
        print(f"编译完成：{dll}")
    return dll


def _install(args: argparse.Namespace) -> int:
    """一键接入：找进程 → 编译/注入 → 握手自检 → 打印接入命令。"""
    from .ntqq import hookproc

    scanner = hookproc.ProcessScanner()
    processes = hookproc.find_processes(("QQ.exe",), scanner)
    if not processes:
        print("没找到 QQ.exe。先手动打开并登录 QQ 客户端（登录态由客户端自己维持），再跑一次。")
        return 1

    target = next((p for p in processes if p.pid == args.pid), None) if args.pid else hookproc.pick_main(processes)
    if target is None:
        print(f"没有 pid={args.pid} 的 QQ 主进程", file=sys.stderr)
        return 1
    print(f"目标进程：pid={target.pid}（{len(processes)} 个 QQ 进程里挑出的主进程）")

    dll = args.dll or hookproc.default_hook_dll()
    if not dll:
        dll = _try_build_hook()
    if not dll:
        print("没有可用的 hook DLL", file=sys.stderr)
        return 1

    prefix = args.prefix
    state = scanner.probe_pipe(target.pid)
    if state == "missing" or not prefix:
        if state == "missing":
            print(f"注入 {os.path.basename(dll)} …")
            try:
                result = hookproc.Injector(scanner).inject(target.pid, dll, args.method)
            except hookproc.HookError as exc:
                print(f"注入失败：{exc}", file=sys.stderr)
                return 1
            print(f"注入成功（base=0x{result.handle:x}）")
        for candidate in hookproc.PIPE_PREFIXES:
            if scanner.pipe_name(target.pid, "control").startswith(candidate):
                prefix = candidate
                break
    prefix = prefix or "arklink"
    return asyncio.run(_install_verify(target.pid, prefix, args))


async def _install_verify(pid: int, prefix: str, args: argparse.Namespace) -> int:
    from .ntqq.hookpipe import HookClient, PipeError

    client = HookClient(pid, prefix=prefix)
    state: dict[str, str] = {}
    client.on_login_state = lambda frame: state.update(uin=frame.uin if frame.logged_in else "")
    try:
        await client.connect(timeout=args.timeout)
    except (PipeError, OSError, asyncio.TimeoutError) as exc:
        print(f"连不上管道 {prefix}.{pid}.*：{exc}", file=sys.stderr)
        return 2
    print(f"管道已连通：prefix={prefix} uin={client.uin or '（还没登录）'}")
    try:
        reply = await client.request("arklink.status", b"", timeout=5)
        if reply.body:
            print(f"自检：{reply.body.decode('utf-8', 'replace')}")
    except Exception:
        pass
    await client.close()

    token = args.token or "arklink"
    print(
        "\n接入方式（上层不用改代码，只改地址）：\n"
        f"  python -m arklink serve onebot --type ntqq-hook --port 6700 --token {token}\n"
        "  NoneBot2 / Koishi 连：ws://127.0.0.1:6700/onebot/v11/ws\n"
        "  HTTP API 同端口：http://127.0.0.1:6700/\n"
        "\nArkBot 侧 bot.toml：\n"
        "  [[adapters]]\n"
        '  type = "ntqq-hook"\n'
        f'  dll_path = "{os.path.basename(args.dll or "") or ""}"\n'
        "  auto_inject = true\n"
        "  read_only = true\n"
    )
    if not client.logged_in:
        print("提示：QQ 还没登录完成，登录后通道会自动就绪（无需重新注入）。")
    return 0


def _serve(args: argparse.Namespace) -> int:
    """把任意适配器暴露成一个 OneBot 11 服务端（NapCat 平替）。"""
    import json as _json

    from . import registry
    from .onebot import OneBot11Server

    cfg: dict[str, Any] = {}
    for item in args.set or []:
        key, _, raw = item.partition("=")
        if not key:
            continue
        try:
            cfg[key] = _json.loads(raw)
        except _json.JSONDecodeError:
            cfg[key] = raw

    adapter = registry.create(args.type, **cfg)
    reports = [u for u in (args.report or "").split(",") if u]

    async def main() -> int:
        server = OneBot11Server(
            [adapter],
            host=args.host,
            port=args.port,
            access_token=args.token,
            report_urls=reports,
            heartbeat=args.heartbeat,
        )
        task = asyncio.create_task(adapter.run_forever(), name="adapter")
        try:
            await server.serve_forever()
        finally:
            await adapter.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return 0

    print(f"适配器：{getattr(adapter, 'name', args.type)}")
    return asyncio.run(main())


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

    p_hook = sub.add_parser("hook", help="注入通道：查进程 / 注入 / 探事件 / 卸载")
    p_hook.add_argument("--list", action="store_true", help="列出 QQ 进程与管道状态")
    p_hook.add_argument("--inject", action="store_true", help="注入 hook DLL")
    p_hook.add_argument("--probe", action="store_true", help="连上管道并打印收到的消息包")
    p_hook.add_argument("--unload", action="store_true", help="卸载已注入的模块")
    p_hook.add_argument("--pid", type=int, default=0, help="目标进程（默认自动挑主进程）")
    p_hook.add_argument("--dll", default="", help="hook DLL 路径（默认自动找）")
    p_hook.add_argument("--method", default="loadlibrary", help="注入方式")
    p_hook.add_argument("--prefix", default="", help="管道前缀（默认自动探测 mojo/arklink）")
    p_hook.add_argument("--seconds", type=float, default=30.0, help="--probe 监听时长")
    p_hook.add_argument("--count", type=int, default=0, help="收到 N 条后退出")
    p_hook.add_argument("--ping", action="store_true", help="--probe 时先发一个 ping 请求")
    p_hook.add_argument("--install", action="store_true", help="一键接入：找进程→编译/注入→自检→打印接入命令")
    p_hook.add_argument("--token", default="", help="--install 时打印给上层的 token（默认 arklink）")
    p_hook.add_argument("--timeout", type=float, default=20.0, help="--install 等待握手的时间")
    p_hook.set_defaults(func=_hook)

    p_serve = sub.add_parser("serve", help="把适配器暴露成 OneBot 11 服务端（NapCat 平替）")
    p_serve.add_argument("kind", choices=["onebot"], help="目前只有 onebot")
    p_serve.add_argument("--type", default="ntqq-hook", help="适配器类型（见 registry）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=6700)
    p_serve.add_argument("--token", default="", help="access_token，空 = 不校验")
    p_serve.add_argument("--report", default="", help="HTTP 上报地址，逗号分隔")
    p_serve.add_argument("--heartbeat", type=float, default=30.0, help="心跳间隔秒，0 = 关")
    p_serve.add_argument("--set", action="append", metavar="K=V", help="传给适配器的参数，可重复（值按 JSON 解析）")
    p_serve.set_defaults(func=_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
