"""Bot 层：给上层用的最小、明确、可预期的接口。

ArkLink 的定位不是「一堆逆向工具」，而是**一个 QQ bot 运行时**：

    上层（你的业务 / LLM agent / 定时任务）
        │  只写一个函数：收到消息 → 返回要回的话
        ▼
    arklink.bot.Bot        ← 你只需要认识这三个东西
        │  send / reply / stream
        ▼
    ArkLink API（同机或虚拟机里常驻）

为什么要有这一层，而不是让上层直接拼 HTTP：

* **回调式**：`@bot.on_message` 一个装饰器，返回字符串就自动回复，
  不用关心 SSE 续传、seq 去重、群聊/私聊的 chat_type 差异。
* **不许炸**：handler 抛异常只记日志、不中断收消息——比「一条坏消息把 bot 弄死」
  更符合现实。
* **防自激**：自己发出去的消息不会再回灌给 handler（否则 echo bot 会自己咬自己）。
* **可换后端**：本地进程、虚拟机里的 API、远程机器，只换一个 base_url。

三条命令就能看到东西在动：

    arklink bot new mybot.py      # 生成带注释的模板
    arklink bot run mybot.py      # 跑起来
    arklink bot check             # 出问题先跑这个，它会告诉你下一步
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable, Iterable

from .client import ApiError, Client

log = logging.getLogger("arklink")

#: handler 的返回值含义
Handler = Callable[..., Any]


class Bot:
    """把「收消息 → 你的函数 → 回复」串起来。"""

    def __init__(
        self,
        client: Client | None = None,
        *,
        prefix: str = "",
        reply_prefix: str = "",
        ignore_self: bool = True,
        log_replies: bool = True,
    ) -> None:
        self.client = client or Client.from_env()
        #: 只处理以它开头的消息；空 = 全处理
        self.prefix = prefix
        #: 回复时加在前面的字符串（例如「[bot] 」）
        self.reply_prefix = reply_prefix
        self.ignore_self = ignore_self
        self.log_replies = log_replies
        self._handlers: list[Handler] = []
        self._self_uins: set[str] = set()
        self.stats = {"seen": 0, "handled": 0, "replied": 0, "errors": 0, "skipped": 0}

    # ------------------------------------------------------------ 注册
    def on_message(self, func: Handler | None = None):
        """装饰器：`@bot.on_message`。函数可以只收一个 dict，也可以收 (bot, msg)。"""
        if func is None:
            def decorator(fn: Handler) -> Handler:
                self._handlers.append(fn)
                return fn

            return decorator
        self._handlers.append(func)
        return func

    def handler(self, func: Handler) -> Handler:
        return self.on_message(func)

    @property
    def handlers(self) -> list[Handler]:
        return list(self._handlers)

    # ------------------------------------------------------------ 动作
    def send(self, to: str, text: str, *, group: bool = False, dry_run: bool = False) -> dict[str, Any]:
        return self.client.send(to, text, group=group, dry_run=dry_run)

    def reply(self, msg: dict[str, Any], text: str, *, dry_run: bool = False) -> dict[str, Any]:
        """回到消息来的那个会话：私聊回私聊，群聊回群聊。"""
        if not text:
            return {"ok": False, "msg": "空回复，没发出去"}
        target = str(msg.get("peer_uid") or msg.get("sender_uid") or "")
        if not target:
            return {"ok": False, "msg": "这条消息没有 peer_uid/sender_uid，无法回复"}
        is_group = int(msg.get("chat_type") or 1) == 2
        body = self.reply_prefix + text if self.reply_prefix else text
        try:
            result = self.client.send(target, body, group=is_group, dry_run=dry_run)
        except ApiError as exc:
            # 被白名单拒绝是常态（比如群没开、对方不在名单里），不能让它把 bot 弄挂；
            # 但要在日志里说清楚为什么，别让上层以为「发出去了」。
            self.stats["errors"] += 1
            log.warning("回复 %s 失败：%s", target, exc.msg)
            return {"ok": False, "code": exc.code, "msg": exc.msg}
        self.stats["replied"] += 1
        if self.log_replies:
            log.info("回复 %s（%s）：%s", target, "群" if is_group else "私聊", body[:60])
        return result

    # ------------------------------------------------------------ 运行
    def _call(self, func: Handler, msg: dict[str, Any]) -> Any:
        """兼容三种签名：f(msg) / f(bot, msg) / f(msg, bot)。"""
        try:
            params = len(inspect.signature(func).parameters)
        except (TypeError, ValueError):
            params = 1
        if params >= 2:
            return func(self, msg)
        return func(msg)

    def handle(self, msg: dict[str, Any]) -> list[Any]:
        """处理一条消息（测试可直接调它，不用起网络）。"""
        self.stats["seen"] += 1
        text = str(msg.get("text") or "")
        if self.prefix and not text.startswith(self.prefix):
            self.stats["skipped"] += 1
            return []
        if self.ignore_self and str(msg.get("sender_uid") or "") in self._self_uins:
            self.stats["skipped"] += 1
            return []

        results: list[Any] = []
        for func in self._handlers:
            try:
                out = self._call(func, msg)
            except Exception:  # 一条消息处理失败不能拖垮整个 bot
                self.stats["errors"] += 1
                log.exception("handler %s 处理消息失败", getattr(func, "__name__", func))
                continue
            self.stats["handled"] += 1
            if isinstance(out, str) and out.strip():
                results.append(self.reply(msg, out))
            elif isinstance(out, dict):
                results.append(out)
        return results

    def learn_self(self) -> set[str]:
        """问服务端「我配了哪些号」，用于防自激。失败不影响运行。"""
        try:
            status = self.client.status()
            uins = (status.get("limits") or {}).get("allowed_uins") or []
            self._self_uins = {str(u) for u in uins if u}
        except ApiError as exc:
            log.warning("拿不到发送账号（%s），自激保护先按空处理", exc)
        return self._self_uins

    def run(self, since: int | None = None, *, reconnect: bool = True, max_messages: int = 0) -> None:
        """阻塞运行：收一条、处理一条。Ctrl+C 退出。

        `since=None`（默认）从**当前最新**开始，不回放历史——否则第一次跑起来
        会把缓冲里的旧消息全部「回复」一遍。要看历史从 `since=0` 开始。
        """
        if since is None:
            try:
                since = int(self.client.messages(limit=1).get("last_seq") or 0)
            except ApiError:
                since = 0
        self.learn_self()
        log.info("bot 已启动：%d 个 handler，地址 %s", len(self._handlers), self.client.base_url)
        if not self._handlers:
            log.warning("还没注册任何 handler，收到消息也不会回复（先写 @bot.on_message）")
        count = 0
        try:
            for msg in self.client.stream(since=since, reconnect=reconnect):
                self.handle(msg)
                count += 1
                if max_messages and count >= max_messages:
                    break
        except KeyboardInterrupt:
            log.info("已停止（处理 %d 条）", count)

    # ------------------------------------------------------------ 诊断
    def check(self) -> list[tuple[str, bool, str]]:
        """返回 (项目, 是否通过, 说明)。给 `arklink bot check` 用，每行都带下一步。"""
        rows: list[tuple[str, bool, str]] = []
        try:
            health = self.client.health()
        except ApiError as exc:
            return [("服务", False, f"连不上 {self.client.base_url}：{exc.msg}。先起服务：arklink serve api")]
        rows.append(("服务", True, f"{self.client.base_url}（v{health.get('version')}）"))

        try:
            status = self.client.status()
        except ApiError as exc:
            rows.append(("鉴权", False, f"{exc.msg}。把 token 写进 ~/.arklink/api.json 或用 ARKLINK_TOKEN"))
            return rows
        rows.append(("鉴权", True, "token 有效"))

        limits = status.get("limits") or {}
        uins = limits.get("allowed_uins") or []
        peers = limits.get("allowed_peers") or []
        rows.append(("发送账号", bool(uins),
                     f"已配置 {', '.join(uins)}" if uins else "未配置：arklink peers uin <你的QQ号>"))
        rows.append(("收件人", bool(peers),
                     f"已配置 {', '.join(peers)}" if peers else "未配置：arklink peers allow <备注>"))
        engine = status.get("engine")
        available = status.get("available")
        rows.append(("发送引擎", bool(available),
                     f"{engine} 可用" if available else f"{engine} 不可用：确认 QQ 已登录（无头环境用 --engine x11）"))
        return rows


def load_bot(path: str, *, client: Client | None = None) -> Bot:
    """从 .py 文件加载 bot：认 `setup(bot)` 或模块级 `bot` 变量。"""
    import importlib.util
    import os

    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到 {path}")
    name = "arklink_userbot_" + os.path.splitext(os.path.basename(path))[0].replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.abspath(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"加载不了 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    setup = getattr(module, "setup", None)
    if callable(setup):
        bot = getattr(module, "bot", None) or Bot(client)
        setup(bot)
        return bot
    bot = getattr(module, "bot", None)
    if isinstance(bot, Bot):
        if client is not None:
            bot.client = client
        return bot
    raise AttributeError(
        f"{path} 里既没有 setup(bot) 也没有模块级 bot = Bot()；"
        "用 `arklink bot new 文件名.py` 生成一个正确的模板"
    )


TEMPLATE = '''"""最小的 QQ bot：一条消息进来，回一条回去。

跑起来:
    arklink bot run {name}            # 需要先有服务：arklink serve api

改这里就行，别的都不用动。
"""

from arklink.bot import Bot

bot = Bot()


@bot.on_message
def echo(msg):
    """收到任何消息，回一句。返回字符串 = 自动回复；返回 None = 不回。"""
    return f"收到：{{msg['text']}}"


# 想按关键词分流，就再写一个函数：
# @bot.on_message
# def help_cmd(msg):
#     if msg["text"].strip() in ("/help", "帮助"):
#         return "可用命令：/help"
'''
