"""TEA 原语测试：标准向量 + 往返 + 三种填充。"""

from __future__ import annotations

import pytest

from arklink.ntqq import tea


def test_standard_tea_vector_32_rounds():
    """教科书 TEA 的公开测试向量：key/plain 全 0 → 0x41EA3A0A 94BAA940。"""
    v0, v1 = tea.encrypt_block(0, 0, b"\x00" * 16, rounds=32)
    assert (v0, v1) == (0x41EA3A0A, 0x94BAA940)
    assert tea.decrypt_block(v0, v1, b"\x00" * 16, rounds=32) == (0, 0)


@pytest.mark.parametrize("rounds", [16, 32])
@pytest.mark.parametrize("text", [b"", b"a", b"hello", b"x" * 7, b"y" * 8, b"z" * 33, bytes(range(64))])
def test_block_roundtrip(rounds: int, text: bytes):
    v0, v1 = tea.encrypt_block(int.from_bytes(text[:4].ljust(4, b"\0"), "big"),
                               int.from_bytes(text[4:8].ljust(4, b"\0"), "big"),
                               b"0123456789abcdef", rounds=rounds)
    d0, d1 = tea.decrypt_block(v0, v1, b"0123456789abcdef", rounds=rounds)
    assert (d0, d1) == (int.from_bytes(text[:4].ljust(4, b"\0"), "big"),
                        int.from_bytes(text[4:8].ljust(4, b"\0"), "big"))


@pytest.mark.parametrize("scheme", ["qq", "pkcs7", "zero"])
@pytest.mark.parametrize("mode", ["cbc", "pcbc"])
@pytest.mark.parametrize("text", [b"", b"a", b"hello world", b"x" * 100, "中文消息".encode()])
def test_qq_tea_roundtrip(scheme: str, mode: str, text: bytes):
    key = b"0123456789abcdef"
    cipher = tea.qq_tea_encrypt(text, key, mode=mode, scheme=scheme)
    assert len(cipher) % 8 == 0
    assert tea.qq_tea_decrypt(cipher, key, mode=mode, scheme=scheme) == text


def test_cipher_is_deterministic_and_block_aligned():
    key = b"0123456789abcdef"
    a = tea.qq_tea_encrypt(b"same input", key)
    b = tea.qq_tea_encrypt(b"same input", key)
    assert a == b and len(a) % 8 == 0


def test_wrong_key_does_not_raise():
    key = b"0123456789abcdef"
    cipher = tea.qq_tea_encrypt(b"secret payload", key)
    assert tea.qq_tea_decrypt(cipher, b"ffffffffffffffff") != b"secret payload"


def test_bad_cipher_length_raises():
    with pytest.raises(ValueError):
        tea.qq_tea_decrypt(b"1234567", b"0123456789abcdef")
