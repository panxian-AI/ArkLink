"""操作「已登录的官方 QQ 客户端」——最后一条腿。

为什么需要它：自己实现登录（headless / 协议端）时，QQ 看到的是一个没人用的
"影子会话"——没有真实客户端心跳、设备指纹对不上、ticket 生命周期异常，
于是很容易被踢下线。而**用户手动登录的官方客户端**是"真的在跑"，
风控无从下手；只要它在线，就总能从外部把消息塞进去。

本模块用 Win32 做三件事（不需要 hook、不需要注入）：

    detect()          找 QQ.exe 进程 + 主窗口，判断是否在线/前台
    open_and_send()   唤起窗口 → 搜索会话 → 粘贴文本 → 回车
    send_text()       往"当前打开的会话"直接粘贴并发送

能力边界（必须说清楚）：

* **只发不收**：收消息仍需其他通道（Milky / OneBot / 官方 API）。
  它是一条"通知腿"，不是完整 bot。
* **只发文本**：图片/文件一律返回 `1404`，让框架换通道，不要静默丢消息。
* **依赖窗口焦点**：会抢焦点，`dry_run=True` 时只记录不敲键盘。

适用场景：所有协议端都被封、但用户的 QQ 还开着 —— 至少还能把告警发出去。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from ..base import LinkAdapter, api_error
from . import elements as el

logger = logging.getLogger("arklink.ntqq.host")

IS_WINDOWS = sys.platform == "win32"

#: QQ NT 是 Electron，主窗口类名；老版 QQ 是 TXGuiFoundation
WINDOW_CLASSES = ("Chrome_WidgetWin_1", "TXGuiFoundation")

class PROCESSENTRY32W(ctypes.Structure):
    """Windows 结构体字段一律用定宽类型：`c_long` 在 Linux 上是 8 字节，
    会让尺寸断言在 CI（Linux）上莫名失败。"""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        # 用 uint16 数组而不是 c_wchar：c_wchar 在 Linux 上是 4 字节
        ("szExeFile", ctypes.c_uint16 * 260),
    ]


def _exe_name(entry: "PROCESSENTRY32W") -> str:
    """从 PROCESSENTRY32W 取 exe 名（UTF-16LE，遇 0 截断）。"""
    raw = bytes(entry.szExeFile)
    return raw.decode("utf-16-le", "ignore").split("\x00", 1)[0]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    """SendInput 要求 cbSize 精确：x64 上必须是 40 字节。"""

    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


CF_UNICODETEXT = 13
#: 剪贴板里的「文件列表」。QQ 输入框收到这个会当成待发送的附件（图片直接预览成图）。
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
#: DROPFILES 结构在 x64 上的字节数：DWORD(4) + POINT(8) + BOOL(4) + BOOL(4)
DROPFILES_SIZE = 20
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_LWIN = 0x5B

#: 热键写法：^Ctrl !Alt +Shift #Win，后跟一个键名，如 "^f"、"^!q"
_MODIFIERS = {"^": VK_CONTROL, "!": VK_MENU, "+": VK_SHIFT, "#": VK_LWIN}

#: 具名键（比单字符直观，也避免 "enter" 被当成 'E'）
_NAMED_KEYS = {
    "enter": VK_RETURN,
    "return": VK_RETURN,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}


def parse_hotkey(spec: str) -> tuple[list[int], int]:
    """`"^f"` → ([VK_CONTROL], 0x46)。支持 `^`/`!`/`+`/`#` 与单字符键名。"""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("热键为空")
    mods: list[int] = []
    while spec and spec[0] in _MODIFIERS:
        mods.append(_MODIFIERS[spec[0]])
        spec = spec[1:]
    named = _NAMED_KEYS.get(spec.lower())
    if named is not None:
        return mods, named
    if len(spec) != 1:
        raise ValueError(f"只支持单字符或具名键（enter/tab/esc…），收到 {spec!r}")
    key = spec.upper()
    vk = ord(key)
    if not (0x30 <= vk <= 0x5A or 0x70 <= vk <= 0x87):  # 0-9 / A-Z / F1-F24
        raise ValueError(f"不支持的按键 {key!r}")
    return mods, vk


def build_dropfiles(paths: Iterable[str]) -> bytes:
    """构造 `CF_HDROP` 的剪贴板数据：DROPFILES 头 + UTF-16LE 路径列表。

    布局（x64）::

        [0:4]    pFiles  路径列表相对结构体起点的偏移，固定 20
        [4:12]   pt      拖放坐标；粘贴场景用不到，填 0
        [12:16]  fNC     是否非客户区，0
        [16:20]  fWide   1 表示宽字符（UTF-16LE）
        [20:]    path1\\0 path2\\0 \\0

    路径之间用单个 `\\0` 分隔，**整个列表再补一个 `\\0` 收尾**（双 null）。
    少了最后那个 null，接收方会读越界或干脆忽略这次粘贴——这是 CF_HDROP
    最常见的坑，且现象是「静默不生效」，很难查。
    """
    items = [str(p) for p in paths if str(p)]
    if not items:
        return b""
    head = struct.pack("<IiiII", DROPFILES_SIZE, 0, 0, 0, 1)
    body = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in items) + b"\x00\x00"
    return head + body


def parse_dropfiles(data: bytes) -> list[str]:
    """把 `build_dropfiles` 的产物解回路径列表（自检与排障用）。"""
    if len(data) < DROPFILES_SIZE:
        return []
    offset = struct.unpack("<I", data[:4])[0]
    if not DROPFILES_SIZE <= offset <= len(data):
        return []
    text = data[offset:]
    # 每个路径以 \0 结尾，最后一个 \0 是列表终止符，解码后会多出一个空串
    out, start = [], 0
    while start + 1 < len(text):
        if text[start : start + 2] == b"\x00\x00":
            break
        end = start
        while end + 1 < len(text) and text[end : end + 2] != b"\x00\x00":
            end += 2
        out.append(text[start:end].decode("utf-16-le", "replace"))
        start = end + 2
    return out


@dataclass
class ClientInfo:
    """客户端状态快照。"""

    running: bool = False
    pid: int = 0
    hwnd: int = 0
    title: str = ""
    foreground: bool = False
    #: 这个窗口属于哪个 QQ 号（能认出来才有值）
    uin: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "hwnd": self.hwnd,
            "title": self.title,
            "foreground": self.foreground,
            "uin": self.uin,
        }


# ---------------------------------------------------------------- Win32 封装


class _Win32:
    """把要用的 user32/kernel32 调用集中起来，非 Windows 上全部返回空。"""

    def __init__(self) -> None:
        self.ok = IS_WINDOWS
        if not self.ok:
            return
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        # 声明 DPI aware：高缩放屏幕（笔记本常见 125%/150%）上， unaware 进程的
        # GetWindowRect 与 SetCursorPos 各活在一套坐标里，点击会静默落到别的
        # 位置——实测会点中会话列表第一条而不是搜索框。声明之后所有坐标都是
        # 物理像素，所见即所得。必须赶在任何窗口/图形调用之前。
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except (AttributeError, OSError):
            try:  # 老 Windows 没有 shcore
                self.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        # 64 位必须显式声明返回指针的 API：ctypes 默认按 32 位 int 截断，
        # GlobalLock 拿到的高位会被砍掉，memmove 直接写 0 地址。
        u, k = self.user32, self.kernel32
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow.restype = ctypes.c_bool
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.FindWindowW.restype = ctypes.c_void_p
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u.IsWindowVisible.restype = ctypes.c_bool
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        u.OpenClipboard.argtypes = [ctypes.c_void_p]
        u.OpenClipboard.restype = ctypes.c_bool
        u.EmptyClipboard.restype = ctypes.c_bool
        u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u.SetClipboardData.restype = ctypes.c_void_p
        u.GetClipboardData.argtypes = [ctypes.c_uint]
        u.GetClipboardData.restype = ctypes.c_void_p
        u.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
        u.SendInput.restype = ctypes.c_uint
        u.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
        u.MapVirtualKeyW.restype = ctypes.c_uint
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.GetWindowRect.restype = ctypes.c_bool
        k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k.GlobalAlloc.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.restype = ctypes.c_bool

    # ---- 进程 ----
    def pids_of(self, names: Iterable[str]) -> list[int]:
        if not self.ok:
            return []
        wanted = {n.lower() for n in names}
        TH32CS_SNAPPROCESS = 0x00000002
        snap = self.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (-1, 0xFFFFFFFFFFFFFFFF):
            return []
        found: list[int] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if self.kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                while True:
                    if _exe_name(entry).lower() in wanted:
                        found.append(int(entry.th32ProcessID))
                    if not self.kernel32.Process32NextW(snap, ctypes.byref(entry)):
                        break
        finally:
            self.kernel32.CloseHandle(snap)
        return found

    # ---- 窗口 ----
    def windows_of_pid(self, pid: int) -> list[tuple[int, str, str]]:
        """[(hwnd, class, title)]，只取可见窗口。"""
        if not self.ok:
            return []
        result: list[tuple[int, str, str]] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _lparam: int) -> bool:
            owner = ctypes.c_uint32()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not self.user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(512)
            self.user32.GetClassNameW(hwnd, cls, 256)
            self.user32.GetWindowTextW(hwnd, title, 512)
            result.append((int(hwnd or 0), cls.value, title.value))
            return True

        self.user32.EnumWindows(enum_proc(callback), 0)
        return result

    def foreground_hwnd(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0) if self.ok else 0

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """窗口屏幕矩形 (left, top, right, bottom)；拿不到返回全 0。"""
        if not self.ok or not hwnd:
            return (0, 0, 0, 0)
        import ctypes.wintypes as wt

        r = wt.RECT()
        if not self.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return (0, 0, 0, 0)
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))

    def click(self, x: int, y: int) -> None:
        """在屏幕坐标点一下左键。

        为什么用鼠标：QQ NT 的会话搜索框没有可用的快捷键（`Ctrl+F` 打开的是
        「综合搜索 / 加好友」，不是会话列表），只能点到那个输入框上。
        """
        if not self.ok:
            return
        u = self.user32
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        inp = INPUT(type=0)  # 0 = INPUT_MOUSE
        inp.u.mi = MOUSEINPUT(int(x), int(y), 0, 0x0002, 0, None)  # LEFTDOWN
        u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.03)
        inp2 = INPUT(type=0)
        inp2.u.mi = MOUSEINPUT(int(x), int(y), 0, 0x0004, 0, None)  # LEFTUP
        u.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(INPUT))
        time.sleep(0.08)

    def focus(self, hwnd: int) -> bool:
        """把窗口切到前台，并**确认**它真的在前台。

        `SetForegroundWindow` 在调用方不是前台进程时经常被系统拒绝，
        偷懒不确认的结果就是：粘贴和回车敲进了用户正在用的编辑器里。
        先按一下 ALT 再调（绕过前台锁的经典手法），失败重试三次。
        """
        if not self.ok or not hwnd:
            return False
        self.user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
        for _ in range(3):
            self.chord([VK_MENU])  # 解除前台锁
            ok = bool(self.user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
            time.sleep(0.2)
            if ok or self.foreground_hwnd() == hwnd:
                return self.foreground_hwnd() == hwnd
            time.sleep(0.2)
        return False

    # ---- 剪贴板 ----
    def _set_raw(self, fmt: int, data: bytes) -> bool:
        """把一段原始字节放进剪贴板（调用方负责拼好 fmt 对应的布局）。"""
        if not self.ok or not data:
            return False
        u = self.user32
        k = self.kernel32
        if not u.OpenClipboard(None):
            return False
        try:
            u.EmptyClipboard()
            handle = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = k.GlobalLock(handle)
            if not ptr:
                return False
            ctypes.memmove(ptr, data, len(data))
            k.GlobalUnlock(handle)
            # SetClipboardData 成功后这块内存归系统所有，不能再 GlobalFree
            return bool(u.SetClipboardData(fmt, handle))
        finally:
            u.CloseClipboard()

    def set_clipboard(self, text: str) -> bool:
        return self._set_raw(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")

    def set_clipboard_files(self, paths: Iterable[str]) -> bool:
        """把本地文件放进剪贴板：QQ 输入框 Ctrl+V 后会变成待发送的附件。"""
        return self._set_raw(CF_HDROP, build_dropfiles(paths))

    def get_clipboard(self) -> str:
        if not self.ok:
            return ""
        u = self.user32
        k = self.kernel32
        if not u.OpenClipboard(None):
            return ""
        try:
            handle = u.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                k.GlobalUnlock(handle)
        finally:
            u.CloseClipboard()

    # ---- 键盘 ----
    def key(self, vk: int, up: bool = False) -> None:
        if not self.ok:
            return
        scan = self.user32.MapVirtualKeyW(vk, 0)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP if up else 0, 0, None)
        sent = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if not sent:
            logger.debug("SendInput 失败 vk=0x%02x err=%s", vk, ctypes.GetLastError())

    def chord(self, keys: list[int]) -> None:
        for vk in keys:
            self.key(vk)
            time.sleep(0.02)
        for vk in reversed(keys):
            self.key(vk, up=True)
            time.sleep(0.02)


_W32 = _Win32()


def _area_of(hwnd: int) -> int:
    """窗口面积（像素²）。用来在多个候选窗口里挑真正的主窗口。

    不能按枚举顺序取第一个：同一进程会同时挂出主窗口和几个 158x26 的小窗口
    （最小化残留 / 托盘浮窗），它们常在屏幕外，点上去等于什么都没做。
    """
    left, top, right, bottom = _W32.rect(hwnd)
    return max(0, right - left) * max(0, bottom - top)


# ---------------------------------------------------------------- 客户端门面


class ClientHost:
    """对「已登录 QQ 客户端」的操作封装。"""

    def __init__(
        self,
        process_names: Iterable[str] = ("QQ.exe", "QQNT.exe"),
        search_hotkey: str = "^f",
        select_all_hotkey: str = "^a",
        paste_hotkey: str = "^v",
        send_key: str = "enter",
        open_delay: float = 0.6,
        #: 粘完附件后等多久再回车。QQ 把文件挂到输入框要一点时间，
        #: 按早了会只发文字、附件被静默丢掉（客户端不报错），所以宁可多等。
        attach_delay: float = 1.5,
        dry_run: bool = True,
        uin: str = "",
        candidates: Iterable[str] = (),
        #: 聊天输入框在窗口内的相对位置（9.9.31 默认布局实测）
        input_xy: tuple[float, float] = (0.65, 0.87),
    ) -> None:
        self.process_names = tuple(process_names)
        self.search_hotkey = search_hotkey
        self.select_all_hotkey = select_all_hotkey
        self.paste_hotkey = paste_hotkey
        self.send_key = send_key
        self.open_delay = open_delay
        self.attach_delay = float(attach_delay)
        self.dry_run = dry_run
        #: 允许操作哪个号。空 = 不校验（不建议，同机多号时会发错）
        self.uin = str(uin or "")
        #: 认号时的候选集合：默认 允许号 + 其它见过的号
        self.candidates = tuple(str(c) for c in candidates if str(c))
        self.input_xy = (float(input_xy[0]), float(input_xy[1]))
        self._uin_cache: dict[int, str] = {}
        self.last_action = ""

    # ---- 账号识别 ----
    def uin_of_pid(self, pid: int) -> str:
        """判断某个 QQ 进程是哪个号（内存指纹，见 `qqabi.count_string`）。"""
        if pid in self._uin_cache:
            return self._uin_cache[pid]
        if not IS_WINDOWS:
            return ""
        cands = list(self.candidates) or ([self.uin] if self.uin else [])
        try:
            from . import qqabi

            # 候选必须包含本机所有登录过的号，否则「单候选」永远赢，
            # 会把另一个号误判成目标号。allow-list 只用来兜底。
            for uin in qqabi.installed_uins():
                if uin not in cands:
                    cands.append(uin)
            if not cands:
                return ""
            found, _counts = qqabi.whoami(pid, cands)
        except Exception:
            found = ""
        if found:
            self._uin_cache[pid] = found
        return found

    # ---- 检测 ----
    def detect(self) -> ClientInfo:
        if not IS_WINDOWS:
            return ClientInfo()
        pids = _W32.pids_of(self.process_names)
        if not pids:
            return ClientInfo()
        windows: list[tuple[int, int, str, int]] = []
        for pid in pids:
            for hwnd, cls, title in _W32.windows_of_pid(pid):
                if cls in WINDOW_CLASSES or "QQ" in title:
                    windows.append((pid, hwnd, title, _area_of(hwnd)))
        if not windows:
            return ClientInfo(running=True, pid=pids[0])

        def build(pid: int, hwnd: int, title: str) -> ClientInfo:
            return ClientInfo(
                running=True,
                pid=pid,
                hwnd=hwnd,
                title=title,
                foreground=_W32.foreground_hwnd() == hwnd,
                uin=self.uin_of_pid(pid),
            )

        # 同一个号可能同时有主窗口和几个「可见但没用」的小窗口（最小化残留、
        # 托盘浮窗）。它们坐标常在屏幕外（实测见过 -21333,-21333 的 158x26），
        # 而枚举顺序不保证主窗口在前——按坐标点下去会落到屏幕外，什么都不会发生，
        # 也不报错。所以一律按面积倒序，让主窗口永远排第一。
        windows.sort(key=lambda item: item[3], reverse=True)

        # 多开时窗口标题都叫 "QQ"，必须按账号挑，否则会拿另一个号发出去
        if self.uin and len(windows) > 1:
            for pid, hwnd, title, _area in windows:
                if self.uin_of_pid(pid) == self.uin:
                    return build(pid, hwnd, title)
            self.last_action = f"没有找到账号 {self.uin} 的窗口（多开？）"
        pid, hwnd, title, _area = windows[0]
        return build(pid, hwnd, title)

    # ---- 操作 ----
    def focus(self, info: ClientInfo | None = None) -> bool:
        info = info or self.detect()
        if not info.hwnd:
            self.last_action = "focus: 没有主窗口"
            return False
        ok = _W32.focus(info.hwnd)
        self.last_action = f"focus: {'ok' if ok else 'fail'} hwnd={info.hwnd}"
        return ok

    def send_text(self, text: str, *, hwnd: int = 0) -> bool:
        """往当前打开的会话发文本：WM_CHAR 输入 + UIA 点「发送」按钮。

        真机结论（2026-11-14）：SendInput 点击/回车全被客户端过滤，
        唯一可靠路径是 UIA 定位 + PostMessage（详见 uia.py 模块注释）。
        """
        if not text:
            return False
        if self.dry_run:
            self.last_action = f"[dry-run] send_text {len(text)} 字: {text[:40]!r}"
            logger.info(self.last_action)
            return True
        hwnd = hwnd or self.detect().hwnd
        if not hwnd:
            self.last_action = "send_text: 没有主窗口"
            return False
        try:
            ok = self._channel(hwnd).send_current_text(text)
        except Exception as exc:
            self.last_action = f"send_text: {exc}"
            return False
        self.last_action = f"send_text: {len(text)} 字 {'ok' if ok else 'fail'}"
        return ok

    def open_and_send(self, peer: str, text: str) -> bool:
        """点会话搜索框 → 点联系人 → 点输入框 → 粘贴 → 回车。

        全程用鼠标点击而不是回车选择：QQ 的「综合搜索」（Ctrl+F）和
        会话搜索行为不一样，回车在综合搜索里会把人带进加好友页。
        每一步都验证，失败立刻停——发错对象比发不出去严重得多。
        """
        if self.dry_run:
            self.last_action = f"[dry-run] open_and_send peer={peer} text={text[:40]!r}"
            logger.info(self.last_action)
            return True
        hwnd = self._target_hwnd("open_and_send")
        if not hwnd:
            return False
        # UIA：打开会话 → WM_CHAR 输入 → 点「发送」
        try:
            ok = self._channel(hwnd).send_text(str(peer), text)
        except Exception as exc:
            self.last_action = f"open_and_send: {exc}"
            return False
        self.last_action = f"open_and_send: peer={peer} {'ok' if ok else 'fail'}"
        return ok

    def open_and_send_files(
        self,
        peer: str,
        paths: Iterable[str],
        *,
        text: str = "",
    ) -> bool:
        """定位到会话，再发本地附件（图片/文件）。"""
        files = [str(p) for p in paths if str(p)]
        if self.dry_run:
            self.last_action = (
                f"[dry-run] open_and_send_files peer={peer} "
                f"files={files[:3]}{'…' if len(files) > 3 else ''} text={text[:20]!r}"
            )
            logger.info(self.last_action)
            return True
        if not files:
            self.last_action = "open_and_send_files: 没有附件"
            return False
        hwnd = self._target_hwnd("open_and_send_files")
        if not hwnd:
            return False
        try:
            ok = self._channel(hwnd).send_files(str(peer), files, text=text)
        except Exception as exc:
            self.last_action = f"open_and_send_files: {exc}"
            return False
        self.last_action = (
            f"open_and_send_files: peer={peer} files={len(files)} "
            f"{'ok' if ok else 'fail'}"
        )
        return ok

    def send_files(self, paths: Iterable[str], *, text: str = "", hwnd: int = 0) -> bool:
        """往当前打开的会话发本地附件（文件对话框路径）。

        剪贴板族方案（CF_HDROP + 粘贴）在真机上被客户端全部过滤：
        WM_PASTE / SendInput Ctrl+V / WM_CHAR 0x16 / WM_DROPFILES 均无效。
        可靠路径是 UIA 点「文件」按钮 → 原生对话框 WM_SETTEXT + Enter →
        确认框「发送(N)」PostMessage 点击（详见 uia.py）。
        """
        files = [str(p) for p in paths if str(p)]
        if not files:
            self.last_action = "send_files: 没有附件"
            return False
        if self.dry_run:
            self.last_action = f"[dry-run] send_files {len(files)} 个: {files[:3]}"
            logger.info(self.last_action)
            return True
        hwnd = hwnd or self.detect().hwnd
        if not hwnd:
            self.last_action = "send_files: 没有主窗口"
            return False
        try:
            ok = self._channel(hwnd).send_current_files(files, text=text)
        except Exception as exc:
            self.last_action = f"send_files: {exc}"
            return False
        self.last_action = (
            f"send_files: {len(files)} 个附件" + (f" + {len(text)} 字" if text else "")
            + (" ok" if ok else " fail")
        )
        return ok

    # ---- 内部 ----
    def _channel(self, hwnd: int) -> Any:
        """UIA 通道。窗口句柄来自 detect()（按账号认窗口），不会碰别的账号。"""
        from .uia import UiChannel

        return UiChannel(hwnd)

    def _target_hwnd(self, tag: str) -> int:
        info = self.detect()
        if not info.running or not info.hwnd:
            self.last_action = f"{tag}: 客户端不在线或没有窗口"
            return 0
        return info.hwnd

    def read_messages(self) -> list[Any]:
        """读**当前打开**聊天窗口里可见的消息（UIA 轮询接收用）。"""
        hwnd = self._target_hwnd("read_messages")
        if not hwnd:
            return []
        try:
            with self._channel(hwnd) as ch:
                return ch.read_messages()
        except Exception as exc:
            logger.warning("read_messages 失败: %s", exc)
            return []

    def read_peer_messages(self, peer: str) -> list[Any]:
        """打开 peer 的聊天并读取可见消息（轮询接收用；会切换当前会话）。"""
        hwnd = self._target_hwnd("read_peer_messages")
        if not hwnd:
            return []
        try:
            with self._channel(hwnd) as ch:
                if not ch.win.open_chat(str(peer)):
                    return []
                return ch.win.read_messages()
        except Exception as exc:
            logger.warning("read_peer_messages(%s) 失败: %s", peer, exc)
            return []


# ---------------------------------------------------------------- 适配器


def plain_text_of(message: Any) -> str:
    """只取文本段；有非文本段时返回空串（调用方据此拒绝）。"""
    if isinstance(message, str):
        return message
    chain = message
    if hasattr(message, "segments"):
        chain = [{"type": s.type, "data": dict(s.data)} for s in message.segments]
    if not isinstance(chain, list):
        return ""
    parts: list[str] = []
    for seg in chain:
        if not isinstance(seg, dict):
            return ""
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            parts.append(str(data.get("text") or ""))
        elif stype == "at":
            parts.append(f"@{data.get('qq')}")
        elif stype == "reply":
            continue
        else:
            return ""      # 图片/语音/文件：本通道不支持
    return "".join(parts).strip()


class ClientHostAdapter(LinkAdapter):
    """把「操作官方客户端」挂成一个通道（发送 + 可选的 UIA 轮询接收）。

    bot.toml：
        [[adapters]]
        type = "ntqq-client"
        dry_run = false              # 校准好链路后再关掉
        process_names = ["QQ.exe"]
        receive_peers = ["阿潘"]     # 要轮询的会话名（备注/昵称）；空 = 不收

    接收说明：UIA 轮询读的是**聊天窗口里可见的气泡**（详见 uia.py），
    依赖界面语言；只会切换到 receive_peers 列出的会话，轮询期间 QQ 窗口
    会跟着切来切去。收不到历史消息，也不适合高频场景——要真·实时推送，
    后续从 ntqq 协议栈（sso/push，纯标准库）接事件，本通道作为兜底。
    """

    name = "ntqq-client"
    #: 类级默认：不收。配置了 receive_peers 时实例上会覆盖为 True
    supports_receive = False

    def __init__(
        self,
        host: ClientHost | None = None,
        dry_run: bool = True,
        search_hotkey: str = "^f",
        paste_hotkey: str = "^v",
        process_names: Iterable[str] | None = None,
        poll_interval: float = 5.0,
        receive_peers: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.host = host or ClientHost(
            process_names=process_names or ("QQ.exe", "QQNT.exe"),
            search_hotkey=search_hotkey,
            paste_hotkey=paste_hotkey,
            dry_run=dry_run,
        )
        self.poll_interval = poll_interval
        self.info: ClientInfo = ClientInfo()
        #: 轮询接收的会话列表；指纹去重窗口 + 合成 message_id
        self.receive_peers = tuple(str(p) for p in (receive_peers or ()) if str(p))
        if self.receive_peers:
            self.supports_receive = True
        self._seen: dict[str, deque[str]] = {}
        self._msg_seq = 0

    # 覆盖：本通道"健康"= 客户端在线
    @property
    def is_connected(self) -> bool:
        return self.info.running

    async def connect(self) -> None:
        while self._running:
            try:
                self.info = await asyncio.to_thread(self.host.detect)
                if self.info.running:
                    self._connected.set()
                else:
                    self._connected.clear()
            except Exception:
                logger.exception("检测 QQ 客户端失败")
            if self.info.running and self.receive_peers:
                try:
                    await self._poll_receive()
                except Exception:
                    logger.exception("UIA 轮询接收失败")
            await asyncio.sleep(self.poll_interval)

    async def _poll_receive(self) -> None:
        """每个会话轮流：打开聊天 → 读可见气泡 → 指纹去重 → emit。"""
        for peer in self.receive_peers:
            msgs = await asyncio.to_thread(self.host.read_peer_messages, peer)
            seen = self._seen.setdefault(peer, deque(maxlen=300))
            for m in msgs:
                if m.outgoing:
                    continue          # 自己发的（含本通道发出的）不当事件
                fp = m.fingerprint
                if not fp.strip("|") or fp in seen:
                    continue
                seen.append(fp)
                self._msg_seq += 1
                self._msg_seq &= 0x7FFFFFFF
                await self._emit_dict({
                    "time": int(time.time()),
                    "self_id": self.host.uin or 0,
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "message_id": self._msg_seq,
                    "user_id": 0,             # UIA 拿不到对方 QQ 号
                    "message": [{"type": "text", "data": {"text": m.text}}],
                    "raw_message": m.text,
                    "sender": {"user_id": 0, "nickname": m.sender or peer},
                    "via": "ui-poll",
                    "peer_name": peer,
                    "msg_time": m.time,
                })

    async def disconnect(self) -> None:
        self._connected.clear()
        self.info = ClientInfo()

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        if action in ("send_msg", "send_message", "send_private_msg", "send_group_msg"):
            try:
                parsed = el.parse_message(params.get("message"))
            except el.MessageError as exc:
                raise api_error(action, 1400, str(exc)) from None
            files, remote = el.split_attachments(parsed)
            if remote:
                kinds = "、".join(sorted({e.kind for e in remote}))
                raise api_error(
                    action, 1404,
                    f"{kinds} 段只有地址没有本地文件：剪贴板只能粘本机文件，请先下载到磁盘",
                )
            # 有附件时文字部分要剔掉 [图片] 这类占位符
            text = (
                "".join(e.text for e in parsed if e.kind == "text").strip()
                if files
                else el.plain_text(parsed)
            )
            if not text and not files:
                raise api_error(action, 1404, "消息没有可发的内容")
            peer = params.get("group_id") or params.get("user_id")
            if not peer:
                raise api_error(action, 1404, "缺少 group_id/user_id")
            if files:
                ok = await asyncio.to_thread(
                    self.host.open_and_send_files, str(peer), files, text=text
                )
            else:
                ok = await asyncio.to_thread(self.host.open_and_send, str(peer), text)
            if not ok:
                raise api_error(action, -1, f"客户端操作失败: {self.host.last_action}")
            return {"message_id": 0, "via": "client-host"}
        if action == "get_status":
            return {"online": self.info.running, "client": self.info.as_dict()}
        raise api_error(action, 1404, f"客户端通道不支持 {action}")

    async def send_reply(self, event: Any, chain: Any, quote: bool = True) -> bool:
        # 本通道收不到事件，理论上不会成为来源通道；保险起见返回 False 让框架换路
        return False


def create(**cfg: Any) -> ClientHostAdapter:
    """entry point 工厂：`ntqq-client = "arklink.ntqq.host:create"`。"""
    return ClientHostAdapter(**cfg)
