"""WS-Security UsernameToken with PasswordDigest, as TRACES NT expects.

PasswordDigest = Base64( SHA-1( nonce_bytes + created + authentication_key ) )
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Credentials


@dataclass(frozen=True)
class WsseToken:
    username: str
    password_digest: str
    nonce_b64: str
    created: str
    expires: str


def format_timestamp(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_timestamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def password_digest(nonce: bytes, created: str, password: str) -> str:
    sha = hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8"))
    return base64.b64encode(sha.digest()).decode("ascii")


def make_token(
    credentials: Credentials,
    ttl_seconds: int = 60,
    now: datetime | None = None,
    nonce: bytes | None = None,
) -> WsseToken:
    """Build a fresh token. Call once per request: nonces must not be reused."""
    now = now or datetime.now(timezone.utc)
    nonce = nonce if nonce is not None else os.urandom(16)
    created = format_timestamp(now)
    expires = format_timestamp(now + timedelta(seconds=ttl_seconds))
    return WsseToken(
        username=credentials.username,
        password_digest=password_digest(nonce, created, credentials.auth_key),
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        created=created,
        expires=expires,
    )
