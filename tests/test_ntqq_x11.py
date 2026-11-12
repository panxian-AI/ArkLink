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

    def __call__(self, args):
        cmd = list(args)
        self.calls.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        if cmd[1:3] == ["search", "--name"]:
            return subprocess.CompletedProcess(cmd, 0, f"{self.window}\n", "")
        if cmd[1] == "getwindowgeometry":
            out = f"WIDTH={self.width}\nHEIGHT={self.height}\n"
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
    assert verbs.count("click") == 3          # 搜索框、第一条结果、输入框
    assert verbs.count("type") == 2           # 收件人、正文
    assert "key" in verbs
    # 正文那一次 type 必须带 --clearmodifiers，且不碰剪贴板
    types = [c for c in runner.calls if c[1] == "type"]
    assert all("--clearmodifiers" in c for c in types)
    assert "你好" in types[-1]


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


def test_geometry_fallback_still_acts():
    runner = FakeRunner(width=0, height=0)
    engine = X11Engine(runner=runner, display=":99", min_gap=0.0, open_delay=0.0)
    r = asyncio.run(engine.send(_req()))
    assert r.ok


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
