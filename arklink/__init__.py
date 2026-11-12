"""ArkLink —— QQ bot 运行时。

三件事：
1. **bot 层**：`arklink.bot.Bot` / `arklink.client.Client` / `serve api`，
   上层写一个函数就能收消息、回消息；
2. **发送闸门**：账号 / 收件人 / 群聊三道白名单，任何入口都不绕过；
3. **协议与逆向**：`milky` / `satori` 适配，以及 QQ NT 原生协议的字节级工具（研究态）。

能力边界写在 `arklink/caps.py`，接口与文档都从它读，不各说一套。
"""

from __future__ import annotations

__version__ = "0.12.0"

from .base import LinkAdapter  # noqa: E402
from .client import ApiError, Client  # noqa: E402
from .registry import ADAPTERS, available, create  # noqa: E402

__all__ = [
    "LinkAdapter",
    "ADAPTERS",
    "available",
    "create",
    "Client",
    "ApiError",
    "__version__",
]
