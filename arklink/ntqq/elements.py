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
| `image`      | 2                        | 需要先上传，v1 直接拒绝 |
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: QQ 侧 elementType（和 wrapper.node 的 `MsgElement.elementType` 对齐）
ELEMENT_TEXT = 1
ELEMENT_PICTURE = 2
ELEMENT_FACE = 6

_CQ = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")
_CQ_ARG = re.compile(r",([a-zA-Z_]+)=([^,\]]*)")


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
            out.append(Element("image", file=str(data.get("file") or data.get("url") or "")))
        elif kind == "reply":
            out.append(Element("reply", id=str(data.get("id", ""))))
        elif kind in ("json", "xml", "markdown", "music", "video", "record", "file"):
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
    """给日志/预览用的纯文本（@ 变成 @QQ号）。"""
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
        else:
            parts.append(f"[{el.kind}]")
    return "".join(parts).strip()


def supported(elements: Iterable[Element]) -> tuple[list[Element], list[Element]]:
    """拆成（能发的, 不能发的）。v1 只支持 text/at/face。"""
    ok: list[Element] = []
    bad: list[Element] = []
    for el in elements:
        (ok if el.kind in ("text", "at", "face") else bad).append(el)
    return ok, bad


def validate(elements: list[Element], *, max_length: int = 3000) -> None:
    """发之前先过一遍；有问题直接抛 `MessageError`。"""
    if not elements:
        raise MessageError("消息为空（全是空文本？）")
    text = plain_text(elements)
    if not text:
        raise MessageError("消息没有可发的文字内容")
    if len(text) > max_length:
        raise MessageError(f"消息 {len(text)} 字，超过上限 {max_length}")
    ok, bad = supported(elements)
    if bad:
        kinds = "、".join(sorted({el.kind for el in bad}))
        raise MessageError(f"暂时不支持这些消息段：{kinds}")
