"""OneBot 消息段 → QQ `MsgElement` 的中间表示。

发消息的输入有四种写法，都要归一到同一份「元素列表」：

```
"你好"                                       纯字符串
[{"type":"text","data":{"text":"你好"}}]      OneBot 11 数组
CQ 码："[CQ:at,qq=123] 你好"
AstrBot/LangBot 之类框架直接递进来的 dict
```

归一是为了**先验后发**：长度、空消息、不支持的段，都在这一层拦下来，
不要等到进了 QQ 进程才发现塞不进去（那时候只剩崩溃和静默两种结果）。

元素类型和 QQ 侧的对应关系：

| Element.kind | wrapper 里的 elementType | 备注 |
|--------------|--------------------------|------|
| `text`       | 1                        | 最常用，纯文本 |
| `at`         | 1（带 atType/atUid）      | 文本元素 + @ 属性 |
| `face`       | 6                        | 小黄脸，按 id |
| `reply`      | 1（前面拼引用）           | 原生引用要 msgId，先降级成文本 |
| `image`      | 2                        | 需要先上传；UI 通道用剪贴板粘贴 |
| `file` / `video` / `record` | 待定         | 同上，按本地文件粘贴过去 |

关于附件：`image` / `file` / `video` / `record` 的 `file` 字段可以是**本地路径**，
也可以是 URL。两者走的路完全不同——

* 本地路径：剪贴板 `CF_HDROP` 直接粘进输入框，不需要注入、不需要上传接口；
* URL / base64：必须先把字节下载下来落盘，否则粘不进去。

所以这里只做「认出来 + 分好类」，落盘与能力判断留给上层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: QQ 侧 elementType（和 wrapper.node 的 `MsgElement.elementType` 对齐）
ELEMENT_TEXT = 1
ELEMENT_PICTURE = 2
ELEMENT_FACE = 6

#: 需要伴随文件才能发的段。它们的共同点是 `file` 字段指向一个真实文件。
ATTACHMENT_KINDS = ("image", "file", "video", "record")

#: 一份附件最多带几个：粘贴多个文件时客户端会排队上传，
#: 太多既慢又容易触发风控，所以在这里设个闸。
MAX_ATTACHMENTS = 10

_CQ = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")
_CQ_ARG = re.compile(r",([a-zA-Z_]+)=([^,\]]*)")

#: 认得出是「远程资源」而不是本地路径的前缀
_REMOTE_PREFIXES = ("http://", "https://", "base64://", "data:", "file://")


def looks_remote(value: str) -> bool:
    """这个 `file` 字段是不是远程/内联资源（而不是本地路径）。"""
    return str(value or "").strip().lower().startswith(_REMOTE_PREFIXES)


def file_of(data: dict[str, Any]) -> str:
    """从段数据里取文件位置：file → path → url，OneBot 实现们各有各的写法。"""
    for key in ("file", "path", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


#: 兼容旧名（模块内部原先叫 `_file_of`）
_file_of = file_of


@dataclass
class Element:
    kind: str
    text: str = ""
    qq: str = ""
    id: str = ""
    file: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def element_type(self) -> int:
        return {
            "text": ELEMENT_TEXT,
            "at": ELEMENT_TEXT,
            "reply": ELEMENT_TEXT,
            "face": ELEMENT_FACE,
            "image": ELEMENT_PICTURE,
        }.get(self.kind, ELEMENT_TEXT)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "element_type": self.element_type}
        for key in ("text", "qq", "id", "file"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


class MessageError(ValueError):
    """消息本身不合法（空、超长、段类型不支持）。"""


def parse_message(message: Any) -> list[Element]:
    """把四种写法统一成 `list[Element]`。"""
    if message is None:
        raise MessageError("消息为空")
    if isinstance(message, str):
        return _parse_cq(message) if "[CQ:" in message else _text(message)
    if isinstance(message, dict):
        if "type" in message:
            return _segments([message])
        for key in ("message", "msg", "content"):
            if key in message:
                return parse_message(message[key])
        raise MessageError(f"看不懂的消息字典：{sorted(message)[:6]}")
    if isinstance(message, (list, tuple)):
        if all(isinstance(x, str) for x in message):  # 多段字符串
            return _text("".join(message))
        return _segments(list(message))
    raise MessageError(f"不支持的消息类型：{type(message).__name__}")


def _text(text: str) -> list[Element]:
    if not text:
        raise MessageError("消息为空")
    return [Element("text", text=text)]


def _parse_cq(text: str) -> list[Element]:
    """CQ 码 → 元素。只认 text/at/face，其余原样留在文本里。"""
    out: list[Element] = []
    pos = 0
    for match in _CQ.finditer(text):
        if match.start() > pos:
            out.append(Element("text", text=text[pos : match.start()]))
        kind, args_text = match.group(1), match.group(2)
        args = {k: v for k, v in _CQ_ARG.findall(args_text or "")}
        if kind == "at":
            out.append(Element("at", qq=args.get("qq", "")))
        elif kind == "face":
            out.append(Element("face", id=args.get("id", "")))
        elif kind in ATTACHMENT_KINDS:
            out.append(Element(kind, file=_file_of(args), raw=dict(args)))
        else:  # 不认识的 CQ 码保留原文，至少不丢字
            out.append(Element("text", text=match.group(0)))
        pos = match.end()
    if pos < len(text):
        out.append(Element("text", text=text[pos:]))
    return _merge_text(out)


def _segments(items: list[Any]) -> list[Element]:
    out: list[Element] = []
    for item in items:
        if isinstance(item, str):
            out.append(Element("text", text=item))
            continue
        if not isinstance(item, dict):
            raise MessageError(f"消息段不是字典：{item!r}")
        kind = str(item.get("type") or item.get("kind") or "").strip()
        data = item.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if kind == "text":
            out.append(Element("text", text=str(data.get("text", ""))))
        elif kind == "at":
            out.append(Element("at", qq=str(data.get("qq", ""))))
        elif kind == "face":
            out.append(Element("face", id=str(data.get("id", ""))))
        elif kind == "image":
            out.append(Element("image", file=_file_of(data), raw=data))
        elif kind == "reply":
            out.append(Element("reply", id=str(data.get("id", ""))))
        elif kind in ("file", "video", "record"):
            out.append(Element(kind, file=_file_of(data), raw=data))
        elif kind in ("json", "xml", "markdown", "music"):
            out.append(Element(kind, raw=data))
        elif kind == "":
            raise MessageError("消息段缺少 type")
        else:
            out.append(Element(kind, raw=data))
    return _merge_text(out)


def _merge_text(items: list[Element]) -> list[Element]:
    """相邻文本合并 —— QQ 侧元素越少越省事，配额也更友好。"""
    merged: list[Element] = []
    for item in items:
        if item.kind == "text" and not item.text:
            continue
        if item.kind == "text" and merged and merged[-1].kind == "text":
            merged[-1].text += item.text
        else:
            merged.append(item)
    return merged


def plain_text(elements: Iterable[Element]) -> str:
    """给日志/预览用的纯文本（@ 变成 @QQ号，附件变成占位符）。"""
    parts: list[str] = []
    for el in elements:
        if el.kind == "text":
            parts.append(el.text)
        elif el.kind == "at":
            parts.append("" if el.qq == "all" else f"@{el.qq}")
        elif el.kind == "face":
            parts.append(f"[表情{el.id}]")
        elif el.kind == "reply":
            parts.append("")
        elif el.kind == "image":
            parts.append("[图片]")
        elif el.kind == "file":
            parts.append("[文件]")
        elif el.kind == "video":
            parts.append("[视频]")
        elif el.kind == "record":
            parts.append("[语音]")
        else:
            parts.append(f"[{el.kind}]")
    return "".join(parts).strip()


def text_length(elements: Iterable[Element]) -> int:
    """只数文字，不数附件占位符——否则长度上限会被 `[图片]` 这类字样吃掉。"""
    return sum(len(el.text) for el in elements if el.kind == "text")


def attachments(elements: Iterable[Element]) -> list[Element]:
    """取出所有需要伴随文件的段。"""
    return [el for el in elements if el.kind in ATTACHMENT_KINDS]


def split_attachments(elements: Iterable[Element]) -> tuple[list[str], list[Element]]:
    """把附件拆成（能直接粘贴的本地路径, 需要先落盘的远程段）。

    远程段不做自动下载：超时、重定向、大小上限、落盘到哪儿、失败了重试几次，
    每一个都得有明确的答案，塞进发送路径只会让「为什么发不出去」更难查。
    所以这里只负责认出来，由上层回一句「先把文件下到本地，再传路径」。
    """
    paths: list[str] = []
    remote: list[Element] = []
    for el in attachments(elements):
        target = (el.file or "").strip()
        if not target:
            remote.append(el)
        elif looks_remote(target):
            remote.append(el)
        elif target not in paths:
            paths.append(target)
    return paths, remote


def supported(
    elements: Iterable[Element],
    *,
    kinds: Iterable[str] = ("text", "at", "face"),
) -> tuple[list[Element], list[Element]]:
    """按通道能发的段类型拆成（能发的, 不能发的）。"""
    allow = set(kinds)
    ok: list[Element] = []
    bad: list[Element] = []
    for el in elements:
        (ok if el.kind in allow else bad).append(el)
    return ok, bad


#: 段类型全集：认不出的一律当错误，而不是硬塞成文本发出去
KNOWN_KINDS = frozenset(
    ("text", "at", "face", "reply", "json", "xml", "markdown", "music", *ATTACHMENT_KINDS)
)


def validate(elements: list[Element], *, max_length: int = 3000) -> None:
    """发之前先过一遍**形状**：空消息、未知段、超长。

    这里刻意不判断「这个通道能不能发附件」——那是 `caps` 的事。
    两件事混在一层，会出现「换个通道就能过校验」的隐式行为，
    调用方根本没法从错误里看出该改通道还是该改消息。
    """
    if not elements:
        raise MessageError("消息为空（全是空文本？）")
    unknown = sorted({el.kind for el in elements if el.kind not in KNOWN_KINDS})
    if unknown:
        raise MessageError(f"不认识这些消息段：{'、'.join(unknown)}")
    length = text_length(elements)
    if length > max_length:
        raise MessageError(f"消息 {length} 字，超过上限 {max_length}")
    has_content = any(
        (el.kind == "text" and bool(el.text.strip())) or el.kind != "text" for el in elements
    )
    if not has_content:
        raise MessageError("消息没有可发的内容（文本全是空白？）")
    count = len(attachments(elements))
    if count > MAX_ATTACHMENTS:
        raise MessageError(f"一条消息最多带 {MAX_ATTACHMENTS} 个附件，收到 {count} 个")
