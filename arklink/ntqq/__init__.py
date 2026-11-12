"""QQ NT 原生协议工具箱。

    tea.py       TEA（16 轮）+ CBC/PCBC 链接 + 三种填充
    pb.py        protobuf 线格式的最小编解码（宽容解析）
    sso.py       古典 SSO 封包头/尾布局（数字 cmd）
    ntframe.py   NT 帧（protocol 12/13，服务名字符串）—— 真正发 OIDB 的那层
    ecdh.py      secp192k1 密钥协商（wtlogin 的 _shareKey 来源）
    code2d.py    wtlogin.trans_emp：0x31 申请 / 0x12 轮询 / 0x13 换票据
    session.py   扫码登录状态机 + keystore 落盘
    roster.py    好友 / 群列表（OIDB 0xfd4 / 0xfe5）
    push.py      推送 MsgPush → OneBot 11 事件
    native.py    把上面几件串起来：登录 → 只读 → 收推送
    host.py      操作「已登录的官方客户端」（只发文本），最后一条通知腿
"""

from __future__ import annotations

from . import code2d, ecdh, ntframe, pb, push, roster, tea
from .host import ClientHost, ClientHostAdapter, ClientInfo
from .native import LoopbackTransport, NativeQQAdapter, SSOTransport, TcpTransport
from .qsign import QSignClient, QSignError, Signature
from .session import Keystore, QrLoginSession
from .sso import SSOPacket, cmd_name, iter_packets, register_cmd
from .tea import decrypt_block, encrypt_block, qq_tea_decrypt, qq_tea_encrypt

__all__ = [
    "ClientHost",
    "ClientHostAdapter",
    "ClientInfo",
    "Keystore",
    "LoopbackTransport",
    "NativeQQAdapter",
    "QrLoginSession",
    "SSOTransport",
    "TcpTransport",
    "QSignClient",
    "QSignError",
    "Signature",
    "SSOPacket",
    "cmd_name",
    "code2d",
    "ecdh",
    "iter_packets",
    "ntframe",
    "pb",
    "push",
    "register_cmd",
    "roster",
    "tea",
    "encrypt_block",
    "decrypt_block",
    "qq_tea_encrypt",
    "qq_tea_decrypt",
]
