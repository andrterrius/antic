from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def _normalize(key: str) -> str:
    k2 = key.strip().replace(" ", "")
    if len(k2) % 8 != 0:
        k2 += "=" * (8 - len(k2) % 8)
    return k2


def _prefix0(h: str) -> str:
    if len(h) < 6:
        h = "0" * (6 - len(h)) + h
    return h


def get_hotp_token(secret: str, intervals_no: int) -> str:
    key = base64.b32decode(_normalize(secret), True)
    msg = struct.pack(">Q", intervals_no)
    digest = bytearray(hmac.new(key, msg, hashlib.sha1).digest())
    offset = digest[19] & 15
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return _prefix0(str(code))


def get_totp_token(secret: str) -> str:
    return get_hotp_token(secret, intervals_no=int(time.time()) // 30)


def totp_seconds_remaining() -> int:
    return 30 - (int(time.time()) % 30)
