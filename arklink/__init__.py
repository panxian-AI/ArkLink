"""ArkLink —— ArkBot 的协议外挂层。

三件事：
1. `milky`      把 Milky 协议端（NapCat-milky / Lagrange / Acidify …）接进来；
2. `satori`     把 Koishi / Chronocat 这类 Satori SDK 接进来；
3. `ntqq`       QQ NT 原生协议的字节级工具箱（研究态，不登录）。

前两个是可用通道，第三个是逆向脚手架：抓包 → 解包 → 对拍。
"""

from __future__ import annotations

__version__ = "0.10.0"

from .base import LinkAdapter  # noqa: E402
from .registry import ADAPTERS, available, create  # noqa: E402

__all__ = ["LinkAdapter", "ADAPTERS", "available", "create", "__version__"]
