"""跨仓库挂载测试：ArkBot 内核 + ArkLink 协议通道 + 假协议端。

这组测试是「新 bot 挂在框架上」的验收：
插件只写一次 `@self.on_command("echo")`，底层是 Milky 还是 Satori 完全无感。
没装 arkbot 时自动跳过（本包可以脱离框架单独测协议）。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

pytest.importorskip("arkbot")

from arkbot import Plugin  # noqa: E402
from arkbot.adapters import load_adapter  # noqa: E402
from arkbot.app import ArkBot  # noqa: E402
from arkbot.config import Config  # noqa: E402

from arklink.milky import MilkyAdapter  # noqa: E402
from arklink.satori import SatoriAdapter, _stable_id  # noqa: E402
from fakes.fake_milky import GROUP_ID, FakeMilky  # noqa: E402
from fakes.fake_satori import FakeSatori  # noqa: E402


class EchoPlugin(Plugin):
    name = "echo_probe"

    async def onLoad(self) -> None:
        @self.on_command("echo", "复读")
        async def echo(event, state):
            await event.reply(state.get("args", "?"))


def _config() -> Config:
    # 适配器由代码注入，这里只给一个不用的占位，避免去连真实端口
    return Config({
        "adapter": {"type": "onebot11", "url": "ws://127.0.0.1:1", "reconnect_interval": 0.2},
        "bot": {"log_level": "WARNING", "plugins_dir": ""},
        "plugins": {"echo_probe": {}},
    })


async def _wait(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def test_milky_channel_mounted_into_arkbot():
    async def run():
        fake = await FakeMilky().start()
        adapter = MilkyAdapter(
            base_url=fake.base_url,
            access_token=fake.token,
            event_url=fake.event_url,
            reconnect_interval=0.2,
        )
        app = ArkBot(config=_config(), adapter=adapter)
        app.plugin_manager.register_class(EchoPlugin)
        task = asyncio.create_task(app._main())
        try:
            assert await fake.wait_client(), "适配器没有连上 /event"
            await asyncio.sleep(0.2)
            await fake.push(FakeMilky.group_message("/echo 你好 Milky"))

            got = await _wait(lambda: any(c[0] == "send_group_message" for c in fake.calls))
            assert got, f"没有发出 send_group_message，实际调用: {fake.calls}"
            _, params = [c for c in fake.calls if c[0] == "send_group_message"][-1]
            assert params["group_id"] == GROUP_ID
            assert {"type": "text", "data": {"text": "你好 Milky"}} in params["message"]
            # 默认带引用段（reply.message_seq）
            assert params["message"][0]["type"] == "reply"
            assert app.bot.self_id == 10001
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await fake.stop()

    asyncio.run(run())


def test_satori_channel_mounted_into_arkbot():
    async def run():
        fake = await FakeSatori().start()
        adapter = SatoriAdapter(
            api_base=fake.api_base,
            token=fake.token,
            events_url=fake.events_url,
            user_id="10001",
            reconnect_interval=0.2,
        )
        app = ArkBot(config=_config(), adapter=adapter)
        app.plugin_manager.register_class(EchoPlugin)
        task = asyncio.create_task(app._main())
        try:
            assert await fake.wait_ready(), "Satori 客户端没有完成 IDENTIFY"
            await asyncio.sleep(0.2)
            await fake.push(FakeSatori.group_message("/echo 你好 Satori"))

            got = await _wait(lambda: any(c[0].endswith("message.create") for c in fake.calls))
            assert got, f"没有发出 message.create，实际调用: {fake.calls}"
            _, body = [c for c in fake.calls if c[0].endswith("message.create")][-1]
            assert body["channel_id"] == str(GROUP_ID)  # Satori 的 id 是字符串
            assert "你好 Satori" in body["content"]
            assert body["content"].startswith("<quote")  # 默认引用
            assert app.bot.self_id == _stable_id("10001")
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await fake.stop()

    asyncio.run(run())


def test_loader_dotted_path_mounts_third_party_adapter():
    adapter = load_adapter({
        "type": "arklink.milky:MilkyAdapter",
        "base_url": "http://127.0.0.1:3000",
        "access_token": "t",
    })
    assert isinstance(adapter, MilkyAdapter)
    assert adapter.name == "milky"
    assert adapter.is_connected is False


def test_loader_reports_unknown_type():
    with pytest.raises(ValueError) as ei:
        load_adapter({"type": "totally-unknown"})
    assert "entry point" in str(ei.value)
