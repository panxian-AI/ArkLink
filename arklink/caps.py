"""能力矩阵：同类 bot 能干的事，这里逐条说清楚「现在能不能做、为什么」。

为什么要这张表，而不是「先全都返回成功」：

* 上层拿到 1404 时最想知道的是**缺什么**。一句「不支持」等于没说；
  写明「当前是 UI 通道，只能打字；要发图片需要进程内直发通道」，
  调用方就知道该换通道还是该改需求。
* 这张表是**唯一事实来源**：文档、`arklink bot check`、HTTP `/api/v1/capabilities`
  都从这里读，避免三处各说一套。

通道能力差异（实测结论）：

| 通道 | 能做什么 | 不能做什么 |
|---|---|---|
| `host` / `x11`（操作官方客户端 UI） | 发文本、按备注找会话 | 图片/文件/@/回复/撤回/群管理 |
| `pipe`（进程内直发，DLL 就绪后） | 全部 | — |
| `dry-run` | 演练全部接口 | 不真发 |
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 通道 → 是否支持「结构化消息」（图片/@/回复/撤回/群管理）。
#: 现在为空是**实测结论**：DLL 的发送实现还没落地，pipe 通道也发不了；
#: 等 `arklink.send` 通了，把 "pipe" 加进来，这张表和文档会一起变绿。
RICH_CHANNELS: set[str] = set()

#: 只有 UI 通道时，这些能力统一给这个提示
NEED_PIPE = "需要进程内直发通道（DLL 的 arklink.send）；当前通道只能发文本"


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    ok: bool
    reason: str = ""
    need: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "ok": self.ok, "reason": self.reason, "need": self.need}


def _items(channel: str) -> list[Capability]:
    rich = channel in RICH_CHANNELS
    dry = channel == "dry-run"
    rows = [
        Capability("send.text", "发文本（私聊/群聊）", True),
        Capability("send.multi", "一次发多个对象 / 多条", True),
        Capability("receive.message", "收消息（总线 + SSE）", True,
                   "来源取决于采集器：hook 通道就绪后自动进来；也可用 /api/v1/inbound 推入"),
        Capability("policy", "发送白名单（谁能发、发给谁）", True),
        Capability("image", "发图片", rich, "" if rich else "UI 通道打不了图片", "" if rich else NEED_PIPE),
        Capability("face", "发表情", rich, "" if rich else "UI 通道打不了表情", "" if rich else NEED_PIPE),
        Capability("at", "@ 某人", rich, "" if rich else "UI 通道只能打出「@名字」文字，不是真 @", "" if rich else NEED_PIPE),
        Capability("reply", "引用回复", rich, "" if rich else "UI 通道没有引用接口", "" if rich else NEED_PIPE),
        Capability("recall", "撤回消息", rich, "" if rich else "UI 通道做不到按消息 id 撤回", "" if rich else NEED_PIPE),
        Capability("history", "拉历史消息", rich,
                   "" if rich else "本地消息库是加密的（file is not a database），UI 通道也拿不到历史",
                   "" if rich else NEED_PIPE),
        Capability("roster", "好友 / 群列表", True,
                   "来自 peers.json（arklink roster --save-peers 生成）；为空时只返回空表"),
        Capability("group.mute", "群禁言", rich, "" if rich else "需要群管理接口", "" if rich else NEED_PIPE),
        Capability("group.kick", "群踢人", rich, "" if rich else "需要群管理接口", "" if rich else NEED_PIPE),
    ]
    # 演练模式不影响「能不能做」，只影响「真不真发」——这两件事混在一起
    # 会让调用方误以为换个通道就能发图片，所以这里不做特殊处理。
    _ = dry
    return rows


def capabilities(channel: str = "") -> dict[str, Any]:
    items = _items((channel or "").strip().lower())
    return {
        "channel": channel,
        "items": [c.as_dict() for c in items],
        "ok": [c.key for c in items if c.ok],
        "missing": [c.key for c in items if not c.ok],
    }


def require(channel: str, key: str) -> Capability | None:
    """取一条能力；不支持时返回那条（带 reason/need），由调用方转成 1404。"""
    for item in _items((channel or "").strip().lower()):
        if item.key == key:
            return None if item.ok else item
    return Capability(key, key, False, f"未知能力 {key}", "")
