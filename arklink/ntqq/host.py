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
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..base import LinkAdapter, api_error

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
GMEM_MOVEABLE = 0x0002
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
    def set_clipboard(self, text: str) -> bool:
        if not self.ok:
            return False
        u = self.user32
        k = self.kernel32
        if not u.OpenClipboard(None):
            return False
        try:
            u.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = k.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            k.GlobalUnlock(handle)
            u.SetClipboardData(CF_UNICODETEXT, handle)
            return True
        finally:
            u.CloseClipboard()

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
        windows: list[tuple[int, int, str]] = []
        for pid in pids:
            for hwnd, cls, title in _W32.windows_of_pid(pid):
                if cls in WINDOW_CLASSES or "QQ" in title:
                    windows.append((pid, hwnd, title))
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

        # 多开时窗口标题都叫 "QQ"，必须按账号挑，否则会拿另一个号发出去
        if self.uin and len(windows) > 1:
            for pid, hwnd, title in windows:
                if self.uin_of_pid(pid) == self.uin:
                    return build(pid, hwnd, title)
            self.last_action = f"没有找到账号 {self.uin} 的窗口（多开？）"
        return build(*windows[0])

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
        """往当前打开的会话发文本：先点输入框，再粘贴、回车。"""
        if not text:
            return False
        if self.dry_run:
            self.last_action = f"[dry-run] send_text {len(text)} 字: {text[:40]!r}"
            logger.info(self.last_action)
            return True
        hwnd = hwnd or self.detect().hwnd
        left, top, right, bottom = _W32.rect(hwnd)
        if not right:
            self.last_action = "send_text: 拿不到窗口位置"
            return False
        # 输入框在聊天区底部（9.9.31 默认布局测得：约 65% 宽、87% 高）
        _W32.click(
            left + int((right - left) * self.input_xy[0]),
            top + int((bottom - top) * self.input_xy[1]),
        )
        mods, vk = parse_hotkey(self.paste_hotkey)
        if not _W32.set_clipboard(text):
            self.last_action = "send_text: 剪贴板写入失败"
            return False
        _W32.chord([*mods, vk])
        time.sleep(0.15)
        self._press_send()
        self.last_action = f"send_text: {len(text)} 字"
        return True

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
        info = self.detect()
        if not info.running or not info.hwnd:
            self.last_action = "open_and_send: 客户端不在线或没有窗口"
            return False
        if not _W32.focus(info.hwnd):
            self.last_action = "open_and_send: 没能拿到前台焦点（放弃，不往未知窗口里打字）"
            return False
        left, top, right, bottom = _W32.rect(info.hwnd)
        if not right:
            self.last_action = "open_and_send: 拿不到窗口位置"
            return False
        # 1) 会话列表顶部的搜索框
        _W32.click(left + 150, top + 65)
        self._type(str(peer))
        time.sleep(self.open_delay)
        # 2) 搜索结果第一条（联系人区，固定出现在搜索框正下方）
        _W32.click(left + 210, top + 156)
        time.sleep(self.open_delay + 0.4)  # 等会话加载
        # 3) 输入框 → 粘贴 → 回车
        return self.send_text(text, hwnd=info.hwnd)

    # ---- 内部 ----
    def _type(self, text: str) -> None:
        if not _W32.set_clipboard(text):
            return
        mods, vk = parse_hotkey(self.paste_hotkey)
        _W32.chord([*mods, vk])
        time.sleep(0.1)

    def _press_send(self) -> None:
        if self.send_key.lower() == "enter":
            _W32.chord([VK_RETURN])
        else:
            mods, vk = parse_hotkey(self.send_key)
            _W32.chord([*mods, vk])


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
    """把「操作官方客户端」挂成一个通道（只发文本，不收消息）。

    bot.toml：
        [[adapters]]
        type = "ntqq-client"
        dry_run = false              # 校准好热键后再关掉
        search_hotkey = "^f"
        process_names = ["QQ.exe"]
    """

    name = "ntqq-client"
    #: 明确声明：本通道收不到事件，框架不要指望它做回复路由
    supports_receive = False

    def __init__(
        self,
        host: ClientHost | None = None,
        dry_run: bool = True,
        search_hotkey: str = "^f",
        paste_hotkey: str = "^v",
        process_names: Iterable[str] | None = None,
        poll_interval: float = 5.0,
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
            await asyncio.sleep(self.poll_interval)

    async def disconnect(self) -> None:
        self._connected.clear()
        self.info = ClientInfo()

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        if action in ("send_msg", "send_message", "send_private_msg", "send_group_msg"):
            text = plain_text_of(params.get("message"))
            if not text:
                raise api_error(action, 1404, "客户端通道只支持纯文本，请换通道发送")
            peer = params.get("group_id") or params.get("user_id")
            if not peer:
                raise api_error(action, 1404, "缺少 group_id/user_id")
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
