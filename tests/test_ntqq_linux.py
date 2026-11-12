"""跨平台适配测试。

Windows 与 Linux 的差别只有三处：进程表、管道形态、注入方式。这里把
「可以离线验证的部分」全部测掉，真机注入仍走 CLI（见 docs/Linux适配.md）。

标注 `posix_only` 的用例在 Windows 上跳过，但在 CI（ubuntu）上会真跑 ——
Linux 的代码路径不会因为本地是 Windows 就烂掉。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from arklink.ntqq import hookproc
from arklink.ntqq.hookpipe import UnixPipe, default_pipe_factory, Win32Pipe

posix_only = pytest.mark.skipif(os.name == "nt", reason="只在 POSIX 上跑")
windows_only = pytest.mark.skipif(os.name != "nt", reason="只在 Windows 上跑")


def run(coro):
    return asyncio.run(coro)


def test_pipe_path_matches_platform():
    path = hookproc.pipe_path("arklink.123.control")
    if os.name == "nt":
        assert path == r"\\.\pipe\arklink.123.control"
    else:
        assert path == os.path.join(hookproc.runtime_dir(), "arklink.123.control")
        assert path.startswith("/")


def test_default_hook_module_name():
    found = hookproc.default_hook_dll()
    if os.name == "nt":
        assert found.endswith(".dll")
    else:
        # 没编译时返回空，但绝不能返回 .dll
        assert found == "" or found.endswith(".so")


def test_pipe_factory_picks_transport():
    pipe = default_pipe_factory("arklink.1.control")
    assert isinstance(pipe, Win32Pipe if os.name == "nt" else UnixPipe)


@posix_only
def test_proc_snapshot_contains_self():
    scanner = hookproc.ProcessScanner()
    entries = scanner._snapshot()
    assert entries, "/proc 扫不出任何进程，说明平台分支没生效"
    pids = {pid for pid, _name in entries}
    assert os.getpid() in pids


@posix_only
def test_command_line_reads_proc_cmdline():
    scanner = hookproc.ProcessScanner()
    cmdline = scanner.command_line(os.getpid())
    assert cmdline, "读不到自己的命令行"
    assert "python" in cmdline.lower() or "pytest" in cmdline.lower()


@posix_only
def test_maps_parsing_finds_libc():
    base = hookproc.mapping_base("self", "libc.so")
    assert base > 0, "解析 /proc/self/maps 失败"


@posix_only
def test_probe_missing_socket():
    scanner = hookproc.ProcessScanner()
    assert scanner.probe_pipe(os.getpid()) == "missing"


@posix_only
def test_unix_pipe_roundtrip(tmp_path):
    sock_path = str(tmp_path / "arklink.999.control")
    received: list[bytes] = []

    async def main():
        done = asyncio.Event()

        async def handler(reader, writer):
            received.append(await reader.read(1024))
            writer.write(b"pong")
            await writer.drain()
            done.set()
            writer.close()

        server = await asyncio.start_unix_server(handler, sock_path)
        pipe = UnixPipe("arklink.999.control", path=sock_path)
        await pipe.open()
        await pipe.write(b"ping")
        await asyncio.wait_for(done.wait(), timeout=3)
        assert await asyncio.wait_for(pipe.read(), timeout=3) == b"pong"
        await pipe.close()
        server.close()
        await server.wait_closed()

    run(main())
    assert received == [b"ping"]


@posix_only
def test_unix_pipe_reports_closed_peer(tmp_path):
    from arklink.ntqq.hookpipe import PipeError

    sock_path = str(tmp_path / "arklink.998.control")

    async def main():
        server = await asyncio.start_unix_server(lambda r, w: w.close(), sock_path)
        pipe = UnixPipe("arklink.998.control", path=sock_path)
        await pipe.open()
        with pytest.raises(PipeError):
            await asyncio.wait_for(pipe.read(), timeout=3)
        await pipe.close()
        server.close()
        await server.wait_closed()

    run(main())


@posix_only
def test_injector_rejects_unknown_method_on_posix(tmp_path):
    module = tmp_path / "libarklink-hook-x64.so"
    module.write_bytes(b"\x7fELF")
    injector = hookproc.Injector()
    with pytest.raises(hookproc.HookError):
        injector.inject(os.getpid(), str(module), "magic")


@posix_only
def test_injector_requires_existing_module():
    injector = hookproc.Injector()
    with pytest.raises(hookproc.HookError):
        injector.inject(os.getpid(), "/nonexistent/libarklink.so")


@windows_only
def test_windows_module_lookup_name():
    assert hookproc.pipe_path("arklink.5.recv").startswith("\\\\.\\pipe\\")
