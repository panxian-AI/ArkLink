"""发消息的「闸门」：谁能发、能发给谁。

发消息是这个项目里唯一会**改变服务端状态**的动作，也是唯一会被肉眼看到的动作。
所以它比收消息多一层硬约束，且这层约束写在代码里，不靠配置自觉：

* **默认什么都不允许**：`allowed_uins` 与 `allowed_peers` 出厂都是空。
  没配置就发不出去，提示里直接告诉你怎么配 —— 仓库里不写死任何人的号或备注，
  别人拿到代码不会误发到原作者账号上。
* **只允许配置过的账号**：`allowed_uins` 里没有的号，`send_*` 一律拒绝。
* **只允许发给白名单里的人**：`allowed_peers` 为空 = 全拒。匹配对象是
  备注 / 昵称 / uid，任一命中即可；同名多人视为不唯一，直接拒绝。
* **群聊默认关闭**：`allow_group=False`。要开必须同时给群号白名单。

拒绝不是抛异常糊过去，而是返回 `Decision(ok=False, code, reason)`，
让上层（OneBot / CLI）能原样告诉调用方「为什么没发出去」。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 出厂默认：**空**。空不是「不限」，而是「还没配置」——
#: 想发消息必须先 `arklink peers uin <你的号>` 和 `peers allow <备注>`。
#: 这样仓库里没有任何人的真实账号，别人 clone 下来也不会误发到原作者。
DEFAULT_UINS: tuple[str, ...] = ()
DEFAULT_PEERS: tuple[str, ...] = ()

#: 错误码：沿用 OneBot 的习惯，1400 参数错、1403 无权限、1404 不支持
CODE_BAD_REQUEST = 1400
CODE_FORBIDDEN = 1403
CODE_UNSUPPORTED = 1404
CODE_NOT_FOUND = 1404


@dataclass(frozen=True)
class Peer:
    """一个可以发消息的对象。"""

    chat_type: int          # 1 = 私聊, 2 = 群
    peer_uid: str
    uin: str = ""
    name: str = ""          # 备注优先
    nick: str = ""

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(x for x in (self.name, self.nick, self.uin, self.peer_uid) if x)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_type": self.chat_type,
            "peer_uid": self.peer_uid,
            "uin": self.uin,
            "name": self.name,
            "nick": self.nick,
        }


@dataclass
class Decision:
    ok: bool
    code: int = 0
    reason: str = ""
    peer: Peer | None = None

    def __bool__(self) -> bool:  # 方便 `if policy.check(...)`
        return self.ok


def peers_path() -> Path:
    """`~/.arklink/peers.json`：uid ↔ uin ↔ 备注 的本地映射。

    官方客户端里 uid 是一串 `u_xxx`，OneBot 侧习惯用 QQ 号，两者必须能对上。
    这个文件由 `arklink peers scan` 生成，也可以手写。
    """
    override = os.environ.get("ARKLINK_PEERS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".arklink" / "peers.json"


def load_peers(path: str | os.PathLike[str] | None = None) -> list[Peer]:
    target = Path(path).expanduser() if path else peers_path()
    if not target.exists():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("peers") if isinstance(raw, dict) else raw
    peers: list[Peer] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("peer_uid"):
            continue
        peers.append(
            Peer(
                chat_type=int(item.get("chat_type") or 1),
                peer_uid=str(item["peer_uid"]),
                uin=str(item.get("uin") or ""),
                name=str(item.get("name") or ""),
                nick=str(item.get("nick") or ""),
            )
        )
    return peers


def save_peers(peers: Iterable[Peer], path: str | os.PathLike[str] | None = None) -> Path:
    target = Path(path).expanduser() if path else peers_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "peers": [p.as_dict() for p in peers]}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


#: 策略文件里认这几个键，其余一律忽略（避免手改出花样把闸门搞失效）
POLICY_KEYS: tuple[str, ...] = (
    "allowed_uins",
    "allowed_peers",
    "allow_group",
    "allowed_groups",
    "max_length",
)


def policy_path() -> Path:
    """`~/.arklink/policy.json`：白名单的落盘位置。

    存在的意义是「脱离开发者也能改」：不用改 Python、不用重装，
    `arklink peers allow 505 --group` 写这个文件，下次启动就生效。
    环境变量 `ARKLINK_POLICY` 可整体换一份（测试和「多套配置」都用它）。
    """
    override = os.environ.get("ARKLINK_POLICY")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".arklink" / "policy.json"


def load_policy_file(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    target = Path(path).expanduser() if path else policy_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # 只认已知键；写错键名时宁可不生效，也不要让一个拼写错误变成「放开所有人」
    return {key: raw[key] for key in POLICY_KEYS if key in raw}


def save_policy_file(
    data: dict[str, Any], path: str | os.PathLike[str] | None = None
) -> Path:
    target = Path(path).expanduser() if path else policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"version": 1}
    payload.update({key: data[key] for key in POLICY_KEYS if key in data})
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # 只有本人可读：白名单本身不敏感，但没必要让别人改
        target.chmod(0o600)
    except OSError:
        pass
    return target


@dataclass
class SendPolicy:
    """发送白名单。出厂为空，谁都没配 → 谁都发不出去。"""

    allowed_uins: tuple[str, ...] = DEFAULT_UINS
    allowed_peers: tuple[str, ...] = DEFAULT_PEERS
    allow_group: bool = False
    allowed_groups: tuple[str, ...] = ()
    max_length: int = 3000
    peers: list[Peer] = field(default_factory=list)

    # ---------------- 构造 ----------------
    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None = None, *, load: bool = True) -> "SendPolicy":
        """默认值 → `policy.json` → 调用方传入的 cfg（越靠后优先级越高）。

        三层是刻意的：代码里的默认值就是产品的安全底线（空白名单、群聊关闭），
        文件层让用户能自己加人，cfg 层留给测试和一次性覆盖。
        """
        merged: dict[str, Any] = load_policy_file() if load else {}
        for key, value in dict(cfg or {}).items():
            if value is not None:
                merged[key] = value
        policy = cls(
            allowed_uins=_as_tuple(merged.get("allowed_uins", DEFAULT_UINS)),
            allowed_peers=_as_tuple(merged.get("allowed_peers", DEFAULT_PEERS)),
            allow_group=bool(merged.get("allow_group", False)),
            allowed_groups=_as_tuple(merged.get("allowed_groups", ())),
            max_length=int(merged.get("max_length", 3000)),
        )
        if load:
            policy.peers = load_peers(merged.get("peers_file"))
        return policy

    # ---------------- 落盘 ----------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_uins": list(self.allowed_uins),
            "allowed_peers": list(self.allowed_peers),
            "allow_group": self.allow_group,
            "allowed_groups": list(self.allowed_groups),
            "max_length": self.max_length,
        }

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        return save_policy_file(self.to_dict(), path)

    # ---------------- 改白名单 ----------------
    def allow(self, *names: str, group: bool = False) -> list[str]:
        """加进收件人白名单，返回真正新增的那几个（重复加不算）。"""
        added: list[str] = []
        for raw in names:
            name = str(raw or "").strip()
            if not name:
                continue
            if name not in self.allowed_peers:
                self.allowed_peers = (*self.allowed_peers, name)
                added.append(name)
            if group:
                self.allow_group = True
                if name not in self.allowed_groups:
                    self.allowed_groups = (*self.allowed_groups, name)
        return added

    def allow_uin(self, *uins: str) -> list[str]:
        """配置发送账号白名单。空参数不动；返回真正新增的号。"""
        added: list[str] = []
        for raw in uins:
            uin = str(raw or "").strip()
            if not uin:
                continue
            if uin not in self.allowed_uins:
                self.allowed_uins = (*self.allowed_uins, uin)
                added.append(uin)
        return added

    def deny(self, *names: str) -> list[str]:
        """从白名单里删掉，返回真正删掉的那几个。"""
        removed: list[str] = []
        for raw in names:
            name = str(raw or "").strip()
            if not name:
                continue
            if name in self.allowed_peers:
                self.allowed_peers = tuple(x for x in self.allowed_peers if x != name)
                removed.append(name)
            if name in self.allowed_groups:
                self.allowed_groups = tuple(x for x in self.allowed_groups if x != name)
        if not self.allowed_groups:
            self.allow_group = False
        return removed

    def reset(self) -> None:
        """恢复产品默认边界，同时把策略文件删掉（下次读到的就是默认值）。"""
        self.allowed_uins = DEFAULT_UINS
        self.allowed_peers = DEFAULT_PEERS
        self.allow_group = False
        self.allowed_groups = ()
        target = policy_path()
        if target.exists():
            target.unlink()

    # ---------------- 判定 ----------------
    def check_account(self, uin: str | int) -> Decision:
        text = str(uin or "").strip()
        if not text:
            return Decision(
                False,
                CODE_FORBIDDEN,
                "还没配置发送账号：先 `arklink peers uin <你的QQ号>`（或写进 policy.json 的 allowed_uins）",
            )
        if not self.allowed_uins:
            return Decision(
                False,
                CODE_FORBIDDEN,
                "发送账号白名单是空的（出厂默认）：先 `arklink peers uin <你的QQ号>`",
            )
        if text not in self.allowed_uins:
            allowed = "、".join(self.allowed_uins) or "（空）"
            return Decision(
                False,
                CODE_FORBIDDEN,
                f"当前登录账号 {text} 不在发送白名单里（只允许 {allowed}）",
            )
        return Decision(True)

    def resolve(self, target: str | int | Peer) -> Decision:
        """把「备注 / QQ号 / uid」解析成一个明确的 Peer。"""
        if isinstance(target, Peer):
            return self._check_peer(target)
        text = str(target or "").strip()
        if not text:
            return Decision(False, CODE_BAD_REQUEST, "没给收件人")
        if not self.allowed_peers:
            return Decision(
                False,
                CODE_FORBIDDEN,
                "收件人白名单是空的（出厂默认）：先 `arklink peers allow <备注或群名>`",
            )
        hits = [p for p in self.peers if text in p.aliases]
        if len(hits) > 1:
            same = {(p.chat_type, p.peer_uid) for p in hits}
            if len(same) > 1:
                names = "、".join(f"{p.name or p.nick or p.peer_uid}" for p in hits[:4])
                return Decision(False, CODE_FORBIDDEN, f"「{text}」匹配到多个对象（{names}），不唯一")
        if not hits:
            # 自举：白名单里点名的对象，即使还没扫过花名册也允许发。
            # 否则「只发白名单里的人」这条要求会被一个不存在的 peers.json 卡住，
            # 而 UI 通道本来就只需要一个能在搜索框里打出来的名字。
            if text in self.allowed_peers and text not in ("", "*"):
                # 名字同时出现在群白名单里，就按群会话处理；否则按私聊。
                # 这一点对协议通道很关键：chat_type 决定走哪个发送接口。
                chat = 2 if text in self.allowed_groups else 1
                return self._check_peer(Peer(chat_type=chat, peer_uid=text, name=text))
            return Decision(
                False,
                CODE_NOT_FOUND,
                f"本地映射里没有「{text}」，先 `arklink peers allow {text}` 或 `arklink roster --save-peers`",
            )
        return self._check_peer(hits[0])

    def _check_peer(self, peer: Peer) -> Decision:
        if not self.allowed_peers:
            return Decision(
                False,
                CODE_FORBIDDEN,
                "收件人白名单是空的（出厂默认）：先 `arklink peers allow <备注或群名>`",
            )
        if peer.chat_type == 2 and not self.allow_group:
            return Decision(False, CODE_FORBIDDEN, "群聊发送被策略关闭（allow_group=false）")
        if peer.chat_type == 2 and self.allowed_groups and peer.peer_uid not in self.allowed_groups:
            return Decision(False, CODE_FORBIDDEN, f"群 {peer.peer_uid} 不在群白名单里")
        allowed = set(self.allowed_peers)
        if not (set(peer.aliases) & allowed):
            return Decision(
                False,
                CODE_FORBIDDEN,
                f"「{peer.name or peer.nick or peer.peer_uid}」不在收件人白名单里（只允许 {'、'.join(self.allowed_peers)}）",
            )
        return Decision(True, peer=peer)

    def check_length(self, text: str) -> Decision:
        if len(text) > self.max_length:
            return Decision(False, CODE_BAD_REQUEST, f"单条消息 {len(text)} 字，超过上限 {self.max_length}")
        return Decision(True)

    def describe(self) -> str:
        names = "、".join(self.allowed_peers) or "未配置"
        uins = "、".join(self.allowed_uins) or "未配置"
        if not self.allow_group:
            group = "关闭"
        elif self.allowed_groups:
            group = "允许（" + "、".join(self.allowed_groups) + "）"
        else:
            group = "允许（不限群）"
        return f"账号 {uins} → 收件人 {names}，群聊{group}，已知联系人 {len(self.peers)} 个"


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(x.strip() for x in value.replace("，", ",").split(",") if x.strip())
    return tuple(str(x).strip() for x in value if str(x).strip())
