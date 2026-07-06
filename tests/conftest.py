"""Shared test utilities."""

_XOR_KEY = b"ACERT"


def xor_crypt(data: bytes) -> bytes:
    """XOR cipher with key ACERT — same call encrypts and decrypts."""
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(data))
