"""QQ NT 原生协议工具箱（研究态）。

    tea.py       TEA（16 轮）+ CBC/PCBC 链接 + 三种填充
    pb.py        protobuf 线格式的最小编解码（宽容解析）
    sso.py       SSO 封包头/尾布局
    qsign.py     本地签名服务客户端
    native.py    把上面几件串成「拼包 → 加密 → 发 → 收 → 解密 → 解包」的管线
    host.py      操作「已登录的官方客户端」（只发文本），最后一条通知腿
"""

from __future__ import annotations

from .host import ClientHost, ClientHostAdapter, ClientInfo
from .native import LoopbackTransport, NativeQQAdapter, SSOTransport
from .qsign import QSignClient, QSignError, Signature
from .sso import SSOPacket, cmd_name, iter_packets, register_cmd
from .tea import decrypt_block, encrypt_block, qq_tea_decrypt, qq_tea_encrypt

__all__ = [
    "ClientHost",
    "ClientHostAdapter",
    "ClientInfo",
    "NativeQQAdapter",
    "LoopbackTransport",
    "SSOTransport",
    "QSignClient",
    "QSignError",
    "Signature",
    "SSOPacket",
    "cmd_name",
    "iter_packets",
    "register_cmd",
    "encrypt_block",
    "decrypt_block",
    "qq_tea_encrypt",
    "qq_tea_decrypt",
]
