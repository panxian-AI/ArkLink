"""注入通道的协议/客户端测试。

不碰真机：用内存管道伪造一个 hook 服务端，把「HELLO → REQUEST/ACK/REPLY →
主动推 packet → 关闭」整条对话跑一遍。真机验证（进程发现、注入）走 CLI，
见 `docs/进程注入通道.md`。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from arklink.ntqq import hookproc, hookproto
from arklink.ntqq.hookpipe import HookClient, MemoryPipe, PipeError


# ---------------------------------------------------------------- 帧
def test_frame_roundtrip():
    frame = hookproto.packet(
        hookproto.CMD_MSG_PUSH, b"\x08\x01\x12\x02hi", seq=42, uin="10001"
    )
    raw = frame.encode()
    assert len(raw) == hookproto.HEADER_SIZE + len(hookproto.CMD_MSG_PUSH) + 5 + 6
    out = hookproto.split_frames(raw)
    assert len(out) == 1
    got = out[0]
    assert (got.op, got.value0, got.cmd, got.uin, got.body) == (
        hookproto.OP_PACKET,
        42,
        hookproto.CMD_MSG_PUSH,
        "10001",
        b"\x08\x01\x12\x02hi",
    )


def test_frame_reader_handles_fragments():
    """一个字节一个字节喂，也必须只吐一帧。"""
    raw = hookproto.hello("mojo.1.control", 1).encode()
    reader = hookproto.FrameReader()
    frames = []
    for byte in raw:
        frames.extend(reader.push(bytes([byte])))
    assert len(frames) == 1
    assert frames[0].msg == "mojo.1.control"
    assert reader.pending == 0


def test_frame_reader_handles_glued_frames():
    raw = b"".join(
        [
            hookproto.login_state("10001").encode(),
            hookproto.capability(True).encode(),
            hookproto.ack(7).encode(),
        ]
    )
    reader = hookproto.FrameReader()
    frames = reader.push(raw[:50])
    frames += reader.push(raw[50:])
    assert [f.op for f in frames] == [
        hookproto.OP_LOGIN_STATE,
        hookproto.OP_CAPABILITY,
        hookproto.OP_ACK,
    ]
    assert frames[0].logged_in and frames[0].uin == "10001"


def test_bad_magic_is_rejected():
    raw = bytearray(hookproto.hello("x", 1).encode())
    raw[0] ^= 0xFF
    with pytest.raises(hookproto.FrameError):
        hookproto.split_frames(bytes(raw))


def test_oversized_body_is_rejected():
    frame = hookproto.Frame(op=hookproto.OP_PACKET, body=b"x" * (hookproto.MAX_BODY_BYTES + 1))
    with pytest.raises(hookproto.FrameError):
        frame.encode()


def test_login_state_semantics():
    frame = hookproto.login_state(0, logged_in=False)
    assert not frame.logged_in and frame.uin == ""
    frame = hookproto.login_state("1000000002")
    assert frame.logged_in and frame.uin == "1000000002" and frame.value0 == 1000000002


def test_header_is_exactly_40_bytes():
    """帧头长度是硬约定，改一个字段整条流就对不上。"""
    raw = hookproto.Frame(op=1).encode()
    magic, version = struct.unpack_from("<IH", raw, 0)
    assert (magic, version) == (hookproto.MAGIC, 1)
    assert hookproto.HEADER_SIZE == 40
    assert len(raw) == 40


# ---------------------------------------------------------------- 进程判定
def test_looks_like_main_filters_electron_children():
    assert hookproc.looks_like_main('"D:\\Tencent\\QQNT\\QQ.exe" ', "QQ.exe")
    assert hookproc.looks_like_main("", "QQ.exe")  # 读不到就放行，让上层试
    for child in ("--type=renderer", "--type=gpu-process", "--type=utility"):
        assert not hookproc.looks_like_main(f'"QQ.exe" {child}', "QQ.exe")
    assert not hookproc.looks_like_main('"QQ.exe"', "WeChat.exe")


class FakeScanner(hookproc.ProcessScanner):
    """把三个 Win32 入口换成固定数据。"""

    def __init__(self, processes, pipes, cmdlines, busy=()):
        super().__init__()
        self._processes = processes
        self._pipes = set(pipes)
        self._cmdlines = cmdlines
        self._busy = set(busy)

    def _snapshot(self):
        return list(self._processes)

    def command_line(self, pid):
        return self._cmdlines.get(pid, "")

    def pipe_names(self):
        return set(self._pipes)

    def probe_pipe(self, pid, suffix="control"):
        name = self.pipe_name(pid, suffix)
        if not name:
            return "missing"
        return "busy" if pid in self._busy else "free"


def test_scan_picks_main_and_marks_pipe():
    scanner = FakeScanner(
        processes=[(100, "QQ.exe"), (200, "QQ.exe"), (300, "QQ.exe"), (400, "other.exe")],
        pipes={"mojo.200.control", "mojo.200.recv"},
        cmdlines={
            100: '"QQ.exe" --type=renderer',
            200: '"QQ.exe" ',
            300: '"QQ.exe" --type=gpu-process',
        },
    )
    found = scanner.scan()
    assert [p.pid for p in found] == [200, 100, 300]
    assert found[0].is_main and found[0].pipe == "mojo.200.control"
    assert found[0].pipe_state == "free"
    assert hookproc.pick_main(found).pid == 200


def test_scan_prefers_free_pipe_when_several_mains():
    scanner = FakeScanner(
        processes=[(11, "QQ.exe"), (22, "QQ.exe")],
        pipes={"mojo.11.control", "mojo.22.control"},
        cmdlines={11: '"QQ.exe"', 22: '"QQ.exe"'},
        busy={11},
    )
    found = scanner.scan()
    assert found[0].pid == 22 and found[0].pipe_state == "free"
    assert found[1].pipe_state == "busy"


# ---------------------------------------------------------------- 客户端
class FakeHookServer:
    """内存版 hook 服务端：HELLO → 登录态 → 按需推包。"""

    def __init__(self, uin: str = "10001") -> None:
        self.uin = uin
        self.control_client, self.control_server = MemoryPipe.pair()
        self.recv_client, self.recv_server = MemoryPipe.pair()
        self.received: list[hookproto.Frame] = []
        self._readers = {"control": hookproto.FrameReader(), "recv": hookproto.FrameReader()}
        self._tasks: list[asyncio.Task] = []
        self.pid = 4242

    def pipes(self, name: str) -> MemoryPipe:
        return self.control_client if name.endswith(".control") else self.recv_client

    async def start(self) -> None:
        await self.control_server.write(hookproto.hello(f"mojo.{self.pid}.control", self.pid).encode())
        await self.recv_server.write(hookproto.hello(f"mojo.{self.pid}.recv", self.pid, recv=True).encode())
        self._tasks.append(asyncio.create_task(self._serve(self.control_server, "control")))
        self._tasks.append(asyncio.create_task(self._serve(self.recv_server, "recv")))

    async def _serve(self, pipe: MemoryPipe, kind: str) -> None:
        reader = self._readers[kind]
        while True:
            chunk = await pipe.read()
            if not chunk:
                return
            for frame in reader.push(chunk):
                self.received.append(frame)
                if kind == "control" and frame.op == hookproto.OP_REQUEST:
                    await pipe.write(hookproto.ack(frame.request_id).encode())
                    if frame.flags & hookproto.FLAG_WANT_REPLY:
                        await pipe.write(
                            hookproto.reply(frame.request_id, body=b'{"ok":true}').encode()
                        )

    async def push_login(self) -> None:
        await self.recv_server.write(hookproto.login_state(self.uin).encode())

    async def push_packet(self, body: bytes = b"\x08\x01") -> None:
        await self.recv_server.write(
            hookproto.packet(hookproto.CMD_MSG_PUSH, body, seq=1, uin=self.uin).encode()
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def run(coro):
    return asyncio.run(coro)


def test_client_handshake_and_login_state():
    async def main():
        server = FakeHookServer()
        await server.start()
        client = HookClient(server.pid, prefix="mojo", pipe_factory=server.pipes)
        hello_c, hello_r = await client.connect(timeout=2)
        assert hello_c.pid == server.pid and not hello_c.recv
        assert hello_r.recv
        await server.push_login()
        for _ in range(50):
            if client.logged_in:
                break
            await asyncio.sleep(0.01)
        assert client.logged_in and client.uin == "10001"
        await client.close()
        await server.stop()

    run(main())


def test_client_receives_packet():
    async def main():
        server = FakeHookServer()
        await server.start()
        client = HookClient(server.pid, pipe_factory=server.pipes)
        packets: list[hookproto.Frame] = []
        client.on_packet = packets.append
        await client.connect(timeout=2)
        await server.push_packet(b"\x08\x02")
        for _ in range(50):
            if packets:
                break
            await asyncio.sleep(0.01)
        assert packets and packets[0].cmd == hookproto.CMD_MSG_PUSH
        assert packets[0].body == b"\x08\x02"
        await client.close()
        await server.stop()

    run(main())


def test_client_request_gets_reply():
    async def main():
        server = FakeHookServer()
        await server.start()
        client = HookClient(server.pid, pipe_factory=server.pipes)
        await client.connect(timeout=2)
        reply = await client.request("arklink.ping", b"\x01", timeout=2)
        assert reply.op == hookproto.OP_REPLY and reply.body == b'{"ok":true}'
        assert server.received[-1].cmd == "arklink.ping"
        await client.close()
        await server.stop()

    run(main())


def test_client_connect_times_out_without_hello():
    """hook 模块没加载（或 DllMain 卡住）时，connect 必须超时而不是永远挂着。"""

    async def main():
        pairs: dict[str, MemoryPipe] = {}

        def factory(name: str) -> MemoryPipe:
            client, _server = MemoryPipe.pair()
            pairs[name] = client
            return client

        client = HookClient(9999, pipe_factory=factory)
        with pytest.raises(PipeError):
            await asyncio.wait_for(client.connect(timeout=0.1), timeout=2)
        await client.close()

    run(main())


def test_request_without_ack_raises():
    class MuteServer(FakeHookServer):
        async def _serve(self, pipe, kind):  # 只收不回
            while True:
                chunk = await pipe.read()
                if not chunk:
                    return
                self._readers[kind].push(chunk)

    async def main():
        server = MuteServer()
        await server.start()
        client = HookClient(server.pid, pipe_factory=server.pipes, ack_timeout=0.1)
        await client.connect(timeout=2)
        with pytest.raises(asyncio.TimeoutError):
            await client.request("arklink.ping", timeout=0.1)
        await client.close()
        await server.stop()

    run(main())


def test_close_is_idempotent():
    async def main():
        server = FakeHookServer()
        await server.start()
        client = HookClient(server.pid, pipe_factory=server.pipes)
        await client.connect(timeout=2)
        await client.close()
        await client.close()
        await server.stop()

    run(main())


def test_default_hook_dll_prefers_env(monkeypatch, tmp_path):
    dll = tmp_path / "my.dll"
    dll.write_bytes(b"MZ")
    monkeypatch.setenv("ARKLINK_HOOK_DLL", str(dll))
    assert hookproc.default_hook_dll() == str(dll)


# ---------------------------------------------------------------- 适配器
def _minimal_push(text: str = "hello") -> bytes:
    from arklink.ntqq import pb

    elem = pb.message_field(1, pb.string_field(1, text))
    rich = pb.message_field(2, elem)
    group = pb.uint_field(1, 7200123) + pb.string_field(7, "测试群")
    routing = pb.uint_field(1, 10086) + pb.message_field(8, group)
    content = pb.uint_field(1, 1) + pb.uint_field(5, 42) + pb.uint_field(6, 1700000000)
    common = (
        pb.message_field(1, routing)
        + pb.message_field(2, content)
        + pb.message_field(3, pb.message_field(1, rich))
    )
    return pb.message_field(1, common)


def test_adapter_turns_packet_into_onebot_event():
    from arklink.ntqq.hook import HookQQAdapter

    async def main():
        server = FakeHookServer()
        await server.start()
        scanner = FakeScanner(
            processes=[(server.pid, "QQ.exe")],
            pipes={f"mojo.{server.pid}.control", f"mojo.{server.pid}.recv"},
            cmdlines={server.pid: '"QQ.exe"'},
        )
        adapter = HookQQAdapter(scanner=scanner, client_factory=server.pipes, connect_timeout=2)
        events: list[dict] = []

        async def capture(raw):
            events.append(raw)

        adapter._emit_dict = capture  # type: ignore[method-assign]
        adapter._running = True
        task = asyncio.create_task(adapter.connect())
        for _ in range(100):
            if adapter.client is not None:
                break
            await asyncio.sleep(0.02)
        assert adapter.client is not None, "适配器没有建立会话"

        await server.push_login()
        await server.push_packet(_minimal_push("你好"))
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.02)

        assert events, "收包没有转成事件"
        event = events[0]
        assert event["post_type"] == "message"
        assert event["message_type"] == "group"
        assert event["group_id"] == 7200123
        assert adapter.status()["packets"] >= 1

        adapter._running = False
        await adapter.disconnect()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.stop()

    run(main())


def test_adapter_skips_injection_when_disabled():
    from arklink.ntqq.hook import HookQQAdapter

    class RecordingInjector:
        def __init__(self):
            self.calls = []

        def inject(self, pid, dll, method="loadlibrary"):
            self.calls.append((pid, dll))
            raise AssertionError("不应该注入")

    async def main():
        scanner = FakeScanner(processes=[(777, "QQ.exe")], pipes=set(), cmdlines={777: '"QQ.exe"'})
        injector = RecordingInjector()
        adapter = HookQQAdapter(scanner=scanner, injector=injector, auto_inject=False, poll_interval=0.01)
        adapter._running = True
        await asyncio.wait_for(adapter._ensure_session(), timeout=2)
        assert injector.calls == []
        assert "auto_inject=false" in adapter.last_error

    run(main())


def test_adapter_injects_then_connects():
    from arklink.ntqq.hook import HookQQAdapter

    class InjectingScanner(FakeScanner):
        def __init__(self, pid):
            super().__init__([(pid, "QQ.exe")], set(), {pid: '"QQ.exe"'})

        def inject_side_effect(self):
            self._pipes.add(f"arklink.{self._processes[0][0]}.control")

    class FakeInjector:
        def __init__(self, scanner, server):
            self.scanner = scanner
            self.server = server
            self.calls = []

        def inject(self, pid, dll, method="loadlibrary"):
            self.calls.append((pid, dll, method))
            self.scanner.inject_side_effect()
            return hookproc.InjectionResult(pid=pid, dll=dll, handle=0x1000)

    async def main():
        server = FakeHookServer()
        await server.start()
        scanner = InjectingScanner(server.pid)
        injector = FakeInjector(scanner, server)

        adapter = HookQQAdapter(
            scanner=scanner,
            injector=injector,
            client_factory=server.pipes,
            connect_timeout=2,
            dll_path="C:/fake/hook.dll",
        )
        adapter._running = True
        task = asyncio.create_task(adapter.connect())
        for _ in range(100):
            if adapter.client is not None:
                break
            await asyncio.sleep(0.02)
        assert injector.calls and injector.calls[0][0] == server.pid
        assert adapter.injected and adapter.injection is not None
        adapter._running = False
        await adapter.disconnect()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.stop()

    run(main())


def test_adapter_api_surface():
    from arklink.ntqq.hook import HookQQAdapter

    adapter = HookQQAdapter()

    async def main():
        try:
            await adapter.call_api("send_group_msg", {"group_id": 1, "message": "x"})
        except Exception as exc:
            assert getattr(exc, "retcode", None) == 1404
        else:  # pragma: no cover
            raise AssertionError("只读模式不应该能发送")
        status = await adapter.call_api("get_status")
        assert status["online"] is False

    run(main())
