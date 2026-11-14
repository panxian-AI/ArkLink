"""X11 引擎测试：命令序列、失败路径、dry-run。用假 runner，不需要真的 X。

真实环境里 xdotool 的每一行输出都验证过吗？没有——所以这里只锁「我们发了什么命令、
按什么顺序、出错怎么报」，这是能离线保证的部分。
"""

from __future__ import annotations

import asyncio
import subprocess

from arklink.ntqq.elements import Element
from arklink.ntqq.send import SendRequest
from arklink.ntqq.x11 import X11Engine


class FakeRunner:
    def __init__(self, window: str = "12345", width: int = 1000, height: int = 800) -> None:
        self.calls: list[list[str]] = []
        self.window = window
        self.width = width
        self.height = height
        self.fail_on: str | None = None
        #: 多窗口场景：{id: (title, w, h)}；空 dict 时退回单窗口模式
        self.windows: dict[str, tuple[str, int, int]] = {}

    def __call__(self, args):
        cmd = list(args)
        self.calls.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        if cmd[1:3] == ["search", "--name"]:
            if self.windows:
                import re

                pattern = cmd[3]
                hits = [wid for wid, (title, _w, _h) in self.windows.items()
                        if re.search(pattern, title)]
                return subprocess.CompletedProcess(cmd, 0, "\n".join(hits) + ("\n" if hits else ""), "")
            return subprocess.CompletedProcess(cmd, 0, f"{self.window}\n", "")
        if cmd[1] == "getwindowname":
            wid = cmd[2] if len(cmd) > 2 else ""
            name = self.windows.get(wid, (self.window, 0, 0))[0]
            return subprocess.CompletedProcess(cmd, 0, f"{name}\n", "")
        if cmd[1] == "getwindowgeometry":
            wid = cmd[-1]  # xdotool getwindowgeometry --shell <id>
            if self.windows and wid in self.windows:
                _, w, h = self.windows[wid]
            else:
                w, h = self.width, self.height
            out = f"WIDTH={w}\nHEIGHT={h}\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


def _req(text="hi", peer="测试对象") -> SendRequest:
    return SendRequest(chat_type=1, peer_uid=peer, elements=[Element(kind="text", text=text)],
                       peer_name=peer)


def test_dry_run_touches_nothing():
    runner = FakeRunner()
    engine = X11Engine(runner=runner, dry_run=True, display=":99")
    r = asyncio.run(engine.send(_req()))
    assert r.ok and r.dry_run
    assert runner.calls == []


def test_send_sequence():
    runner = FakeRunner()
    engine = X11Engine(runner=runner, display=":99", min_gap=0.0, open_delay=0.0)
    r = asyncio.run(engine.send(_req("你好", "测试对象")))
    assert r.ok and r.extra["via"] == "x11"
    verbs = runner.verbs()
    assert verbs[0] == "search"
    assert "windowactivate" in verbs
    # 搜索框 + 输入框；选会话用回车，不再是第三次点击
    assert verbs.count("click") == 2
    key_calls = [" ".join(c) for c in runner.calls if c[1] == "key"]
    # 选会话必须是回车：点坐标会点进"聊天记录"分组，会话不打开就会发错人
    assert any("Return" in k for k in key_calls)
    # 打字前必须先清空搜索框和输入框，否则两次搜索会拼在一起（"505阿"）
    assert sum("ctrl+a" in k for k in key_calls) >= 2
    assert sum("Delete" in k for k in key_calls) >= 2
    # 正文要么走剪贴板粘贴，要么退回逐字 type，但内容必须落到输入框
    flat = " ".join(" ".join(c) for c in runner.calls)
    assert "你好" in flat or any("ctrl+v" in k for k in key_calls)


def test_no_window_reports_404():
    runner = FakeRunner(window="")
    engine = X11Engine(runner=runner, display=":99", min_gap=0.0)
    r = asyncio.run(engine.send(_req()))
    assert not r.ok and r.code == 1404 and "找不到窗口" in r.msg


def test_activate_failure_reports():
    class FailActivate(FakeRunner):
        def __call__(self, args):
            if list(args)[1:2] == ["windowactivate"]:
                self.calls.append(list(args))
                return subprocess.CompletedProcess(list(args), 1, "", "")
            return super().__call__(args)

    engine = X11Engine(runner=FailActivate(), display=":99", min_gap=0.0)
    r = asyncio.run(engine.send(_req()))
    assert not r.ok and "激活窗口失败" in r.msg


def test_zero_size_window_is_not_a_main_window():
    """1x1 / 0x0 的辅助窗口不是主窗口：宁可报 404，也不要往幽灵窗口里打字。"""
    runner = FakeRunner(width=0, height=0)
    engine = X11Engine(runner=runner, display=":99", min_gap=0.0, open_delay=0.0)
    r = asyncio.run(engine.send(_req()))
    assert not r.ok and r.code == 1404


def test_find_window_picks_largest_not_last():
    """QQ 有一堆同名窗口（1x1 辅助窗口、各种弹窗），按「最后一个」必翻车。"""
    runner = FakeRunner()
    runner.windows = {
        "1": ("QQ", 1, 1),
        "2": ("qq", 16, 16),
        "3": ("QQ", 720, 640),
        "4": ("QQ", 960, 640),
        "5": ("qq", 200, 200),
    }
    engine = X11Engine(runner=runner, display=":99")
    assert engine.find_window() == "4"


def test_find_window_ignores_search_popup_size(monkeypatch):
    """搜索弹窗（综合搜索）不该被当成主窗口——它比主窗口小，面积排序已经排除。"""
    runner = FakeRunner()
    runner.windows = {"10": ("QQ", 960, 640), "11": ("综合搜索", 680, 680)}
    engine = X11Engine(runner=runner, display=":99")
    assert engine.find_window() == "10"


def test_search_popup_is_escaped_not_clicked():
    """残留的独立「综合搜索」弹窗要 Escape 收掉：点它会把消息打进弹窗。"""
    runner = FakeRunner()
    runner.windows = {"100": ("QQ", 960, 640), "200": ("综合搜索", 680, 680)}
    engine = X11Engine(runner=runner, display=":99", min_gap=0.0, open_delay=0.0)
    r = asyncio.run(engine.send(_req()))
    assert r.ok
    clicks = [c for c in runner.calls if c[1] == "click"]
    targets = [c[3] for c in clicks]
    assert "200" not in targets, "不应该往搜索弹窗里点"
    assert any("Escape" in " ".join(c) for c in runner.calls if c[1] == "key")


def test_empty_text_rejected_before_any_call():
    runner = FakeRunner()
    engine = X11Engine(runner=runner, display=":99")
    r = asyncio.run(engine.send(_req(text="")))
    assert not r.ok and r.code == 1400 and runner.calls == []


def test_available_needs_window():
    engine = X11Engine(runner=FakeRunner(window=""), display=":99")
    assert engine.available is False
    engine2 = X11Engine(runner=FakeRunner(window="999"), display=":99")
    assert engine2.available is True


# ---------------------------------------------------------------- 附件（xclip）

def test_uri_of_percent_encodes():
    """text/uri-list 要百分号编码，否则带空格/中文的文件名解析不出来。"""
    from arklink.ntqq.x11 import uri_of

    uri = uri_of("/tmp/a b.png")
    assert uri.startswith("file://")
    assert "%20" in uri


def test_set_clipboard_files_uses_uri_list(monkeypatch):
    import arklink.ntqq.x11 as X

    engine = X11Engine()
    seen: dict = {}

    def fake_run(args, data):
        seen["args"], seen["data"] = list(args), data
        return subprocess.CompletedProcess(list(args), 0, b"", b"")

    monkeypatch.setattr(X.shutil, "which", lambda name: "/usr/bin/xclip")
    monkeypatch.setattr(engine, "_run_with_input", fake_run)
    assert engine._set_clipboard_files(["/tmp/a.png"]) is True
    assert "xclip" in seen["args"][0] and "text/uri-list" in seen["args"]
    assert seen["data"].startswith(b"file://")


def test_set_clipboard_files_without_xclip(monkeypatch):
    import arklink.ntqq.x11 as X

    engine = X11Engine()
    monkeypatch.setattr(X.shutil, "which", lambda name: None)
    assert engine._set_clipboard_files(["/tmp/a.png"]) is False
    assert "xclip" in engine.last_action


def test_send_with_attachment_dry_run():
    engine = X11Engine(dry_run=True)
    request = SendRequest(
        chat_type=2,
        peer_uid="测试对象",
        elements=[Element(kind="image", file="/tmp/a.png")],
    )
    result = asyncio.run(engine.send(request))
    assert result.ok and "1 个附件" in engine.last_action


def test_send_rejects_remote_attachment():
    engine = X11Engine(dry_run=True)
    request = SendRequest(
        chat_type=2,
        peer_uid="测试对象",
        elements=[Element(kind="image", file="https://x/a.png")],
    )
    result = asyncio.run(engine.send(request))
    assert not result.ok and result.code == 1404 and "本地文件" in result.msg


def test_chinese_prefers_clipboard_paste(monkeypatch):
    """中文正文优先走剪贴板粘贴（原子写入，不会错字）。

    xdotool type 打非 ASCII 靠临时改 keycode 映射，QQ 读键时机不同就会拿到
    上一次映射（"验证"→"阿证"、"无"→"阿"），60ms 也不保险，只能兜底。
    注意 xclip 写入必须**不接管道**，否则 fork 出来的剪贴板守护进程持有
    管道，run() 会等到 20s 超时并静默退回打字——VM 里所有"阿"字的根因。
    """
    monkeypatch.setattr("arklink.ntqq.x11.shutil.which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner()
    engine = X11Engine(runner=runner, display=":99", min_gap=0.0, open_delay=0.0)
    store = {"clip": b""}
    writes = []

    def fake_input(args, data, want_stdout=False):
        if want_stdout:
            return subprocess.CompletedProcess(list(args), 0, store["clip"], b"")
        store["clip"] = data
        writes.append(data.decode("utf-8"))
        return subprocess.CompletedProcess(list(args), 0, b"", b"")

    engine._run_with_input = fake_input
    r = asyncio.run(engine.send(_req("修复后重发", "阿潘")))
    assert r.ok
    assert "修复后重发" in writes
    assert any(c[1] == "key" and "ctrl+v" in c for c in runner.calls)
    # 走剪贴板时不该再逐字打中文
    assert not any(c[1] == "type" and "修复后重发" in " ".join(c) for c in runner.calls)


def test_chinese_falls_back_to_typing_without_xclip():
    """没有 xclip 时退回逐字打，且间隔要够大（>=40ms）。"""
    runner = FakeRunner()
    engine = X11Engine(runner=runner, display=":99", min_gap=0.0, open_delay=0.0)
    assert engine.type_delay >= 40
    r = asyncio.run(engine.send(_req("测试", "阿潘")))
    assert r.ok
    types = [c for c in runner.calls if c[1] == "type"]
    assert any("测试" in " ".join(c) for c in types)
