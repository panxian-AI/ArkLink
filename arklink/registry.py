"""适配器注册表：把「配置里的 type」映射到工厂函数。

ArkBot 侧通过 entry point `arkbot.adapters` 发现本包，也支持直接写点分路径
`type = "arklink.milky:MilkyAdapter"`。这个注册表则给独立使用时一个统一入口。
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

#: type 名 → "module:attr"
ADAPTERS: dict[str, str] = {
    "milky": "arklink.milky:create",
    "satori": "arklink.satori:create",
    "ntqq-native": "arklink.ntqq.native:create",
}


def _resolve(spec: str) -> Callable[..., Any]:
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    target = getattr(module, attr)
    return target


def create(name: str, **cfg: Any) -> Any:
    """按名字造一个适配器。"""
    spec = ADAPTERS.get(name)
    if spec is None:
        raise KeyError(f"未注册的协议 {name!r}，可选: {', '.join(sorted(ADAPTERS))}")
    return _resolve(spec)(**cfg)


def available() -> dict[str, str]:
    return dict(ADAPTERS)
