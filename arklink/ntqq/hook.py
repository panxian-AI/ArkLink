"""`ntqq-hook` 通道：先自己登录 QQ，程序检测到进程后注入并接管消息流。

和另外两条 QQ 路线的区别：

| 通道 | 登录 | 依赖 | 收到消息 | 被踢风险 |
|------|------|------|----------|----------|
| `ntqq-native` | 扫码 | 自己实现协议 | 推送订阅 | 低（但协议易变） |
| `ntqq-client` | 用官方客户端 | 键鼠操作 | ❌ | 无（不碰进程） |
| **`ntqq-hook`** | **官方客户端** | **注入 + 管道** | ✅ | 中（模块可见性） |

工作流程：

```
找 QQ 主进程 → 有 hook 管道？ ── 有 ──→ 直接连
                    └─ 没有 ──→ 注入 hook DLL → 等管道 → 连
                                                        ↓
                          recv 管道推包 → push.to_onebot → 统一事件
```

设计上把「注入」和「hook 实现」拆开：`dll_path` 指向哪个 DLL，就用哪个后端。
默认会找仓库里 `hookdll/build/` 的自研产物，找不到再退回本机已装的现成 hook
（它的管道协议是公开的字节格式，我们只做互操作，不依赖它的运行时）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

from ..base import LinkAdapter, api_error
from . import hookproc, push
from .hookpipe import HookClient, PipeError
from .hookproto import CMD_MSG_PUSH, Frame

logger = logging.getLogger("arklink.ntqq.hook")


class HookQQAdapter(LinkAdapter):
    """注入式 QQ 通道。

    bot.toml：

        [[adapters]]
        type = "ntqq-hook"
        auto_inject = true                     # 检测到 QQ 就注入
        dll_path = ""                          # 空 = 自动找（自研 → 现成）
        read_only = true                       # 只收不发
        wait_process = 0                       # >0 则等 QQ 启动，单位秒
    """

    name = "ntqq-hook"
    max_message_length = 3000
    supports_receive = True

    def __init__(
        self,
        exe_names: Iterable[str] | None = None,
        dll_path: str = "",
        auto_inject: bool = True,
        inject_method: str = "loadlibrary",
        pipe_prefix: str = "",
        read_only: bool = True,
        wait_process: float = 0.0,
        poll_interval: float = 3.0,
        connect_timeout: float = 12.0,
        client_factory: Any = None,
        scanner: hookproc.ProcessScanner | None = None,
        injector: hookproc.Injector | None = None,
    ) -> None:
        super().__init__()
        self.exe_names = tuple(exe_names or hookproc.DEFAULT_EXE_NAMES)
        self.dll_path = dll_path
        self.auto_inject = auto_inject
        self.inject_method = inject_method
        self.pipe_prefix = pipe_prefix
        self.read_only = read_only
        self.wait_process = wait_process
        self.poll_interval = poll_interval
        self.connect_timeout = connect_timeout
        self._client_factory = client_factory
        self.scanner = scanner or hookproc.ProcessScanner()
        self.injector = injector or hookproc.Injector(self.scanner)

        self.client: HookClient | None = None
        self.pid = 0
        self.injected = False
        self.injection: hookproc.InjectionResult | None = None
        self.last_error = ""
        self._stats = {"packets": 0, "dropped": 0, "since": 0.0}

    # ---------------- 健康度 ----------------
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and self.client is not None and self.client.logged_in

    @property
    def uin(self) -> str:
        return self.client.uin if self.client else ""

    def status(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "injected": self.injected,
            "method": self.injection.method if self.injection else "",
            "logged_in": bool(self.client and self.client.logged_in),
            "uin": self.uin,
            "packets": self._stats["packets"],
            "dropped": self._stats["dropped"],
            "last_error": self.last_error,
        }

    # ---------------- 生命周期 ----------------
    async def connect(self) -> None:
        """主循环：进程在 → 连/注入 → 掉线重来。"""
        deadline = time.monotonic() + self.wait_process if self.wait_process > 0 else 0
        while self._running:
            try:
                await self._ensure_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning("hook 会话失败：%s", exc)
            await self._teardown()
            if deadline and time.monotonic() > deadline:
                break
            await asyncio.sleep(self.poll_interval)

    async def _ensure_session(self) -> None:
        processes = await asyncio.to_thread(hookproc.find_processes, self.exe_names, self.scanner)
        target = hookproc.pick_main(processes)
        if target is None:
            self.last_error = "没有找到 QQ 进程（先手动登录 QQ 客户端）"
            logger.info(self.last_error)
            return
        self.pid = target.pid
        prefix = self.pipe_prefix

        # 1) 已有管道 → 直接连（可能来自上一次注入，或别的客户端）
        if not prefix:
            for candidate in hookproc.PIPE_PREFIXES:
                if self.scanner.pipe_name(target.pid, "control").startswith(candidate):
                    prefix = candidate
                    break
        prefix = prefix or "mojo"

        if not self.scanner.pipe_name(target.pid, "control"):
            if not self.auto_inject:
                self.last_error = "没有 hook 管道，且 auto_inject=false"
                logger.info(self.last_error)
                return
            await self._inject(target.pid)

        # 2) 连管道（注入后 DLL 起管道有延迟，这里轮询等）
        client = self._make_client(target.pid, prefix)
        await self._connect_with_retry(client)
        self.client = client
        self._connected.set()
        logger.info("hook 通道已就绪：pid=%d uin=%s", target.pid, client.uin or "?")

        # 3) 守着连接，直到断开或停止
        while self._running:
            await asyncio.sleep(1.0)
            if self.client is None:
                break

    async def _inject(self, pid: int) -> None:
        dll = self.dll_path or hookproc.default_hook_dll()
        if not dll:
            raise hookproc.HookError(
                "找不到可用的 hook DLL：请先编译 hookdll/，或用 dll_path / ARKLINK_HOOK_DLL 指定"
            )
        logger.info("正在注入 %s → pid=%d", dll, pid)
        result = await asyncio.to_thread(self.injector.inject, pid, dll, self.inject_method)
        self.injection = result
        self.injected = True
        if result.note:
            logger.warning("注入提示：%s", result.note)

    async def _connect_with_retry(self, client: HookClient) -> None:
        deadline = time.monotonic() + self.connect_timeout
        delay = 0.2
        while True:
            try:
                await client.connect(timeout=max(1.0, deadline - time.monotonic()))
                return
            except (PipeError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise
                logger.debug("管道还没起来（%s），重试中", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)

    def _make_client(self, pid: int, prefix: str) -> HookClient:
        factory = self._client_factory
        client = HookClient(pid, prefix=prefix, pipe_factory=factory)
        client.on_packet = self._on_packet
        client.on_login_state = self._on_login_state
        client.on_capability = lambda frame: logger.debug("hook 能力帧：%s", frame.status)
        client.on_close = self._on_close
        return client

    async def _teardown(self) -> None:
        self._connected.clear()
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def disconnect(self) -> None:
        self._running = False
        await self._teardown()

    # ---------------- 事件 ----------------
    def _on_login_state(self, frame: Frame) -> None:
        if frame.logged_in:
            self._connected.set()
        else:
            self._connected.clear()

    def _on_close(self, kind: str) -> None:
        self.last_error = f"{kind} 管道断开"
        self._connected.clear()

    def _on_packet(self, frame: Frame) -> None:
        """recv 管道的包 → OneBot 事件。

        这里在事件循环线程里被调用（读循环就是 loop 里的任务），所以可以直接
        `create_task`；不要在回调里 await，会挡住后续帧。
        """
        self._stats["packets"] += 1
        if frame.cmd != CMD_MSG_PUSH:
            logger.debug("忽略非消息包 cmd=%s len=%d", frame.cmd, len(frame.body))
            return
        try:
            event = push.to_onebot(frame.body, self_id=int(self.uin) if self.uin.isdigit() else 0)
        except Exception:
            self._stats["dropped"] += 1
            logger.exception("推送解析失败（%dB）", len(frame.body))
            return
        if event is None:
            self._stats["dropped"] += 1
            return
        try:
            asyncio.get_running_loop().create_task(self._emit_dict(event))
        except RuntimeError:  # 没有运行中的 loop（收尾阶段）
            logger.debug("事件循环已停止，丢弃一帧")

    # ---------------- API ----------------
    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        if action in ("get_login_info", "get_self_info"):
            uin = self.uin
            if not uin:
                raise api_error(action, 1404, "hook 通道还没有登录态")
            return {"user_id": int(uin) if uin.isdigit() else 0, "nickname": "", "online": True}
        if action == "get_status":
            return {"online": self.is_connected, "good": self.is_connected, **self.status()}
        if action == "hook_status":
            return self.status()
        if action in ("send_msg", "send_message", "send_private_msg", "send_group_msg"):
            if self.read_only:
                raise api_error(action, 1404, "hook 通道处于只读模式（read_only=true），发送走其他通道")
            raise api_error(action, 1404, "hook 通道的发送链路还没接（只做收消息）")
        if action in ("get_friend_list", "get_group_list"):
            # hook 只给消息流，花名册走 ntqq-native 的 OIDB 查询
            raise api_error(action, 1404, f"hook 通道不提供 {action}，请用 ntqq-native")
        raise api_error(action, 1404, f"hook 通道不支持 {action}")


def create(**cfg: Any) -> HookQQAdapter:
    """entry point 工厂：`ntqq-hook = "arklink.ntqq.hook:create"`。"""
    return HookQQAdapter(**cfg)
