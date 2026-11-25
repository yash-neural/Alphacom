"""Opaque keyset cursors.

A cursor holds the sort key of the last item returned plus a hash of the filters it
was issued for, signed so it can't be edited. Reusing it with different filters, or
after an hour, is a 400 rather than a silently wrong page.
"""

import base64
import hashlib
import hmac
import json
import time

from app.problems import ApiProblem

CURSOR_TTL_S = 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def filter_fingerprint(filters: dict) -> str:
    return hashlib.sha256(json.dumps(filters, sort_keys=True, default=str).encode()).hexdigest()[:16]


def encode_cursor(secret: str, last_key: list, fingerprint: str) -> str:
    body = _b64(json.dumps({"k": last_key, "f": fingerprint, "exp": int(time.time()) + CURSOR_TTL_S}).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()[:16])
    return f"{body}.{sig}"


def decode_cursor(secret: str, cursor: str, fingerprint: str) -> list:
    def bad(why: str) -> ApiProblem:
        return ApiProblem(400, "invalid-cursor", "Invalid cursor", why)

    try:
        body, sig = cursor.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()[:16])
        if not hmac.compare_digest(sig, expected):
            raise bad("cursor signature does not match")
        data = json.loads(_unb64(body))
    except ApiProblem:
        raise
    except Exception:
        raise bad("cursor is malformed")
    if data["exp"] < time.time():
        raise bad("cursor has expired; start again without a cursor")
    if data["f"] != fingerprint:
        raise bad("cursor was issued for different filters or sort")
    return data["k"]
