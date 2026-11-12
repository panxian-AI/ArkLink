"""找到 QQ 主进程、把 hook 模块塞进去。

这里的三个动作各有各的坑：

1. **找主进程**。QQ NT 是 Electron，一个 QQ 起来会有十几个 `QQ.exe`，渲染进程、
   GPU 进程、utility 进程都在列表里。往渲染进程注入拿不到消息，只会白折腾，
   所以必须先按命令行里的 `--type=` 把它们剔掉。

2. **判断有没有现成的 hook**。管道存在（`\\\\.\\pipe\\mojo.<pid>.control`）就说明
   已经有人注入过了，直接连；连不上（`ERROR_PIPE_BUSY=231`）说明被别的客户端
   占着，这时再注入只会打架。

3. **注入**。默认走 `CreateRemoteThread(LoadLibraryW)`：通用、能拿到 HMODULE、
   可以再用 `FreeLibrary` 干净卸载。代价是模块会出现在 PEB 模块链表里，
   想更隐蔽就得自己写 manual map（见 `docs/进程注入通道.md` 的路线对比）。

所有 Win32 调用都用 ctypes 直连，不引第三方库——这个包要能 `pip install` 后
在任何 Python 3.11+ 上跑起来，而不是先装编译器。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

logger = logging.getLogger("arklink.ntqq.hook")

IS_WINDOWS = os.name == "nt"

#: QQ 主进程的候选可执行名。
#: Windows: QQNT 是 `QQ.exe`（旧版 QQScLauncher.exe）；
#: Linux:  官方 Linux 版 QQNT 的二进制叫 `qq`（包内是 /opt/QQ/qq）。
DEFAULT_EXE_NAMES: tuple[str, ...] = ("QQ.exe", "QQ", "qq")

#: 管道前缀。自有 DLL 用 arklink，现成 hook 用 mojo
PIPE_PREFIXES: tuple[str, ...] = ("mojo", "arklink")


def is_windows() -> bool:
    return os.name == "nt"


def runtime_dir() -> str:
    """Linux 上放 socket 文件的地方（Windows 不用）。"""
    return os.environ.get("XDG_RUNTIME_DIR") or "/tmp"


def pipe_path(name: str) -> str:
    """把裸名字（`arklink.123.control`）翻译成平台上的实际地址。

    - Windows：`\\\\.\\pipe\\<name>`（内核命名管道，不落磁盘）
    - Linux：`<runtime_dir>/<name>`（Unix domain socket 文件）
    - macOS：同上，Unix domain socket
    """
    if is_windows():
        return f"\\\\.\\pipe\\{name}"
    return os.path.join(runtime_dir(), name)

# ---- Win32 常量 ----
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PROCESS_CREATE_THREAD = 0x0002
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_BROKEN_PIPE = 109

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class HookError(RuntimeError):
    """注入/进程发现失败。消息里带 Win32 错误码，方便对着文档查。"""


# ---------------------------------------------------------------- 结构体
if IS_WINDOWS:

    class PROCESSENTRY32W(ctypes.Structure):
        """注意：字段必须定宽。

        `ctypes.c_long` 在 Linux 是 8 字节、Windows 是 4 字节；照抄网上示例会让
        结构体大小从 568 变成 1088，`Process32FirstW` 直接返回 FALSE。
        """

        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_uint64),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_int32),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_uint16 * 260),
        ]

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("th32ModuleID", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("GlblcntUsage", ctypes.c_uint32),
            ("ProccntUsage", ctypes.c_uint32),
            ("modBaseAddr", ctypes.c_void_p),
            ("modBaseSize", ctypes.c_uint32),
            ("hModule", ctypes.c_void_p),
            ("szModule", ctypes.c_uint16 * 256),
            ("szExePath", ctypes.c_uint16 * 260),
        ]


_K32 = None
_NTDLL = None


def _kernel32() -> "ctypes.WinDLL":
    """绑定一次 argtypes，全进程复用。

    不设 restype 的话 ctypes 按 `c_int` 处理返回值，64 位句柄会被截成 32 位——
    `GetModuleHandleW("kernel32.dll")` 就会返回一个假的句柄，再拿它去
    `GetProcAddress` 必然得到 0，报错信息还是「拿不到 LoadLibraryW 地址」，
    跟真正的原因（句柄被截断）完全不搭边。
    """
    global _K32
    if _K32 is not None:
        return _K32
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    dll.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    dll.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    dll.Process32FirstW.restype = ctypes.c_int
    dll.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    dll.Process32NextW.restype = ctypes.c_int
    dll.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
    dll.Module32FirstW.restype = ctypes.c_int
    dll.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]
    dll.Module32NextW.restype = ctypes.c_int
    dll.CloseHandle.argtypes = [ctypes.c_void_p]
    dll.CloseHandle.restype = ctypes.c_int
    dll.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    dll.OpenProcess.restype = ctypes.c_void_p
    dll.VirtualAllocEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    dll.VirtualAllocEx.restype = ctypes.c_void_p
    dll.VirtualFreeEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    dll.VirtualFreeEx.restype = ctypes.c_int
    dll.WriteProcessMemory.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    dll.WriteProcessMemory.restype = ctypes.c_int
    dll.ReadProcessMemory.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    dll.ReadProcessMemory.restype = ctypes.c_int
    dll.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    dll.GetModuleHandleW.restype = ctypes.c_void_p
    dll.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dll.GetProcAddress.restype = ctypes.c_void_p
    dll.CreateRemoteThread.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    dll.CreateRemoteThread.restype = ctypes.c_void_p
    dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    dll.WaitForSingleObject.restype = ctypes.c_uint32
    dll.GetExitCodeThread.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    dll.GetExitCodeThread.restype = ctypes.c_int
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
    _K32 = dll
    return dll


def _ntdll() -> "ctypes.WinDLL":
    global _NTDLL
    if _NTDLL is not None:
        return _NTDLL
    dll = ctypes.WinDLL("ntdll", use_last_error=True)
    dll.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    dll.NtQueryInformationProcess.restype = ctypes.c_int32
    _NTDLL = dll
    return dll


def export_rva(dll_path: str, name: str) -> int:
    """读 PE 导出表，返回某个导出函数的 RVA（0 = 没有）。

    远程调用 DLL 里的函数需要「本地 base + RVA」：同一个文件映射到进程里，
    节内偏移一致，所以 RVA 可以直接加远程模块基址。
    """
    try:
        data = open(dll_path, "rb").read()
    except OSError:
        return 0
    if len(data) < 0x40 or data[:2] != b"MZ":
        return 0
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        return 0
    nsections = struct.unpack_from("<H", data, pe + 6)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    exp_rva, _size = struct.unpack_from("<II", data, dd)
    if not exp_rva:
        return 0
    sections = []
    for i in range(nsections):
        off = opt + struct.unpack_from("<H", data, pe + 20)[0] + i * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((vaddr, max(vsize, rawsize), rawptr))

    def to_off(rva: int) -> int | None:
        for vaddr, size, rawptr in sections:
            if vaddr <= rva < vaddr + size:
                return rawptr + (rva - vaddr)
        return None

    exp = to_off(exp_rva)
    if exp is None:
        return 0
    nnames = struct.unpack_from("<I", data, exp + 24)[0]
    names_off = to_off(struct.unpack_from("<I", data, exp + 32)[0])
    funcs_off = to_off(struct.unpack_from("<I", data, exp + 28)[0])
    if names_off is None or funcs_off is None:
        return 0
    for i in range(nnames):
        name_off = to_off(struct.unpack_from("<I", data, names_off + i * 4)[0])
        if name_off is None:
            continue
        end = data.index(b"\0", name_off)
        if data[name_off:end].decode("ascii", "replace") != name:
            continue
        ordinal = struct.unpack_from("<H", data, names_off + nnames * 4 + i * 2)[0]
        return struct.unpack_from("<I", data, funcs_off + ordinal * 4)[0]
    return 0


def _decode_wide(buf: Sequence[int]) -> str:
    """`c_uint16 * N` → str。

    必须按 2 字节一个字符走到第一个 0x0000 为止：直接 `raw.split(b"\\x00\\x00")`
    会在 ASCII 字符的「高位零 + 下个字符低位零」处误判，把 "QQ.exe" 切成
    "QQ.ex" + 半个码元，最后得到一串乱码。
    """
    units: list[int] = []
    for ch in buf:
        value = int(ch)
        if value == 0:
            break
        units.append(value)
    raw = b"".join(value.to_bytes(2, "little") for value in units)
    return raw.decode("utf-16-le", "replace")


# ---------------------------------------------------------------- 数据
@dataclass(slots=True)
class QQProcess:
    pid: int
    name: str
    cmdline: str = ""
    is_main: bool = False
    pipe: str = ""
    pipe_state: str = "missing"  # missing / busy / free

    def as_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "name": self.name,
            "main": self.is_main,
            "cmdline": self.cmdline,
            "pipe": self.pipe,
            "pipe_state": self.pipe_state,
        }


@dataclass(slots=True)
class InjectionResult:
    pid: int
    dll: str
    handle: int = 0
    method: str = "loadlibrary"
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "dll": self.dll,
            "handle": self.handle,
            "method": self.method,
            "note": self.note,
        }


def looks_like_main(cmdline: str, name: str = "QQ.exe", exe_names: Iterable[str] = DEFAULT_EXE_NAMES) -> bool:
    """纯逻辑判定，方便单测。

    Electron 的子进程都带 `--type=renderer/gpu-process/utility/...`，
    主进程没有这个开关；读不到命令行时宁可不判定（调用方会退化成「全都试」）。
    """
    if name.lower() not in {n.lower() for n in exe_names}:
        return False
    if not cmdline:
        return True
    lowered = cmdline.lower()
    return "--type=" not in lowered


# ---------------------------------------------------------------- 扫描器
class ProcessScanner:
    """把「进程表 / 管道表 / 命令行」包成可替换的三个方法（测试注入假数据）。"""

    def __init__(self, pipe_prefixes: Iterable[str] = PIPE_PREFIXES) -> None:
        self.pipe_prefixes = tuple(pipe_prefixes)

    # -- 进程表 --
    def _snapshot(self) -> list[tuple[int, str]]:
        if not IS_WINDOWS:
            return self._snapshot_posix()
        k32 = _kernel32()
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE or not snap:
            raise HookError(f"CreateToolhelp32Snapshot 失败 err={ctypes.get_last_error()}")
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            out: list[tuple[int, str]] = []
            ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
            while ok:
                out.append((int(entry.th32ProcessID), _decode_wide(entry.szExeFile)))
                ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
            return out
        finally:
            k32.CloseHandle(ctypes.c_void_p(snap))

    # -- 命令行 --
    def _snapshot_posix(self) -> list[tuple[int, str]]:
        """Linux/macOS：遍历 /proc（macOS 没 /proc，走 ps 兜底）。"""
        out: list[tuple[int, str]] = []
        proc = "/proc"
        if os.path.isdir(proc):
            for entry in os.listdir(proc):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                name = ""
                try:
                    with open(f"{proc}/{entry}/comm", "r", encoding="utf-8", errors="replace") as fh:
                        name = fh.read().strip()
                except OSError:
                    continue
                if not name:
                    continue
                out.append((pid, name))
            return out
        # macOS / 没有 /proc 的系统
        import subprocess

        try:
            text = subprocess.run(
                ["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        for line in text.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            out.append((int(parts[0]), os.path.basename(parts[1].strip())))
        return out

    def command_line(self, pid: int) -> str:
        """读目标进程的完整命令行。读不到就返回空串，不抛。"""
        if not IS_WINDOWS:
            return self._command_line_posix(pid)
        handle = None
        k32 = _kernel32()
        try:
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
            if not handle:
                return ""
            size = 0x10000
            buf = ctypes.create_string_buffer(size)
            ret_len = ctypes.c_uint32(0)
            status = _ntdll().NtQueryInformationProcess(
                ctypes.c_void_p(handle), 60, buf, size, ctypes.byref(ret_len)  # 60 = ProcessCommandLineInformation
            )
            if status != 0:
                return ""
            length, _max = struct.unpack_from("<HH", buf.raw, 0)
            pointer = struct.unpack_from("<Q", buf.raw, 8)[0]
            if not pointer or length == 0:
                return ""
            base = ctypes.addressof(buf)
            if base <= pointer < base + size:
                # Win10+ 的 ProcessCommandLineInformation 把字符串直接内联在
                # 调用者缓冲区里（Buffer 指向 buffer+16），此时读自己的内存即可；
                # 如果照着「指针指向目标进程」去 ReadProcessMemory，会稳定拿到
                # ERROR_PARTIAL_COPY(299)。
                return buf.raw[pointer - base : pointer - base + length].decode("utf-16-le", "replace")
            raw = ctypes.create_string_buffer(length)
            read = ctypes.c_size_t(0)
            if not k32.ReadProcessMemory(
                ctypes.c_void_p(handle), ctypes.c_void_p(pointer), raw, length, ctypes.byref(read)
            ):
                return ""
            return raw.raw[: read.value].decode("utf-16-le", "replace")
        except Exception as exc:  # pragma: no cover - 权限问题很常见
            logger.debug("读 pid=%d 命令行失败: %s", pid, exc)
            return ""
        finally:
            if handle:
                k32.CloseHandle(ctypes.c_void_p(handle))

    # -- 管道表 --
    def _command_line_posix(self, pid: int) -> str:
        """`/proc/<pid>/cmdline` 是 NUL 分隔的，转成空格方便统一判断 `--type=`。"""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read(64 * 1024)
        except OSError:
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()

    def pipe_names(self) -> set[str]:
        if not IS_WINDOWS:
            return self._pipe_names_posix()
        try:
            return {n for n in os.listdir("\\\\.\\pipe\\")}
        except OSError:
            return set()

    def _pipe_names_posix(self) -> set[str]:
        """Unix domain socket 是磁盘上的文件，列目录即可（抽象命名空间列不出来，
        所以我们的 .so 固定用文件形式，见 docs/Linux适配.md）。"""
        found: set[str] = set()
        for base in {runtime_dir(), "/tmp"}:
            try:
                entries = os.listdir(base)
            except OSError:
                continue
            for name in entries:
                if name.endswith((".control", ".recv")) and "." in name:
                    found.add(name)
        return found

    def pipe_name(self, pid: int, suffix: str = "control") -> str:
        for prefix in self.pipe_prefixes:
            candidate = f"{prefix}.{pid}.{suffix}"
            if candidate in self.pipe_names():
                return candidate
        return ""

    def probe_pipe(self, pid: int, suffix: str = "control") -> str:
        """只连不看：missing（没有）/ busy（有但被占）/ free（能拿到）。

        `busy` 是正常状态——说明别的客户端已经接管了这条管道，此时再注入
        会把对方挤掉，所以调用方要能区分。
        """
        if not IS_WINDOWS:
            return self._probe_socket_posix(pid, suffix)
        name = self.pipe_name(pid, suffix)
        if not name:
            return "missing"
        k32 = _kernel32()
        handle = k32.CreateFileW(f"\\\\.\\pipe\\{name}", 0x80000000 | 0x40000000, 0, None, 3, 0, None)
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            err = ctypes.get_last_error()
            return "busy" if err == ERROR_PIPE_BUSY else "missing"
        k32.CloseHandle(ctypes.c_void_p(handle))
        return "free"

    def _probe_socket_posix(self, pid: int, suffix: str = "control") -> str:
        import socket

        name = self.pipe_name(pid, suffix)
        if not name:
            return "missing"
        path = pipe_path(name)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        try:
            sock.connect(path)
            return "free"
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
            # Unix socket 没有 Windows 那种「在但被独占」的状态：
            # 有人 listen 就能连（多个客户端也可能同时连），没人 listen 就是 missing。
            return "missing"
        finally:
            sock.close()

    # -- 组合 --
    def scan(self, exe_names: Iterable[str] = DEFAULT_EXE_NAMES) -> list[QQProcess]:
        wanted = {n.lower() for n in exe_names}
        result: list[QQProcess] = []
        for pid, name in self._snapshot():
            if name.lower() not in wanted:
                continue
            cmdline = self.command_line(pid)
            item = QQProcess(pid=pid, name=name, cmdline=cmdline, is_main=looks_like_main(cmdline, name, exe_names))
            if item.is_main:
                item.pipe = self.pipe_name(pid)
                item.pipe_state = self.probe_pipe(pid) if item.pipe else "missing"
            result.append(item)
        # 主进程排前面，主进程里「有管道」的再排前面
        result.sort(key=lambda p: (not p.is_main, p.pipe_state != "free", p.pid))
        return result


# ---------------------------------------------------------------- 注入器
class Injector:
    """远程线程注入。失败一律抛 `HookError`，不吞。"""

    def __init__(self, scanner: ProcessScanner | None = None) -> None:
        self.scanner = scanner or ProcessScanner()

    # -- 公开 API --
    def inject(self, pid: int, dll_path: str, method: str = "loadlibrary") -> InjectionResult:
        if not os.path.isfile(dll_path):
            raise HookError(f"模块文件不存在：{dll_path}")
        if not IS_WINDOWS:
            return self._inject_posix(pid, dll_path, method)
        if method != "loadlibrary":
            raise HookError(f"暂不支持的注入方式 {method!r}（当前只有 loadlibrary）")
        kernel = _kernel32()
        k32 = kernel
        k32.OpenProcess.restype = ctypes.c_void_p
        access = (
            PROCESS_CREATE_THREAD
            | PROCESS_VM_OPERATION
            | PROCESS_VM_WRITE
            | PROCESS_VM_READ
            | PROCESS_QUERY_INFORMATION
        )
        handle = k32.OpenProcess(access, False, pid)
        if not handle:
            raise HookError(
                f"OpenProcess(pid={pid}) 失败 err={ctypes.get_last_error()}"
                "（常见原因：需要管理员权限、或 QQ 是提升权限启动的）"
            )
        try:
            path_bytes = (os.path.abspath(dll_path).encode("utf-16-le") + b"\x00\x00")
            k32.VirtualAllocEx.restype = ctypes.c_void_p
            remote = k32.VirtualAllocEx(
                ctypes.c_void_p(handle), None, len(path_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
            )
            if not remote:
                raise HookError(f"VirtualAllocEx 失败 err={ctypes.get_last_error()}")
            written = ctypes.c_size_t(0)
            if not k32.WriteProcessMemory(
                ctypes.c_void_p(handle),
                ctypes.c_void_p(remote),
                path_bytes,
                len(path_bytes),
                ctypes.byref(written),
            ):
                raise HookError(f"WriteProcessMemory 失败 err={ctypes.get_last_error()}")

            load_library = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
            if not load_library:
                raise HookError("拿不到 LoadLibraryW 地址")

            k32.CreateRemoteThread.restype = ctypes.c_void_p
            thread = k32.CreateRemoteThread(
                ctypes.c_void_p(handle), None, 0, ctypes.c_void_p(load_library), ctypes.c_void_p(remote), 0, None
            )
            if not thread:
                raise HookError(f"CreateRemoteThread 失败 err={ctypes.get_last_error()}")
            try:
                wait = k32.WaitForSingleObject(ctypes.c_void_p(thread), 15000)
                if wait != WAIT_OBJECT_0:
                    raise HookError(f"等待注入线程超时/失败 wait={wait}")
                exit_code = ctypes.c_uint32(0)
                k32.GetExitCodeThread(ctypes.c_void_p(thread), ctypes.byref(exit_code))
            finally:
                k32.CloseHandle(ctypes.c_void_p(thread))
                k32.VirtualFreeEx(ctypes.c_void_p(handle), ctypes.c_void_p(remote), 0, MEM_RELEASE)

            base = int(exit_code.value or 0)
            note = "LoadLibraryW 返回 0，DLL 可能被 DllMain 拒绝加载" if not base else ""
            logger.info("已注入 %s → pid=%d base=0x%x", dll_path, pid, base)
            return InjectionResult(pid=pid, dll=os.path.abspath(dll_path), handle=base, note=note)
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))

    def _remote_call(self, pid: int, address: int, argument: int = 0, wait_ms: int = 5000) -> bool:
        """在目标进程里跑一个函数。

        `argument` 会作为 lpParameter 传进去：x64 下第一个整型参数走 rcx，
        CreateRemoteThread 会把 lpParameter 放进 rcx，所以「一个指针参数」的函数
        （FreeLibrary(HMODULE)、自定义的 `void f(void*)`）都能直接调。
        """
        k32 = _kernel32()
        handle = k32.OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            thread = k32.CreateRemoteThread(
                ctypes.c_void_p(handle),
                None,
                0,
                ctypes.c_void_p(address),
                ctypes.c_void_p(argument) if argument else None,
                0,
                None,
            )
            if not thread:
                return False
            try:
                k32.WaitForSingleObject(ctypes.c_void_p(thread), wait_ms)
            finally:
                k32.CloseHandle(ctypes.c_void_p(thread))
            return True
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))

    def unload(self, pid: int, module_base: int, dll_path: str = "") -> bool:
        """卸载。顺序很关键：先让 DLL 自己停线程，再 FreeLibrary。

        直接 FreeLibrary 会把代码段先撤掉，而工作线程还阻塞在 ReadFile 上，
        下一次返回就跳进已释放内存——目标进程会直接消失。所以有
        `ArkLinkPrepareUnload` 导出时必须先远程调它，并给它时间 Join。
        """
        if not IS_WINDOWS:
            return self._unload_posix(pid, dll_path)
        prepared = False
        if dll_path:
            rva = export_rva(dll_path, "ArkLinkPrepareUnload")
            if rva:
                prepared = self._remote_call(pid, module_base + rva, wait_ms=5000)
                if prepared:
                    time.sleep(0.3)
        k32 = _kernel32()
        free_addr = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"FreeLibrary")
        if not free_addr:
            raise HookError("拿不到 FreeLibrary 地址")
        ok = self._remote_call(pid, free_addr, argument=module_base, wait_ms=5000)
        if not ok and prepared:
            logger.warning("FreeLibrary 调用失败（模块可能仍在进程内，但线程已停）")
        return ok


    # -- POSIX（Linux/macOS）--
    def _inject_posix(self, pid: int, so_path: str, method: str) -> InjectionResult:
        import platform
        import sys

        if not sys.platform.startswith("linux"):
            raise HookError("POSIX 注入目前只实现 Linux（macOS 需要 task_for_pid，未做）")
        if platform.machine().lower() not in ("x86_64", "amd64"):
            raise HookError(f"暂不支持 {platform.machine()}，目前只有 x86_64")
        if method not in ("ptrace", "dlopen", "loadlibrary"):
            raise HookError(f"Linux 上只支持 ptrace 注入，收到 {method!r}")

        path = os.path.abspath(so_path)
        base = self._remote_call_posix(pid, "dlopen", arg_str=path, extra_int=RTLD_NOW)
        note = ""
        if not base:
            note = "dlopen 返回 0：模块可能没加载成功（路径权限 / 依赖缺失 / 已被加载）"
            logger.warning(note)
        else:
            logger.info("已注入 %s → pid=%d base=0x%x（ptrace+dlopen）", path, pid, base)
        return InjectionResult(pid=pid, dll=path, handle=base, note=note, method="ptrace")

    def _unload_posix(self, pid: int, so_path: str) -> bool:
        base = _module_base_posix(pid, os.path.basename(so_path)) if so_path else 0
        if not base:
            logger.warning("目标进程里没找到 %s，无需卸载", so_path)
            return False
        result = self._remote_call_posix(pid, "dlclose", arg_int=base)
        return result == 0

    def _remote_call_posix(
        self,
        pid: int,
        symbol: str,
        arg_str: str = "",
        arg_int: int = 0,
        extra_int: int = 0,
        timeout: float = 10.0,
    ) -> int:
        """在目标进程里调一次 `symbol(arg, extra)`，返回 rax。

        借用的是目标已有的某个线程：停下 → 改寄存器 → 继续 → 再停下 → 还原。
        返回地址故意写 0：函数返回时会触发 SIGSEGV，被我们（tracer）截获，
        目标进程自己永远看不到这个信号。
        """
        offset = _symbol_offset("libc.so.6", symbol)
        remote_func = mapping_base(pid, "libc.so.6") + offset
        if not remote_func:
            raise HookError(f"目标进程里找不到 libc.so.6（pid={pid}）")

        _ptrace(PTRACE_ATTACH, pid)
        original = UserRegs()
        try:
            _wait_stopped(pid, timeout)
            _ptrace(PTRACE_GETREGS, pid, 0, ctypes.addressof(original))

            stack = (original.rsp - 0x800) & ~0xF
            if arg_str:
                data = arg_str.encode("utf-8") + b"\x00"
                _write_bytes(pid, stack, data)
                first = stack
                second = extra_int
                # 字符串放栈顶，返回地址槽放在字符串上方，避免被覆盖
                ret_slot = (stack + len(data) + 15) & ~0xF
            else:
                first = arg_int
                second = extra_int
                ret_slot = stack
            _write_word(pid, ret_slot, 0)

            regs = UserRegs()
            ctypes.memmove(ctypes.addressof(regs), ctypes.addressof(original), ctypes.sizeof(original))
            regs.rdi = first
            regs.rsi = second
            regs.rip = remote_func
            regs.rsp = ret_slot
            regs.orig_rax = ctypes.c_ulonglong(-1).value
            _ptrace(PTRACE_SETREGS, pid, 0, ctypes.addressof(regs))

            _ptrace(PTRACE_CONT, pid, 0, 0)
            _wait_stopped(pid, timeout)

            after = UserRegs()
            _ptrace(PTRACE_GETREGS, pid, 0, ctypes.addressof(after))
            result = int(after.rax)
        finally:
            try:
                _ptrace(PTRACE_SETREGS, pid, 0, ctypes.addressof(original), allow_fail=True)
            finally:
                _ptrace(PTRACE_DETACH, pid, 0, 0, allow_fail=True)
        return result

    def module_loaded(self, pid: int, module_name: str) -> int:
        """目标进程里是否已经加载了某个模块，返回基址（0 = 没加载）。"""
        if not IS_WINDOWS:
            return _module_base_posix(pid, module_name)
        k32 = _kernel32()
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snap == INVALID_HANDLE_VALUE or not snap:
            return 0
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
            ok = k32.Module32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
            while ok:
                if _decode_wide(entry.szModule).lower() == module_name.lower():
                    return int(entry.modBaseAddr or 0)
                ok = k32.Module32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
            return 0
        finally:
            k32.CloseHandle(ctypes.c_void_p(snap))


# ---------------------------------------------------------------- Linux 注入
# 思路：ptrace 停下目标 → 把参数写到它的栈上 → 把 rip 指到 libc 的 dlopen →
# 单步跑到返回 → 读 rax → 恢复寄存器 → detach。整个过程目标进程只「借」了
# 一小段栈空间，代码段和内存映像不变，模块用 dlopen 正式加载（可 dlclose 卸载）。
#
# 与 Windows 的 CreateRemoteThread 相比，这里没有「远程线程」概念：借的是目标
# 已有的某个线程，调用完立刻还原，所以不会有幽灵线程残留。
PTRACE_CONT = 7
PTRACE_GETREGS = 12
PTRACE_SETREGS = 13
PTRACE_ATTACH = 16
PTRACE_DETACH = 17
PTRACE_PEEKDATA = 2
PTRACE_POKEDATA = 5

RTLD_NOW = 2

_REGS_FIELDS = (
    "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8",
    "rax", "rcx", "rdx", "rsi", "rdi", "orig_rax", "rip", "cs", "eflags",
    "rsp", "ss", "fs_base", "gs_base", "ds", "es", "fs", "gs",
)


class UserRegs(ctypes.Structure):
    """x86_64 `struct user_regs_struct`。字段顺序与内核头文件一致，少一个就全错。"""

    _fields_ = [(name, ctypes.c_ulonglong) for name in _REGS_FIELDS]


def _libc_handle() -> Any:
    import ctypes.util

    name = ctypes.util.find_library("c") or "libc.so.6"
    lib = ctypes.CDLL(name, use_errno=True)
    lib.ptrace.restype = ctypes.c_long
    lib.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    return lib


def _ptrace(request: int, pid: int, addr: int = 0, data: int = 0, *, allow_fail: bool = False) -> int:
    ctypes.set_errno(0)
    result = _libc_handle().ptrace(request, pid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    if result == -1 and not allow_fail:
        raise HookError(f"ptrace({request}, pid={pid}) 失败 errno={ctypes.get_errno()}")
    return int(result)


def _maps(pid: int | str) -> list[tuple[int, int, str, str]]:
    """解析 /proc/<pid>/maps → [(start, end, perms, path)]。"""
    if pid == "self":
        path = "/proc/self/maps"
    else:
        path = f"/proc/{pid}/maps"
    out: list[tuple[int, int, str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split(None, 5)
                if len(parts) < 5:
                    continue
                try:
                    start_s, end_s = parts[0].split("-")
                    start, end = int(start_s, 16), int(end_s, 16)
                except ValueError:
                    continue
                out.append((start, end, parts[1], parts[5].strip() if len(parts) > 5 else ""))
    except OSError as exc:
        raise HookError(f"读 /proc/{pid}/maps 失败：{exc}") from exc
    return out


def mapping_base(pid: int | str, needle: str) -> int:
    """某个共享库在目标进程里的加载基址（取最小 start）。"""
    candidates = [start for start, _end, _perms, path in _maps(pid) if needle in path]
    return min(candidates) if candidates else 0


def _module_base_posix(pid: int, module_name: str) -> int:
    for start, _end, _perms, path in _maps(pid):
        if os.path.basename(path) == module_name:
            return start
    return 0


def _symbol_offset(needle: str, symbol: str) -> int:
    """本地 libc 里符号相对基址的偏移（同机同版本，可直接加到远端基址）。"""
    base = mapping_base("self", needle)
    if not base:
        raise HookError(f"本地找不到 {needle} 的映射")
    try:
        lib = ctypes.CDLL(needle if not needle.startswith("libc") else "libc.so.6")
        func = getattr(lib, symbol)
    except (OSError, AttributeError) as exc:
        raise HookError(f"本地 libc 没有 {symbol}：{exc}") from exc
    address = ctypes.cast(func, ctypes.c_void_p).value
    if not address:
        raise HookError(f"拿不到 {symbol} 地址")
    return int(address) - base


def _read_word(pid: int, addr: int) -> int:
    ctypes.set_errno(0)
    value = _libc_handle().ptrace(PTRACE_PEEKDATA, pid, ctypes.c_void_p(addr), None)
    err = ctypes.get_errno()
    if value == -1 and err:
        raise HookError(f"PEEKDATA 0x{addr:x} 失败 errno={err}")
    return int(ctypes.c_ulonglong(value).value)


def _write_word(pid: int, addr: int, word: int) -> None:
    _ptrace(PTRACE_POKEDATA, pid, addr, word)


def _write_bytes(pid: int, addr: int, data: bytes) -> None:
    """按 8 字节对齐写；尾部不足 8 字节要读-改-写，否则会把相邻数据写坏。"""
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + 8]
        target = addr + offset
        if len(chunk) == 8:
            word = int.from_bytes(chunk, "little")
        else:
            word = _read_word(pid, target)
            mask = (1 << (8 * len(chunk))) - 1
            word = (word & ~mask) | int.from_bytes(chunk, "little")
        _write_word(pid, target, word)
        offset += 8


def _wait_stopped(pid: int, timeout: float = 10.0) -> int:
    """等目标停下。用 SIGALRM 兜底，避免 ptrace 序列卡死整个进程。"""
    import signal

    def _alarm(_signum, _frame):
        raise HookError(f"等待 pid={pid} 停下超时（{timeout:.0f}s）")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        while True:
            wpid, status = os.waitpid(pid, os.WUNTRACED)
            if wpid == pid and os.WIFSTOPPED(status):
                return status
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)



# ---------------------------------------------------------------- 门面
def find_processes(
    exe_names: Iterable[str] = DEFAULT_EXE_NAMES, scanner: ProcessScanner | None = None
) -> list[QQProcess]:
    return (scanner or ProcessScanner()).scan(exe_names)


def pick_main(processes: Sequence[QQProcess]) -> QQProcess | None:
    """挑一个最像「该注入」的进程：优先已带管道的，其次第一个主进程。"""
    for item in processes:
        if item.is_main and item.pipe:
            return item
    for item in processes:
        if item.is_main:
            return item
    return processes[0] if processes else None


def default_hook_dll(arch: str = "x64") -> str:
    """找一个现成的 hook DLL 当载体。

    顺序：环境变量 → 仓库内构建产物 → 本机已装的 SnowLuma 运行库。
    最后一项只是「复用已经验证过 hook 逻辑的二进制」，协议层仍在 ArkLink 自己手里；
    想完全自持就把 `ARKLINK_HOOK_DLL` 指向 `hookdll/` 编出来的 DLL。
    """
    env = os.environ.get("ARKLINK_HOOK_DLL", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    build = os.path.normpath(os.path.join(here, "..", "..", "hookdll", "build"))
    names = [f"arklink-hook-{arch}.dll"] if IS_WINDOWS else [f"libarklink-hook-{arch}.so", "libarklink-hook.so"]
    for name in names:
        local = os.path.join(build, name)
        if os.path.isfile(local):
            return local
    if not IS_WINDOWS:
        return ""
    for candidate in (
        rf"D:\snow\native\snowluma-win32-{arch}.dll",
        os.path.expandvars(rf"%LOCALAPPDATA%\Programs\SnowLuma\native\snowluma-win32-{arch}.dll"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""
