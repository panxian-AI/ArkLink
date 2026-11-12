"""Protobuf 线格式的最小实现。

QQ NT 的业务包体（nt_msg.*、trpc.*）都是 protobuf，逆向时最常用的两件事是
「把一段 hex 拆成字段」和「拼一个最小请求」。完整 protobuf 运行时太重，
这里只覆盖线格式：varint、length-delimited、fixed32/64。

`decode` 是**宽容**的：遇到截断或未知 wire type 就停下并返回已解析部分，
抓包时常有半个包，不应该抛异常。
"""

from __future__ import annotations

import struct
from typing import Any

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


# ---------------------------------------------------------------- 编码


def varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def uint_field(field: int, value: int) -> bytes:
    return tag(field, WIRE_VARINT) + varint(int(value))


def bytes_field(field: int, value: bytes) -> bytes:
    return tag(field, WIRE_LEN) + varint(len(value)) + value


def string_field(field: int, value: str) -> bytes:
    return bytes_field(field, value.encode("utf-8"))


def message_field(field: int, value: bytes) -> bytes:
    """嵌套消息和 bytes 在线格式上完全一样，语义区分靠 schema。"""
    return bytes_field(field, value)


def fixed32_field(field: int, value: int) -> bytes:
    return tag(field, WIRE_32BIT) + struct.pack("<I", value & 0xFFFFFFFF)


def fixed64_field(field: int, value: int) -> bytes:
    return tag(field, WIRE_64BIT) + struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF)


# ---------------------------------------------------------------- 解码


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint 截断")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def decode(buf: bytes) -> dict[int, list[Any]]:
    """把 protobuf 字节拆成 {字段号: [值...]}。

    值类型：varint → int，length-delimited → bytes，
    fixed32/64 → int；未知 wire type 停止解析（保留已解析部分）。
    """
    fields: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(buf):
        try:
            key, pos = _read_varint(buf, pos)
        except ValueError:
            break
        field = key >> 3
        wire = key & 0x07
        try:
            if wire == WIRE_VARINT:
                value, pos = _read_varint(buf, pos)
            elif wire == WIRE_LEN:
                length, pos = _read_varint(buf, pos)
                if pos + length > len(buf):
                    break
                value = buf[pos:pos + length]
                pos += length
            elif wire == WIRE_32BIT:
                if pos + 4 > len(buf):
                    break
                value = struct.unpack("<I", buf[pos:pos + 4])[0]
                pos += 4
            elif wire == WIRE_64BIT:
                if pos + 8 > len(buf):
                    break
                value = struct.unpack("<Q", buf[pos:pos + 8])[0]
                pos += 8
            else:
                break
        except ValueError:
            break
        fields.setdefault(field, []).append(value)
    return fields


def as_text(value: Any) -> str:
    """把 length-delimited 值当 UTF-8 文本读（逆向时最常用）。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def dump(buf: bytes, indent: int = 0) -> str:
    """把包体打印成人类可读的树，抓包分析用。"""
    lines: list[str] = []
    pad = "  " * indent
    for field, values in decode(buf).items():
        for value in values:
            if isinstance(value, bytes):
                text = value.decode("utf-8", "replace")
                printable = text.isprintable() and text.strip() != ""
                nested = decode(value) if value else {}
                if printable and len(text) <= 64:
                    lines.append(f"{pad}{field}: {text!r}")
                elif nested and len(value) > 2:
                    lines.append(f"{pad}{field}: {{")
                    lines.append(dump(value, indent + 1))
                    lines.append(f"{pad}}}")
                else:
                    lines.append(f"{pad}{field}: 0x{value.hex()}")
            else:
                lines.append(f"{pad}{field}: {value}")
    return "\n".join(lines)
