"""独立 OneBot 11 服务端（标准库，无第三方依赖）。

用法一（CLI，最省事）：

    python -m arklink serve onebot --type ntqq-hook --port 6700 --token t

用法二（代码）：

    from arklink import registry
    from arklink.onebot import OneBot11Server

    adapter = registry.create("ntqq-hook")
    server = OneBot11Server([adapter], port=6700)
    await server.serve_forever()
"""

from .server import OneBot11Server, WS_PATHS, run, serve

__all__ = ["OneBot11Server", "WS_PATHS", "serve", "run"]
