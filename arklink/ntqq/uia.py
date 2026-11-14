"""UI 通道的 UIA 实现：定位控件、点击、读消息。

全部走「读屏软件」路径：QQ NT 的 accessibility 默认关闭，置位系统
「屏幕阅读器」标志（SPI_SETSCREENREADER）后 Chromium 才会把控件树暴露
给 UIA。脚本退出时恢复原值。不注入进程、不挂钩、不碰内存——对 QQ 来说
和讲述人/读屏软件没有区别。

真机验证过的事实（2026-11-14，QQ 9.9.x，Windows 150% 缩放）：

* SendInput / WM_KEYDOWN / WM_CHAR(CR) 的回车注入全部被客户端过滤；
  WM_CHAR 只能进普通字符。发送要靠 UIA 定位 ButtonControl「发送」后
  PostMessage 点击。
* 鼠标：SendInput 无效，PostMessage WM_LBUTTONDOWN/UP 有效；坐标必须是
  Chrome_RenderWidgetHostHWND 客户区坐标（UIA 的 BoundingRectangle 是
  屏幕物理像素，减渲染窗口原点即可）。
* 附件：WM_PASTE / Ctrl+V / WM_CHAR 0x16 / WM_DROPFILES 都粘不上文件。
  正规路径是点工具栏「文件」按钮 → 原生「请选择」对话框（WM_SETTEXT 填
  路径 + Enter 确认，原生对话框不过滤键盘）→ QQ 弹「发送给 XX」确认框
  （主窗口的**子** Chromium 窗口，EnumWindows 看不到，只能在主窗口 UIA
  树里找 ButtonControl「发送(1)」）→ PostMessage 点击。
* 聊天开关是 toggle：对已打开的会话项再点一次会关闭聊天窗口。用
  「发送按钮是否存在」做状态判定，先关再开，保证最终状态是打开。
"""

from __future__ import annotations

import ctypes
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("arklink.ntqq.uia")

IS_WINDOWS = hasattr(ctypes, "windll")

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0045
SPIF = 0x01 | 0x02

WM_LB_DOWN = 0x0201
WM_LB_UP = 0x0202
WM_CHAR = 0x0102
WM_SETTEXT = 0x000C
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_RETURN = 0x0D

#: 会话项两次查询的坐标容差（物理像素）
STABLE_TOLERANCE = 3
#: 点开会话后等聊天窗口渲染
OPEN_DELAY = 1.8
CLOSE_DELAY = 1.5
#: 粘贴/填对话框后的等待
DIALOG_DELAY = 2.0
#: 时间戳形状（消息行里的 TextControl）
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$|^昨天|^星期[一二三四五六日]|^\d{2}/\d{2}$")


def _user32():
    if not hasattr(ctypes, "windll"):
        raise OSError("非 Windows 平台")
    return ctypes.windll.user32


def _screen_reader() -> tuple[ctypes.windll, bool]:
    u = _user32()
    prev = ctypes.c_bool(False)
    u.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(prev), 0)
    return u, bool(prev.value)


class _Accessibility:
    """屏幕阅读器标志的置位/还原（QQ NT 控件树激活的前置条件）。"""

    def __init__(self) -> None:
        u, self.prev = _screen_reader()
        self.user32 = u
        self._on = None

    def ensure(self, probe_hwnd: int | None = None, retries: int = 4) -> bool:
        import uiautomation as auto

        for i in range(retries):
            self.user32.SystemParametersInfoW(SPI_SETSCREENREADER, True, None, SPIF)
            time.sleep(2.5 if i == 0 else 2.0)
            if self._probe(probe_hwnd):
                self._on = True
                return True
            # 树没起来：先关再开，逼 Chromium 重建
            self.user32.SystemParametersInfoW(SPI_SETSCREENREADER, False, None, SPIF)
            time.sleep(0.6)
        self._on = True
        return False

    def _probe(self, hwnd: int | None) -> bool:
        """就绪判定：目标窗口的树里能走到按钮控件（窗口标题栏按钮一定存在）。"""
        try:
            if hwnd:
                ctrl = _find_window_by_hwnd(hwnd)
                if ctrl is None:
                    return False
                for c, _d in _walk(ctrl, depth=6):
                    if c.ControlTypeName == "ButtonControl":
                        return True
                return False
            import uiautomation as auto

            root = auto.GetRootControl()
            return root is not None and len(root.GetChildren()) > 0
        except Exception:
            return False

    def restore(self) -> None:
        if self._on and not self.prev:
            self.user32.SystemParametersInfoW(SPI_SETSCREENREADER, False, None, SPIF)
            self._on = False

    def __enter__(self) -> "_Accessibility":
        self.ensure()
        return self

    def __exit__(self, *_exc) -> None:
        self.restore()


@dataclass
class ChatMessage:
    """从聊天窗口控件树里解析出的一条消息。"""

    sender: str = ""
    text: str = ""
    time: str = ""
    outgoing: bool = False     # True = 自己发的（气泡靠右）
    index: int = 0

    @property
    def fingerprint(self) -> str:
        """去重指纹：滚动/翻页会改变 index，所以只用（发送者|时间|正文）。"""
        return f"{self.sender}|{self.time}|{self.text}"


@dataclass
class UiaWindow:
    """一个 QQ 主窗口（Chromium）上的操作集合。"""

    hwnd: int
    control: Any = None          # uiautomation ControlWrapper
    _render: Any = None          # Chrome_RenderWidgetHostHWND
    _access: _Accessibility | None = None
    _session_rects: dict = field(default_factory=dict)

    # ---------------- 基础 ----------------
    def attach(self, access: _Accessibility) -> bool:
        """从桌面根的子窗口里按句柄找窗口包装器。

        注意不能用 `ControlFromHandle`：它返回 PaneControl，这个版本的
        uiautomation 没给它实现 WalkControl，遍历会静默失败。
        """
        self._access = access
        ctrl = _find_window_by_hwnd(self.hwnd)
        if ctrl is None:
            return False
        self.control = ctrl
        return True

    @property
    def render(self) -> Any:
        if self._render is None:
            for c, _d in _walk(self.control, depth=5):
                if c.ClassName == "Chrome_RenderWidgetHostHWND":
                    self._render = c
                    break
        return self._render

    def rect(self):
        b = self.control.BoundingRectangle
        return (b.left, b.top, b.width(), b.height())

    def click_at(self, screen_x: int, screen_y: int) -> None:
        """把点击投到渲染窗口：屏幕坐标 → 客户区坐标。"""
        u = _user32()
        rb = self.render.BoundingRectangle
        x = screen_x - rb.left
        y = screen_y - rb.top
        lp = (y << 16) | (x & 0xFFFF)
        u.PostMessageW(self.render.NativeWindowHandle, WM_LB_DOWN, 1, lp)
        time.sleep(0.06)
        u.PostMessageW(self.render.NativeWindowHandle, WM_LB_UP, 0, lp)

    def type_text(self, text: str, gap: float = 0.05) -> None:
        u = _user32()
        rh = self.render.NativeWindowHandle
        for ch in text:
            u.PostMessageW(rh, WM_CHAR, ord(ch) & 0xFFFF, 0)
            time.sleep(gap)

    # ---------------- 定位 ----------------
    def find_session_item(self, peer: str):
        """会话列表里名为 peer 的 TextControl（要求连续两次 rect 一致才算稳）。"""
        lst = self.control.WindowControl(Name="会话列表")
        if not lst.Exists(2):
            return None
        a = _find_text(lst, peer)
        if a is None:
            return None
        r1 = _rect_of(a)
        time.sleep(0.35)
        b = _find_text(lst, peer)
        if b is None:
            return None
        r2 = _rect_of(b)
        if not all(abs(x - y) <= STABLE_TOLERANCE for x, y in zip(r1, r2)):
            return None            # 列表在滚动/刷新，宁可不做
        return b

    def find_send_button(self, name: str = "发送"):
        """聊天窗口的发送按钮。确认框里的是「发送(1)」，聊天框里是「发送」。"""
        best = None
        for c, _d in _walk(self.control, depth=18):
            try:
                if c.ControlTypeName != "ButtonControl":
                    continue
                if (c.Name or "").strip() != name:
                    continue
                b = c.BoundingRectangle
                if b.width() > 5 and b.height() > 5:
                    best = c
            except Exception:
                continue
        return best

    def chat_is_open(self) -> bool:
        return self.find_send_button() is not None

    def find_toolbar_button(self, name: str):
        for c, _d in _walk(self.control, depth=18):
            try:
                if c.ControlTypeName == "ButtonControl" and (c.Name or "").strip() == name:
                    b = c.BoundingRectangle
                    if b.width() > 5 and b.height() > 5:
                        return c
            except Exception:
                continue
        return None

    # ---------------- 会话操作 ----------------
    def open_chat(self, peer: str) -> bool:
        """打开与 peer 的聊天（toggle 感知：已开就先关再开）。"""
        item = self.find_session_item(peer)
        if item is None:
            return False
        r = _rect_of(item)
        cx, cy = r[0] + r[2] // 2, r[1] + r[3] // 2
        if self.chat_is_open():
            self.click_at(cx, cy)
            time.sleep(CLOSE_DELAY)
            if self.chat_is_open():
                logger.warning("关闭聊天失败，中止 open_chat(%s)", peer)
                return False
        self.click_at(cx, cy)
        time.sleep(OPEN_DELAY)
        return self.chat_is_open()

    # ---------------- 发送 ----------------
    def send_text_now(self, text: str) -> bool:
        """往**当前打开**的聊天输入文字并点发送（焦点在输入框时才可靠）。"""
        self.type_text(text)
        time.sleep(0.4)
        btn = self.find_send_button()
        if btn is None:
            return False
        b = _rect_of(btn)
        self.click_at(b[0] + b[2] // 2, b[1] + b[3] // 2)
        time.sleep(1.0)
        return True

    def send_files_now(self, paths: list[str], text: str = "") -> bool:
        """往**当前打开**的聊天发本地文件（文件对话框路径）。"""
        u = _user32()
        if text:
            self.type_text(text)
            time.sleep(0.2)
        btn = self.find_toolbar_button("文件")
        if btn is None:
            logger.warning("找不到工具栏「文件」按钮")
            return False
        b = _rect_of(btn)
        self.click_at(b[0] + b[2] // 2, b[1] + b[3] // 2)
        time.sleep(DIALOG_DELAY)

        # 原生「请选择」对话框：#32770 顶层窗口（GetChildren 枚举不到，要靠句柄遍历）
        dlg = _find_native_dialog(u)
        if dlg is None:
            logger.warning("文件对话框没有弹出")
            return False
        try:
            if not _fill_open_dialog(u, dlg, paths):
                return False
            # Enter 确认（原生对话框接受键盘注入）
            u.PostMessageW(dlg, WM_KEYDOWN, VK_RETURN, 0x001C0001)
            time.sleep(0.06)
            u.PostMessageW(dlg, WM_KEYUP, VK_RETURN, 0xC01C0001)
            time.sleep(DIALOG_DELAY)
            # QQ 的「发送给 XX」确认框（主窗口子窗口，只能从 UIA 树里找）
            return self._click_send_confirm()
        finally:
            pass

    def _click_send_confirm(self) -> bool:
        btn = None
        for c, _d in _walk(self.control, depth=22):
            try:
                if c.ControlTypeName != "ButtonControl":
                    continue
                name = (c.Name or "").strip()
                if name.startswith("发送(") and _rect_of(c)[2] > 20:
                    btn = c
                    break
            except Exception:
                continue
        if btn is None:
            logger.warning("没有弹出「发送(N)」确认框（可能文件没挂上）")
            return False
        b = _rect_of(btn)
        self.click_at(b[0] + b[2] // 2, b[1] + b[3] // 2)
        time.sleep(1.5)
        return True

    # ---------------- 接收 ----------------
    def read_messages(self, limit: int = 50) -> list[ChatMessage]:
        """读**当前打开**聊天窗口里可见的消息气泡。

        布局是启发式解析：消息行的直接子 GroupControl 里，带名字的
        GroupControl 是头像（名字=发送者），TextControl 里长得像时间的
        是时间戳，剩下的按 y 排序拼成正文。气泡中心在聊天区右半 → 自己发的。
        """
        msgs: list[ChatMessage] = []
        chat_area = self._chat_area_rect()
        if chat_area is None:
            return msgs
        ca_l, ca_t, ca_w, ca_h = chat_area
        rows = self._message_rows()
        idx = 0
        for row in rows:
            rr = _rect_of(row)
            if rr[2] <= 0:
                continue
            sender = ""
            stamp = ""
            spans: list[tuple[int, int, str]] = []          # (top, left, text)
            for c, _d in _walk(row, depth=8):
                try:
                    tn = c.ControlTypeName
                    b = c.BoundingRectangle
                    if tn == "GroupControl" and (c.Name or "").strip() and b.width() <= 80:
                        sender = c.Name.strip()
                    elif tn == "TextControl":
                        name = (c.Name or "").strip()
                        if not name:
                            continue
                        if TIME_RE.match(name):
                            stamp = stamp or name
                        else:
                            spans.append((b.top, b.left, name))
                except Exception:
                    continue
            if not spans and not sender:
                continue
            spans.sort()
            text = "".join(s for _, _, s in spans).strip()
            if not text and not sender:
                continue
            outgoing = (spans and spans[0][1] > ca_l + ca_w * 0.55) or (
                spans and spans[-1][1] + len(spans[-1][2]) * 12 > ca_l + ca_w * 0.8
            )
            idx += 1
            msgs.append(ChatMessage(sender=sender, text=text, time=stamp, outgoing=bool(outgoing), index=idx))
        return msgs[-limit:]

    def _message_rows(self) -> list[Any]:
        """聊天区 GroupControl 的直接子节点（每个对应一条消息/一个时间分隔）。"""
        area = self._chat_area_control()
        if area is None:
            return []
        out = []
        for c in area.GetChildren():
            try:
                if c.ControlTypeName == "GroupControl":
                    b = c.BoundingRectangle
                    if b.height() > 10:
                        out.append(c)
            except Exception:
                continue
        return out

    def _chat_area_control(self):
        """包含消息气泡的大 GroupControl：聊天区里「有最多子 GroupControl 的那个」。"""
        best = None
        best_n = 0
        ca = self._chat_area_rect()
        for c, _d in _walk(self.control, depth=12):
            try:
                if c.ControlTypeName != "GroupControl":
                    continue
                b = c.BoundingRectangle
                if ca is None:
                    continue
                n = sum(1 for ch in c.GetChildren() if ch.ControlTypeName == "GroupControl")
                if n > best_n and b.height() > 100:
                    best, best_n = c, n
            except Exception:
                continue
        return best

    def _chat_area_rect(self):
        """聊天区（会话列表右侧、工具栏上方）的近似矩形。"""
        wr = self.rect()
        if wr[2] <= 0:
            return None
        # 会话列表固定在左侧约 1/4 宽；聊天区占其余部分
        left = wr[0] + int(wr[2] * 0.22)
        return (left, wr[1], wr[2] - int(wr[2] * 0.22), wr[3])


# ---------------------------------------------------------------- 顶层通道


class UiChannel:
    """对一个 QQ 主窗口句柄的封装：发送 + 接收。"""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._access: _Accessibility | None = None
        self._win: UiaWindow | None = None

    def __enter__(self) -> "UiChannel":
        self._access = _Accessibility()
        self._access.ensure(self.hwnd)
        self._win = UiaWindow(self.hwnd)
        if not self._win.attach(self._access):
            raise OSError(f"UIA 定位窗口失败 hwnd={self.hwnd}")
        return self

    def __exit__(self, *_exc) -> None:
        if self._access is not None:
            self._access.restore()
        self._win = None

    @property
    def win(self) -> UiaWindow:
        assert self._win is not None
        return self._win

    def send_text(self, peer: str, text: str) -> bool:
        with self:
            if not self.win.open_chat(peer):
                return False
            return self.win.send_text_now(text)

    def send_files(self, peer: str, paths: list[str], text: str = "") -> bool:
        with self:
            if not self.win.open_chat(peer):
                return False
            return self.win.send_files_now(paths, text=text)

    def read_messages(self) -> list[ChatMessage]:
        """读当前打开的聊天（不动会话状态）。"""
        with self:
            return self.win.read_messages()

    def read_peer_messages(self, peer: str) -> list[ChatMessage]:
        """打开 peer 的聊天再读（轮询接收用；会改变当前会话）。"""
        with self:
            if not self.win.open_chat(peer):
                return []
            return self.win.read_messages()

    def send_current_text(self, text: str) -> bool:
        """往已经打开的聊天发文字（不开会话，配合 send_text 用）。"""
        with self:
            if not self.win.chat_is_open():
                return False
            return self.win.send_text_now(text)

    def send_current_files(self, paths: list[str], text: str = "") -> bool:
        with self:
            if not self.win.chat_is_open():
                return False
            return self.win.send_files_now(paths, text=text)


# ---------------------------------------------------------------- helpers


def _find_window_by_hwnd(hwnd: int):
    """在桌面根的子窗口里按 NativeWindowHandle 找窗口包装器（WindowControl）。"""
    import uiautomation as auto

    root = auto.GetRootControl()
    if root is None:
        return None
    for w in root.GetChildren():
        try:
            if w.NativeWindowHandle == hwnd:
                return w
        except Exception:
            continue
    return None


def _walk(ctrl, depth: int):
    """浅层遍历（必须用模块级 auto.WalkControl：PaneControl 没有实例方法）。"""
    import uiautomation as auto

    try:
        for c, d in auto.WalkControl(ctrl, includeTop=False, maxDepth=depth):
            yield c, d
    except Exception:
        return


def _find_text(lst, name: str):
    for c, _d in _walk(lst, depth=12):
        try:
            if c.ControlTypeName == "TextControl" and (c.Name or "") == name:
                return c
        except Exception:
            continue
    return None


def _rect_of(ctrl):
    b = ctrl.BoundingRectangle
    return (b.left, b.top, b.width(), b.height())


def _find_native_dialog(u) -> int | None:
    """找 QQ 弹出的「请选择」文件对话框（#32770，可见）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _lp):
        h = ctypes.cast(hwnd, ctypes.c_void_p).value or 0
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value == "#32770" and u.IsWindowVisible(hwnd):
            found.append(h)
        return True

    u.EnumWindows(cb, None)
    return found[0] if found else None


def _fill_open_dialog(u, dlg_hwnd: int, paths: list[str]) -> bool:
    """在「请选择」对话框里填文件名。

    标准对话框的文件名编辑框支持引号分隔的多文件：`"a.png" "b.png"`。
    UIA 定位编辑框（Name 以「文件名」开头），用 WM_SETTEXT 填——原生控件
    不过滤这条消息。
    """
    import uiautomation as auto

    dlg = auto.ControlFromHandle(dlg_hwnd)
    if dlg is None or not dlg.Exists(3):
        return False
    edit = None
    for c, _d in _walk(dlg, depth=8):
        try:
            if c.ControlTypeName == "EditControl" and "文件名" in (c.Name or ""):
                edit = c
                break
        except Exception:
            continue
    if edit is None or not edit.NativeWindowHandle:
        logger.warning("对话框里找不到「文件名」编辑框")
        return False
    quoted = " ".join(f'"{p}"' for p in paths)
    buf = ctypes.create_unicode_buffer(quoted)
    u.SendMessageW(edit.NativeWindowHandle, WM_SETTEXT, 0, buf)
    time.sleep(0.3)
    return True
