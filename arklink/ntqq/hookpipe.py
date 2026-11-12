"""连接 QQ 进程内的 hook 管道（control + recv），做握手与请求/响应。

一个 hook 进程对外开两条命名管道：

* `\\\\.\\pipe\\<prefix>.<pid>.control` —— 请求/响应（发消息、查状态）
* `\\\\.\\pipe\\<prefix>.<pid>.recv`    —— 服务端单向推送（收到的消息包）

两条都是字节流，帧用 `hookproto` 解析。所以必须有独立的读线程做「攒字节 → 拆帧」，
不能指望 `read()` 一次刚好返回一帧——实测小包会合并、大包会被切碎。

这里刻意把「读」做成后台线程 + asyncio 队列：Win32 的 `ReadFile` 是阻塞的，
直接丢进 `run_in_executor` 会占住默认线程池里的线程，几个连接就把池子耗干了。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
from typing import Any, Callable, Protocol

from .hookproc import pipe_path
from .hookproto import (
    FLAG_WANT_REPLY,
    OP_ACK,
    OP_CAPABILITY,
    OP_ERROR,
    OP_HELLO,
    OP_LOGIN_STATE,
    OP_PACKET,
    OP_REPLY,
    Frame,
    FrameError,
    FrameReader,
    request as make_request,
)

logger = logging.getLogger("arklink.ntqq.hookpipe")

IS_WINDOWS = True
try:  # pragma: no cover - 平台分支
    import os

    IS_WINDOWS = os.name == "nt"
except Exception:  # pragma: no cover
    pass

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2
ERROR_IO_PENDING = 997
PIPE_READ_CHUNK = 64 * 1024


class OVERLAPPED(ctypes.Structure):
    """异步 I/O 的上下文。必须沿用，不能省：见 `Win32Pipe.open` 的注释。"""

    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


class PipeError(RuntimeError):
    """管道打不开/读写失败。"""


class PipeLink(Protocol):
    """一条双向字节流（生产用 Win32 命名管道，测试用内存管道）。"""

    async def read(self) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------- Win32
_k32 = None
_k32_lock = threading.Lock()


def _kernel32() -> Any:
    global _k32
    with _k32_lock:
        if _k32 is None:
            dll = ctypes.WinDLL("kernel32", use_last_error=True)
            dll.CreateFileW.restype = ctypes.c_void_p
            dll.CreateFileW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            dll.ReadFile.restype = ctypes.c_int
            dll.ReadFile.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ]
            dll.WriteFile.restype = ctypes.c_int
            dll.WriteFile.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ]
            dll.CloseHandle.restype = ctypes.c_int
            dll.CloseHandle.argtypes = [ctypes.c_void_p]
            dll.CreateEventW.restype = ctypes.c_void_p
            dll.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
            dll.WaitForSingleObject.restype = ctypes.c_uint32
            dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            dll.GetOverlappedResult.restype = ctypes.c_int
            dll.GetOverlappedResult.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(OVERLAPPED),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_int,
            ]
            dll.CancelIoEx.restype = ctypes.c_int
            dll.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            dll.ResetEvent.restype = ctypes.c_int
            dll.ResetEvent.argtypes = [ctypes.c_void_p]
            _k32 = dll
        return _k32


class Win32Pipe:
    """命名管道客户端：阻塞读跑在专线程里，转成 asyncio 队列。"""

    def __init__(self, name: str, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.name = name
        self.path = name if name.startswith("\\\\.\\pipe\\") else f"\\\\.\\pipe\\{name}"
        self._loop = loop
        self._handle: int | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._write_lock = threading.Lock()

    def open(self, timeout_ms: int = 5000) -> None:
        if not IS_WINDOWS:
            raise PipeError("命名管道只在 Windows 上可用")
        k32 = _kernel32()
        # 必须带 FILE_FLAG_OVERLAPPED：同步句柄上「读挂起」会把同一句柄的写也堵住，
        # 表现就是握手正常、一发请求就永远不返回。
        handle = k32.CreateFileW(
            self.path,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            err = ctypes.get_last_error()
            hint = {
                ERROR_PIPE_BUSY: "管道被别的客户端占用（先关掉那个客户端再试）",
                ERROR_FILE_NOT_FOUND: "管道不存在（hook 模块没加载，或 QQ 已经退出）",
                5: "拒绝访问（试试以管理员身份运行）",
            }.get(err, "")
            raise PipeError(f"打开 {self.path} 失败 err={err} {hint}".strip())
        self._handle = int(handle)
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
        self._thread = threading.Thread(target=self._pump, name=f"pipe{self.path}", daemon=True)
        self._thread.start()
        logger.debug("管道已连接：%s", self.path)

    def _pump(self) -> None:
        assert self._handle is not None
        k32 = _kernel32()
        handle = ctypes.c_void_p(self._handle)
        buf = ctypes.create_string_buffer(PIPE_READ_CHUNK)
        read = ctypes.c_uint32(0)
        ov = OVERLAPPED()
        ov.hEvent = k32.CreateEventW(None, True, False, None)
        try:
            while not self._closed:
                ov.Internal = 0
                ov.InternalHigh = 0
                k32.ResetEvent(ctypes.c_void_p(ov.hEvent))
                ok = k32.ReadFile(handle, buf, PIPE_READ_CHUNK, ctypes.byref(read), ctypes.byref(ov))
                if not ok:
                    err = ctypes.get_last_error()
                    if err != ERROR_IO_PENDING:
                        break
                    k32.WaitForSingleObject(ctypes.c_void_p(ov.hEvent), 0xFFFFFFFF)
                    if not k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(read), False):
                        break
                if read.value == 0:
                    break
                self._push(buf.raw[: read.value])
        finally:
            if ov.hEvent:
                k32.CloseHandle(ctypes.c_void_p(ov.hEvent))
        self._push(b"")  # EOF 哨兵

    def _push(self, data: bytes) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._queue.put_nowait, data)
        except RuntimeError:  # loop 已关闭，退出时常见
            pass

    async def read(self) -> bytes:
        return await self._queue.get()

    async def write(self, data: bytes) -> None:
        if self._handle is None:
            raise PipeError("管道没有打开")
        k32 = _kernel32()
        handle = ctypes.c_void_p(self._handle)
        with self._write_lock:
            view = memoryview(data)
            sent_total = 0
            while sent_total < len(view):
                chunk = bytes(view[sent_total : sent_total + PIPE_READ_CHUNK])
                wrote = ctypes.c_uint32(0)
                ov = OVERLAPPED()
                ov.hEvent = k32.CreateEventW(None, True, False, None)
                try:
                    ok = k32.WriteFile(handle, chunk, len(chunk), ctypes.byref(wrote), ctypes.byref(ov))
                    if not ok:
                        err = ctypes.get_last_error()
                        if err != ERROR_IO_PENDING:
                            raise PipeError(f"写 {self.path} 失败 err={err}")
                        k32.WaitForSingleObject(ctypes.c_void_p(ov.hEvent), 0xFFFFFFFF)
                        if not k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(wrote), False):
                            raise PipeError(f"写 {self.path} 未完成 err={ctypes.get_last_error()}")
                finally:
                    if ov.hEvent:
                        k32.CloseHandle(ctypes.c_void_p(ov.hEvent))
                if wrote.value == 0:
                    raise PipeError("管道写入 0 字节")
                sent_total += wrote.value

    async def close(self) -> None:
        self._closed = True
        if self._handle is not None:
            k32 = _kernel32()
            k32.CancelIoEx(ctypes.c_void_p(self._handle), None)
            k32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None


class MemoryPipe:
    """内存管道（测试/回放用）：两端各一个队列。"""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self._outbox: asyncio.Queue[bytes] | None = None
        self.closed = False

    @classmethod
    def pair(cls) -> tuple["MemoryPipe", "MemoryPipe"]:
        client, server = cls(), cls()
        client._outbox = server._inbox
        server._outbox = client._inbox
        return client, server

    async def read(self) -> bytes:
        return await self._inbox.get()

    async def write(self, data: bytes) -> None:
        if self._outbox is None:
            raise PipeError("内存管道没有对端")
        await self._outbox.put(data)

    async def close(self) -> None:
        self.closed = True


class UnixPipe:
    """Unix domain socket 版管道（Linux/macOS）。

    和 Windows 命名管道最大的差别：**没有「独占」概念**，没有 overlapped I/O，
    也没有 ERROR_PIPE_BUSY。所以直接用 asyncio 的流，一行线程都不用起。
    socket 文件放在 `$XDG_RUNTIME_DIR` 或 `/tmp`，见 `hookproc.pipe_path`。
    """

    def __init__(self, name: str, path: str = "") -> None:
        self.name = name
        self.path = path or pipe_path(name)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False

    async def open(self, timeout: float = 5.0) -> None:
        if self._reader is not None:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.path), timeout=timeout
            )
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise PipeError(f"连不上 {self.path}：{exc}") from exc

    async def read(self) -> bytes:
        if self._reader is None:
            raise PipeError("管道没有打开")
        data = await self._reader.read(PIPE_READ_CHUNK)
        if not data:
            raise PipeError(f"{self.path} 对端关闭")
        return data

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise PipeError("管道没有打开")
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        self._closed = True
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None


def default_pipe_factory(name: str) -> "PipeLink":
    """按平台给一个管道实现。名字是裸名（`arklink.<pid>.control`）。"""
    if IS_WINDOWS:
        return Win32Pipe(name)
    return UnixPipe(name)



# ---------------------------------------------------------------- 客户端
class Hello:
    __slots__ = ("pipe_name", "pid", "recv")

    def __init__(self, frame: Frame, recv: bool) -> None:
        self.pipe_name = frame.msg
        self.pid = int(frame.value0)
        self.recv = recv

    def __repr__(self) -> str:
        kind = "recv" if self.recv else "control"
        return f"<Hello {kind} pid={self.pid} pipe={self.pipe_name!r}>"


class HookClient:
    """一条 QQ 进程的 hook 会话。

    用法：

        client = HookClient(35512)
        await client.connect()          # 连两条管道并完成 HELLO
        await client.start_recv()       # 开始收包
        client.on_packet = handler      # op=6 的包
        reply = await client.request("arklink.ping")
    """

    def __init__(
        self,
        pid: int,
        prefix: str = "mojo",
        *,
        ack_timeout: float = 5.0,
        reply_timeout: float = 15.0,
        pipe_factory: Callable[[str], PipeLink] | None = None,
    ) -> None:
        self.pid = int(pid)
        self.prefix = prefix
        self.ack_timeout = ack_timeout
        self.reply_timeout = reply_timeout
        self.on_packet: Callable[[Frame], Any] | None = None
        self.on_login_state: Callable[[Frame], Any] | None = None
        self.on_capability: Callable[[Frame], Any] | None = None
        self.on_close: Callable[[str], Any] | None = None

        self.control_hello: Hello | None = None
        self.recv_hello: Hello | None = None
        self.login_frame: Frame | None = None

        self._factory = pipe_factory or default_pipe_factory
        self._control: PipeLink | None = None
        self._recv: PipeLink | None = None
        self._control_reader = FrameReader()
        self._recv_reader = FrameReader()
        self._tasks: list[asyncio.Task[Any]] = []
        self._next_id = 1
        self._acks: dict[int, asyncio.Future[Frame]] = {}
        self._replies: dict[int, asyncio.Future[Frame]] = {}
        self._closed = False

    # ---------------- 连接 ----------------
    @property
    def pipe_names(self) -> tuple[str, str]:
        return (f"{self.prefix}.{self.pid}.control", f"{self.prefix}.{self.pid}.recv")

    async def connect(self, timeout: float = 10.0) -> tuple[Hello, Hello]:
        control_name, recv_name = self.pipe_names
        self._control = self._factory(control_name)
        await self._open(self._control)
        self._control_task = asyncio.create_task(self._pump(self._control, self._control_reader, "control"))
        self._tasks.append(self._control_task)
        self.control_hello = await self._wait_hello(True, timeout)

        self._recv = self._factory(recv_name)
        await self._open(self._recv)
        self._recv_task = asyncio.create_task(self._pump(self._recv, self._recv_reader, "recv"))
        self._tasks.append(self._recv_task)
        self.recv_hello = await self._wait_hello(False, timeout)
        logger.info("hook 已连接 pid=%d（%s）", self.pid, control_name)
        return self.control_hello, self.recv_hello

    async def attach(self, control: PipeLink, recv: PipeLink) -> tuple[Hello, Hello]:
        """直接接管两条已有连接（测试用；连接不一定叫 mojo）。"""
        self._control, self._recv = control, recv
        self._tasks.append(asyncio.create_task(self._pump(control, self._control_reader, "control")))
        self._tasks.append(asyncio.create_task(self._pump(recv, self._recv_reader, "recv")))
        hello_c = await self._wait_hello(True, 5.0)
        hello_r = await self._wait_hello(False, 5.0)
        return hello_c, hello_r

    async def _open(self, pipe: PipeLink) -> None:
        opener = getattr(pipe, "open", None)
        if callable(opener):
            try:
                opener()
            except TypeError:  # 某些实现不需要参数
                opener()

    async def _wait_hello(self, control: bool, timeout: float) -> Hello:
        """HELLO 由服务端在 accept 后主动发，读循环会把它塞进 future。"""
        future: asyncio.Future[Hello] = asyncio.get_running_loop().create_future()
        if control:
            self._control_hello_future = future
        else:
            self._recv_hello_future = future
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            kind = "control" if control else "recv"
            raise PipeError(f"{kind} 管道在 {timeout:.0f}s 内没有收到 HELLO（hook 模块可能没启动）") from exc

    # ---------------- 读循环 ----------------
    async def _pump(self, pipe: PipeLink, reader: FrameReader, kind: str) -> None:
        try:
            while not self._closed:
                chunk = await pipe.read()
                if not chunk:
                    break
                try:
                    frames = reader.push(chunk)
                except FrameError as exc:
                    logger.error("%s 管道帧错误：%s", kind, exc)
                    break
                for frame in frames:
                    self._dispatch(frame, kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s 管道读循环异常", kind)
        finally:
            if not self._closed:
                logger.warning("%s 管道关闭（pid=%d）", kind, self.pid)
                if self.on_close is not None:
                    try:
                        self.on_close(kind)
                    except Exception:
                        logger.exception("on_close 回调异常")

    def _dispatch(self, frame: Frame, kind: str) -> None:
        op = frame.op
        if op == OP_HELLO:
            future = getattr(self, "_recv_hello_future" if kind == "recv" else "_control_hello_future", None)
            if future is not None and not future.done():
                future.set_result(Hello(frame, recv=kind == "recv"))
            return
        if op == OP_LOGIN_STATE:
            self.login_frame = frame
            logger.info("登录态变化：logged_in=%s uin=%s", frame.logged_in, frame.uin)
            if self.on_login_state is not None:
                self.on_login_state(frame)
            return
        if op == OP_CAPABILITY:
            if self.on_capability is not None:
                self.on_capability(frame)
            return
        if op == OP_PACKET:
            if self.on_packet is not None:
                self.on_packet(frame)
            return
        if op == OP_ACK:
            future = self._acks.get(frame.request_id)
            if future is not None and not future.done():
                future.set_result(frame)
            return
        if op == OP_REPLY:
            future = self._replies.get(frame.request_id)
            if future is not None and not future.done():
                future.set_result(frame)
            return
        if op == OP_ERROR:
            message = frame.msg or f"hook 返回错误 status={frame.status}"
            for table in (self._acks, self._replies):
                future = table.get(frame.request_id)
                if future is not None and not future.done():
                    future.set_exception(PipeError(message))
            return
        logger.debug("未知帧 op=%d kind=%s（忽略）", op, kind)

    # ---------------- 请求 ----------------
    async def request(self, cmd: str, body: bytes = b"", *, want_reply: bool = True, timeout: float | None = None) -> Frame:
        if self._control is None:
            raise PipeError("control 管道未连接")
        rid = self._next_id
        self._next_id = self._next_id + 1 if self._next_id < 0xFFFFFFFE else 1
        loop = asyncio.get_running_loop()
        ack: asyncio.Future[Frame] = loop.create_future()
        self._acks[rid] = ack
        reply_future: asyncio.Future[Frame] | None = None
        if want_reply:
            reply_future = loop.create_future()
            self._replies[rid] = reply_future
        try:
            await self._control.write(make_request(cmd, body, rid, want_reply).encode())
            await asyncio.wait_for(ack, timeout or self.ack_timeout)
            if not want_reply or reply_future is None:
                return Frame(op=OP_ACK, request_id=rid)
            return await asyncio.wait_for(reply_future, timeout or self.reply_timeout)
        finally:
            self._acks.pop(rid, None)
            self._replies.pop(rid, None)

    # ---------------- 生命周期 ----------------
    @property
    def logged_in(self) -> bool:
        return bool(self.login_frame and self.login_frame.logged_in)

    @property
    def uin(self) -> str:
        return self.login_frame.uin if self.login_frame else ""

    async def close(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - 收尾阶段异常不影响关闭
                logger.debug("等待读循环退出时异常", exc_info=True)
        self._tasks.clear()
        for pipe in (self._control, self._recv):
            if pipe is not None:
                try:
                    await pipe.close()
                except Exception:
                    logger.debug("关闭管道失败", exc_info=True)
        self._control = self._recv = None
        logger.debug("hook 会话已关闭 pid=%d", self.pid)
