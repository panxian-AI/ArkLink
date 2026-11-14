"""「操作官方客户端」通道测试：结构体、热键、纯文本过滤、适配器行为。

不触碰真实窗口/剪贴板：Win32 调用全部走 dry-run 或注入的假 host。
"""

from __future__ import annotations

import asyncio
import ctypes
import sys

import pytest

from arklink.ntqq import host as H


def test_win32_struct_sizes():
    """SendInput 校验 cbSize，结构体大小错了会静默丢按键。"""
    if sys.maxsize > 2**32:
        assert ctypes.sizeof(H.INPUT) == 40
        assert ctypes.sizeof(H.KEYBDINPUT) == 24
    assert ctypes.sizeof(H.PROCESSENTRY32W) == 568


def test_parse_hotkey():
    assert H.parse_hotkey("^f") == ([H.VK_CONTROL], 0x46)
    assert H.parse_hotkey("^!q") == ([H.VK_CONTROL, H.VK_MENU], 0x51)
    assert H.parse_hotkey("enter") == ([], H.VK_RETURN)   # 具名键
    assert H.parse_hotkey("f") == ([], 0x46)             # 单字符键名
    assert H.parse_hotkey("#a") == ([H.VK_LWIN], 0x41)


def test_parse_hotkey_rejects_bad():
    for spec in ("", "^", "^ab", "^\\"):
        with pytest.raises(ValueError):
            H.parse_hotkey(spec)


def test_plain_text_of():
    text = [{"type": "reply", "data": {"id": "1"}}, {"type": "text", "data": {"text": "hi"}}]
    assert H.plain_text_of(text) == "hi"
    assert H.plain_text_of("直接字符串") == "直接字符串"
    assert H.plain_text_of([{"type": "at", "data": {"qq": "123"}},
                            {"type": "text", "data": {"text": " 在吗"}}]) == "@123 在吗"
    # 有非文本段 → 空串，调用方据此换通道
    assert H.plain_text_of([{"type": "image", "data": {"file": "x"}}]) == ""


def test_client_info_dict():
    info = H.ClientInfo(running=True, pid=1, hwnd=2, title="QQ", foreground=True)
    assert info.as_dict()["pid"] == 1 and info.as_dict()["foreground"] is True


def test_detect_without_client():
    info = H.ClientHost(process_names=("definitely-not-running.exe",)).detect()
    assert info.running is False


def test_dry_run_does_not_touch_keyboard():
    client = H.ClientHost(dry_run=True)
    assert client.send_text("hello") is True
    assert "[dry-run]" in client.last_action
    assert client.open_and_send("123", "hi") is True
    assert "peer=123" in client.last_action


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.file_calls: list[tuple[str, list[str], str]] = []
        self.last_action = ""
        self.fail = False

    def detect(self) -> H.ClientInfo:
        return H.ClientInfo(running=True, pid=1, hwnd=2, title="QQ")

    def open_and_send(self, peer: str, text: str) -> bool:
        self.calls.append((peer, text))
        self.last_action = f"sent {peer}"
        return not self.fail

    def open_and_send_files(self, peer: str, paths, text: str = "") -> bool:
        self.file_calls.append((peer, list(paths), text))
        self.last_action = f"sent {peer} +{len(list(paths))}"
        return not self.fail


def test_adapter_sends_text_through_client():
    async def run():
        fake = FakeHost()
        adapter = H.ClientHostAdapter(host=fake)  # type: ignore[arg-type]
        adapter.info = fake.detect()
        assert adapter.is_connected is True

        result = await adapter.call_api("send_group_msg", {
            "group_id": 20002,
            "message": [{"type": "text", "data": {"text": "上线了"}}],
        })
        assert fake.calls == [("20002", "上线了")]
        assert result["via"] == "client-host"

        # 私聊同理
        await adapter.call_api("send_private_msg", {
            "user_id": 30003, "message": [{"type": "text", "data": {"text": "私聊"}}],
        })
        assert fake.calls[-1] == ("30003", "私聊")
    asyncio.run(run())


def test_adapter_rejects_non_text_and_unknown_action():
    async def run():
        fake = FakeHost()
        adapter = H.ClientHostAdapter(host=fake)  # type: ignore[arg-type]
        adapter.info = fake.detect()

        # 附件：走 open_and_send_files，不再一律拒绝
        await adapter.call_api("send_group_msg", {
            "group_id": 1, "message": [{"type": "image", "data": {"file": "a.png"}}],
        })
        assert fake.file_calls and fake.file_calls[-1][1] == ["a.png"]

        with pytest.raises(Exception) as ei2:
            await adapter.call_api("set_group_kick", {"group_id": 1, "user_id": 2})
        assert getattr(ei2.value, "retcode", None) == 1404

        status = await adapter.call_api("get_status")
        assert status["online"] is True
    asyncio.run(run())


def test_adapter_failure_is_not_1404():
    async def run():
        fake = FakeHost()
        fake.fail = True
        adapter = H.ClientHostAdapter(host=fake)  # type: ignore[arg-type]
        adapter.info = fake.detect()
        with pytest.raises(Exception) as ei:
            await adapter.call_api("send_group_msg", {
                "group_id": 1, "message": [{"type": "text", "data": {"text": "x"}}],
            })
        # 不是「通道不支持」，而是本次失败：让业务感知，而不是静默换路
        assert getattr(ei.value, "retcode", None) not in (1404, None)
    asyncio.run(run())


def test_adapter_receive_is_off():
    adapter = H.ClientHostAdapter(host=FakeHost())  # type: ignore[arg-type]
    assert adapter.supports_receive is False

    async def run():
        assert await adapter.send_reply(object(), "x") is False
    asyncio.run(run())


# ---------------------------------------------------------------- 剪贴板附件

def test_build_dropfiles_layout():
    """CF_HDROP 的字节布局错了，接收方会静默忽略这次粘贴。"""
    import struct

    data = H.build_dropfiles(["C:/a.png", "D:/b.pdf"])
    assert struct.unpack("<I", data[:4])[0] == H.DROPFILES_SIZE
    # fWide 必须是 1：为 0 时对方按 ANSI 解析，中文路径全乱
    assert struct.unpack("<I", data[16:20])[0] == 1
    # 列表以双 null 收尾
    assert data.endswith(b"\x00\x00")
    assert H.parse_dropfiles(data) == ["C:/a.png", "D:/b.pdf"]


def test_build_dropfiles_handles_cjk_and_empty():
    data = H.build_dropfiles(["D:/报告 2026.pdf"])
    assert H.parse_dropfiles(data) == ["D:/报告 2026.pdf"]
    assert H.build_dropfiles([]) == b""
    assert H.build_dropfiles(["", ""]) == b""


def test_w32_set_clipboard_files_uses_hdrop(monkeypatch):
    seen = {}

    def fake(fmt, data):
        seen["fmt"], seen["data"] = fmt, data
        return True

    monkeypatch.setattr(H._W32, "_set_raw", fake)
    assert H._W32.set_clipboard_files(["C:/a.png"]) is True
    assert seen["fmt"] == H.CF_HDROP
    assert H.parse_dropfiles(seen["data"]) == ["C:/a.png"]


def test_dry_run_send_files():
    client = H.ClientHost(dry_run=True)
    assert client.send_files(["C:/a.png"], text="看图") is True
    assert "1 个" in client.last_action
    assert client.open_and_send_files("123", ["C:/a.png", "C:/b.pdf"]) is True
    assert "peer=123" in client.last_action


def test_send_files_pastes_text_before_files(monkeypatch):
    """剪贴板一次只装得下一样，所以顺序必须是先文本后文件。"""
    client = H.ClientHost(dry_run=False, attach_delay=0)
    calls: list[tuple] = []
    monkeypatch.setattr(H._W32, "rect", lambda hwnd: (0, 0, 1000, 800))
    monkeypatch.setattr(H._W32, "click", lambda x, y: calls.append(("click", x, y)))
    monkeypatch.setattr(H._W32, "chord", lambda keys: calls.append(("chord", tuple(keys))))
    monkeypatch.setattr(H._W32, "set_clipboard", lambda t: calls.append(("text", t)) or True)
    monkeypatch.setattr(
        H._W32, "set_clipboard_files", lambda p: calls.append(("files", list(p))) or True
    )
    assert client.send_files(["C:/a.png"], text="看图", hwnd=1) is True
    kinds = [c[0] for c in calls]
    assert kinds.index("text") < kinds.index("files")
    assert calls[-1] == ("chord", (H.VK_RETURN,))


def test_send_files_reports_clipboard_failure(monkeypatch):
    client = H.ClientHost(dry_run=False, attach_delay=0)
    monkeypatch.setattr(H._W32, "rect", lambda hwnd: (0, 0, 1000, 800))
    monkeypatch.setattr(H._W32, "click", lambda x, y: None)
    monkeypatch.setattr(H._W32, "set_clipboard_files", lambda p: False)
    assert client.send_files(["C:/a.png"], hwnd=1) is False
    assert "剪贴板写入失败" in client.last_action
