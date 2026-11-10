"""Passwords and signed session cookies."""

import hashlib
import hmac
import json
import os
import base64

from .config import SECRET_KEY


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or os.urandom(16)
    return salt, hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def check_password(password: str, salt: bytes, expected: bytes) -> bool:
    return hmac.compare_digest(hash_password(password, salt)[1], expected)


def make_session(user: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"user": user}).encode()).decode()
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_session(cookie: str) -> dict | None:
    payload, _, sig = cookie.rpartition(".")
    good = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    return json.loads(base64.urlsafe_b64decode(payload))
