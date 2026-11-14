"""X11 引擎：把 QQ 关进一个「只有它自己」的 X 会话里，再用 xdotool 操作。

为什么需要它：Windows 上的 UI 通道必须抢前台焦点，这是「不影响其他程序」的硬冲突。
换到一台无头 Linux（Xvfb 虚拟屏幕 + QQ Linux 版）之后，抢占焦点这件事**失去了意义**——
那个屏幕里只有 QQ，没有任何别人在用的窗口。宿主机的焦点、剪贴板、前台程序一概不受影响。

链路：

    xdotool search --name QQ       找窗口
    xdotool windowactivate <id>    在 Xvfb 里激活（相当于本地窗口，无外部影响）
    xdotool mousemove --window ... 点会话搜索框 / 第一条结果 / 输入框
    xdotool type --clearmodifiers  输入收件人与正文（不经过系统剪贴板）
    xdotool key Return             发送

依赖只有 `xdotool`（Debian: `apt install xdotool`）和 `DISPLAY` 指向那个虚拟屏幕。
没有 `xdotool` 或 `DISPLAY` 不通时，`available` 为 False，上层换通道即可。

⚠️ 这套流程的坐标假设与 Windows 版一致（会话搜索框在左上、输入框在底部），
窗口大小变了就用 `input_xy` / `search_xy` 校准；校不准就退回 `WindowsHost`。

物理隔离 vs 虚拟屏幕：
* Xvfb（推荐）：无 GPU、无桌面、内存占用小，配 `x11vnc` 只用来首次扫码；
* 完整虚拟机：隔离更彻底，但要多跑一个内核与桌面，没必要。

首次登录必须扫码：QQ Linux 版没有「密码登录」以外的免交互通道，所以第一次要么
用 `x11vnc` 连上去扫，要么把 `data/` 目录（登录态）拷进来。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Any, Callable, Sequence
from urllib.parse import quote

from .send import SendRequest, SendResult

logger = logging.getLogger("arklink.ntqq.x11")

#: 会话列表顶部的搜索框（相对窗口左上角，默认布局）
DEFAULT_SEARCH_XY = (150, 65)
#: 聊天输入框（窗口比例，默认布局）
DEFAULT_INPUT_XY = (0.65, 0.87)


def uri_of(path: str) -> str:
    """本机路径 → `file://` URI。

    `text/uri-list` 要的是百分号编码后的形式：直接塞原始路径的话，
    文件名里带空格或中文就会解析失败，而且失败得毫无提示。
    """
    text = os.path.abspath(os.path.expanduser(str(path)))
    return "file://" + quote(text, safe="/")


class X11Engine:
    """用 xdotool 操作 Xvfb 里的 QQ。名字与 `SendEngine` 协议一致。"""

    name = "x11"
    #: 剪贴板粘文件（xclip 提供 text/uri-list），不需要注入
    supports_attachments = True

    def __init__(
        self,
        *,
        display: str = "",
        window_name: str = "QQ",
        search_xy: tuple[int, int] = DEFAULT_SEARCH_XY,
        input_xy: tuple[float, float] = DEFAULT_INPUT_XY,
        #: QQ 搜索结果的独立弹窗标题（点结果要按它的坐标，不能按主窗口的）
        search_popup_name: str = "综合搜索",
        type_delay: int = 15,
        open_delay: float = 0.7,
        #: 粘完附件等多久再回车：客户端要先把文件挂到输入框上
        attach_delay: float = 1.5,
        dry_run: bool = False,
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
        min_gap: float = 0.35,
    ) -> None:
        self.display = display or os.environ.get("ARKLINK_X11_DISPLAY", "") or os.environ.get("DISPLAY", "")
        self.window_name = window_name
        self.search_popup_name = search_popup_name
        self.search_xy = (int(search_xy[0]), int(search_xy[1]))
        self.input_xy = (float(input_xy[0]), float(input_xy[1]))
        self.type_delay = int(type_delay)
        self.open_delay = float(open_delay)
        self.attach_delay = float(attach_delay)
        self.dry_run = dry_run
        self.min_gap = float(min_gap)
        self._run_impl = runner or self._default_run
        self._last = 0.0
        self.last_action = ""

    # ---------------------------------------------------------------- 底层
    def _default_run(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if self.display:
            env["DISPLAY"] = self.display
        return subprocess.run(list(args), capture_output=True, text=True, timeout=20, env=env)

    def _xdo(self, *args: str) -> subprocess.CompletedProcess:
        cmd = ["xdotool", *args]
        try:
            return self._run_impl(cmd)
        except Exception as exc:  # 没装 / DISPLAY 不通
            logger.debug("xdotool 执行失败 %s：%s", args, exc)
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))

    def _run_with_input(self, args: Sequence[str], data: bytes) -> subprocess.CompletedProcess:
        """跑一个需要从 stdin 喂数据的命令（xclip 就是这么用的）。"""
        env = dict(os.environ)
        if self.display:
            env["DISPLAY"] = self.display
        try:
            return subprocess.run(
                list(args), input=data, capture_output=True, timeout=20, env=env
            )
        except Exception as exc:
            logger.debug("命令执行失败 %s：%s", args, exc)
            return subprocess.CompletedProcess(list(args), 127, b"", str(exc).encode())

    def _set_clipboard_files(self, paths: Sequence[str]) -> bool:
        """用 xclip 把文件放进剪贴板（`text/uri-list`）。

        和 Windows 的 `CF_HDROP` 是同一件事的两种写法：X11 侧接收方靠
        uri-list 拿到路径，客户端自己去读文件。同样只能放**本机已有**的文件。
        """
        if not shutil.which("xclip"):
            self.last_action = "x11: 没装 xclip，放不了文件到剪贴板（apt install xclip）"
            return False
        payload = "".join(uri_of(p) + "\r\n" for p in paths).encode("utf-8")
        proc = self._run_with_input(
            ["xclip", "-selection", "clipboard", "-t", "text/uri-list"], payload
        )
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
            self.last_action = f"x11: xclip 写入失败（{detail or proc.returncode}）"
            return False
        return True

    @property
    def available(self) -> bool:
        if not shutil.which("xdotool") and self._run_impl is self._default_run:
            return False
        if not self.display and self._run_impl is self._default_run:
            return False
        return bool(self.find_window())

    # ---------------------------------------------------------------- 窗口
    def find_window(self) -> str:
        """找 QQ 主窗口 id。

        QQ 会有一堆同名/近名窗口（Chromium 辅助窗口 1x1、搜索弹窗、
        各种页面弹窗），按「最后一个」抓必翻车（Windows 宿主侧踩过同款坑）。
        这里按窗口面积过滤 + 面积最大的优先；同面积时精确同名优先。
        """
        proc = self._xdo("search", "--name", self.window_name)
        if proc.returncode != 0:
            return ""
        ids = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        best, best_key = "", (0, False)
        for wid in ids:
            w, h = self._size(wid)
            if w < 400 or h < 300:  # 真主窗口不可能比这还小
                continue
            exact = self._window_name(wid) == self.window_name
            key = (w * h, exact)
            if key > best_key:
                best, best_key = wid, key
        return best

    def _size(self, wid: str) -> tuple[int, int]:
        proc = self._xdo("getwindowgeometry", "--shell", wid)
        values: dict[str, int] = {}
        for line in (proc.stdout or "").splitlines():
            key, _, raw = line.partition("=")
            if key.strip() in ("WIDTH", "HEIGHT") and raw.strip().isdigit():
                values[key.strip()] = int(raw.strip())
        return values.get("WIDTH", 0), values.get("HEIGHT", 0)

    def _window_name(self, wid: str) -> str:
        return (self._xdo("getwindowname", wid).stdout or "").strip()

    def _find_popup(self) -> str:
        """QQ 的搜索结果是一个独立 X 窗口（默认标题「综合搜索」）。"""
        proc = self._xdo("search", "--name", self.search_popup_name)
        if proc.returncode != 0:
            return ""
        ids = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        best, best_area = "", 0
        for wid in ids:
            w, h = self._size(wid)
            if w < 300 or h < 300:
                continue
            if w * h > best_area:
                best, best_area = wid, w * h
        return best

    def _activate(self, wid: str) -> bool:
        proc = self._xdo("windowactivate", "--sync", wid)
        return proc.returncode == 0

    def _geometry(self, wid: str) -> tuple[int, int]:
        proc = self._xdo("getwindowgeometry", "--shell", wid)
        if proc.returncode != 0:
            return (0, 0)
        values: dict[str, int] = {}
        for line in (proc.stdout or "").splitlines():
            key, _, raw = line.partition("=")
            if raw.strip().lstrip("-").isdigit():
                values[key.strip()] = int(raw.strip())
        return (values.get("WIDTH", 0), values.get("HEIGHT", 0))

    # ---------------------------------------------------------------- 动作
    def _click(self, wid: str, x: int, y: int) -> None:
        # --window 让坐标相对该窗口，不需要真实光标移动
        self._xdo("mousemove", "--window", wid, str(int(x)), str(int(y)))
        self._xdo("click", "--window", wid, "1")

    def _type(self, text: str) -> None:
        # 注意：type 走的是 X 键盘事件，不碰系统剪贴板
        self._xdo("type", "--clearmodifiers", "--delay", str(self.type_delay), text)

    async def send(self, request: SendRequest) -> SendResult:
        return self._send_blocking(request)

    def _send_blocking(self, request: SendRequest) -> SendResult:
        files = request.files
        remote = request.remote_files
        if remote:
            kinds = "、".join(sorted({e.kind for e in remote}))
            return SendResult(
                ok=False, code=1404,
                msg=(f"附件只有地址没有本地文件（{kinds}）：剪贴板只能放本机文件，"
                     "请先下载到磁盘再把路径放进 file 字段"),
                engine=self.name, text=request.text, echo=request.echo,
            )
        # 附件消息的文字部分要剔掉 [图片] 这类占位符
        text = request.caption if files else request.text
        if not text and not files:
            return SendResult(ok=False, code=1400, msg="空消息", engine=self.name, echo=request.echo)
        if self.dry_run:
            self.last_action = (
                f"[dry-run] x11 → {request.search_key}: {text[:40]!r}"
                + (f" + {len(files)} 个附件" if files else "")
            )
            logger.info(self.last_action)
            return SendResult(ok=True, dry_run=True, engine=self.name, text=text, echo=request.echo,
                              msg="dry-run：没有真的发送")

        gap = self.min_gap - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)

        wid = self.find_window()
        if not wid:
            return SendResult(ok=False, code=1404, msg=f"X11 里找不到窗口 {self.window_name!r}（DISPLAY={self.display}）",
                              engine=self.name, text=text, echo=request.echo)
        if not self._activate(wid):
            return SendResult(ok=False, code=1404, msg="激活窗口失败（Xvfb 没起？）",
                              engine=self.name, text=text, echo=request.echo)

        width, height = self._geometry(wid)
        if width <= 0 or height <= 0:
            width, height = 1000, 800  # 拿不到尺寸就按默认比例点，失败会体现在结果里

        # 1) 会话搜索 → 2) 第一条结果 → 3) 输入框 → 4) 正文 → 5) 回车
        self._click(wid, self.search_xy[0], self.search_xy[1])
        time.sleep(0.25)
        self._type(str(request.search_key))
        time.sleep(self.open_delay)
        # 搜索结果是独立弹窗（标题见 search_popup_name），叠在主窗口上方——
        # 按主窗口坐标点「第一条结果」和输入框会全落进弹窗（真机踩过的坑）
        popup = self._find_popup()
        if popup:
            pw, ph = self._size(popup)
            # 结果列表在弹窗搜索框下方，取左上区域第一条
            self._click(popup, max(60, int(pw * 0.15)), min(200, int(ph * 0.25)))
            time.sleep(self.open_delay + 0.3)
            # 点中结果后弹窗应自动关；没关就按 Escape 收掉，防止挡住输入框
            if self._find_popup():
                self._xdo("key", "--clearmodifiers", "Escape")
                time.sleep(0.3)
        else:
            self._click(wid, self.search_xy[0] + 60, self.search_xy[1] + 91)
            time.sleep(self.open_delay + 0.3)
        # 选完会话把主窗口重新拉到前台，输入框的点击/键盘才进得去
        self._activate(wid)
        self._click(wid, int(width * self.input_xy[0]), int(height * self.input_xy[1]))
        time.sleep(0.15)
        if text:
            # xdotool type 走键盘事件，不碰剪贴板，所以文字和附件可以先后放
            self._type(text)
            time.sleep(0.15)
        if files:
            if not self._set_clipboard_files(files):
                return SendResult(ok=False, code=1404, msg=f"放文件到剪贴板失败：{self.last_action}",
                                  engine=self.name, text=text, echo=request.echo)
            self._xdo("key", "--clearmodifiers", "ctrl+v")
            time.sleep(self.attach_delay)
        self._xdo("key", "--clearmodifiers", "Return")
        self._last = time.time()
        self.last_action = f"x11 → {request.search_key}: {len(text)} 字" + (
            f" + {len(files)} 个附件" if files else ""
        )
        logger.info(self.last_action)
        extra: dict[str, Any] = {"via": "x11", "window": wid, "display": self.display}
        if files:
            extra["attachments"] = len(files)
        return SendResult(
            ok=True,
            message_id=0,
            msg="已通过 X11 通道发送",
            engine=self.name,
            text=text,
            echo=request.echo,
            extra=extra,
        )
