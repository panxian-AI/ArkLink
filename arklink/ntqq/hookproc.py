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

#: QQ 主进程的候选可执行名（QQNT 是 QQ.exe；旧版是 QQScLauncher.exe）
DEFAULT_EXE_NAMES: tuple[str, ...] = ("QQ.exe",)

#: 管道前缀。自有 DLL 用 arklink，现成 hook 用 mojo
PIPE_PREFIXES: tuple[str, ...] = ("mojo", "arklink")

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
            return []
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
    def command_line(self, pid: int) -> str:
        """读目标进程 PEB 里的命令行。读不到就返回空串，不抛。"""
        if not IS_WINDOWS:
            return ""
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
    def pipe_names(self) -> set[str]:
        if not IS_WINDOWS:
            return set()
        try:
            return {n for n in os.listdir("\\\\.\\pipe\\")}
        except OSError:
            return set()

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
            return "missing"
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
        if not IS_WINDOWS:
            raise HookError("注入只在 Windows 上可用")
        if not os.path.isfile(dll_path):
            raise HookError(f"DLL 不存在：{dll_path}")
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
            return False
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

    def module_loaded(self, pid: int, module_name: str) -> int:
        """目标进程里是否已经加载了某个模块，返回基址（0 = 没加载）。"""
        if not IS_WINDOWS:
            return 0
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
    local = os.path.join(here, "..", "..", "hookdll", "build", f"arklink-hook-{arch}.dll")
    local = os.path.normpath(local)
    if os.path.isfile(local):
        return local
    for candidate in (
        rf"D:\snow\native\snowluma-win32-{arch}.dll",
        os.path.expandvars(rf"%LOCALAPPDATA%\Programs\SnowLuma\native\snowluma-win32-{arch}.dll"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""
