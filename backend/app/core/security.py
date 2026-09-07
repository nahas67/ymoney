"""Security primitives.

- Password hashing: PBKDF2-HMAC-SHA256 (stdlib; no fragile third-party deps).
- JWT access/refresh tokens via PyJWT.
- Secret encryption for platform tokens using AES-GCM when `cryptography`
  is unavailable falls back to Fernet-less HMAC-wrapped XOR... we require
  `cryptography` explicitly instead of inventing weak crypto.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import UTC

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

PBKDF2_ITERATIONS = 600_000


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def _jwt_encode(payload: dict, expires_delta_seconds: int) -> str:
    to_encode = dict(payload)
    from datetime import datetime, timedelta

    exp = datetime.now(UTC) + timedelta(seconds=expires_delta_seconds)
    to_encode["exp"] = exp
    return jwt.encode(to_encode, settings.secret_key, algorithm="HS256")


def create_access_token(user_id: str, extra: dict | None = None) -> str:
    payload = {"sub": user_id, "type": "access"}
    if extra:
        payload.update(extra)
    return _jwt_encode(payload, settings.access_token_expire_minutes * 60)


def decode_access_token(token: str) -> dict | None:
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        if data.get("type") != "access":
            return None
        return data
    except jwt.PyJWTError:
        return None


def generate_refresh_token() -> tuple[str, str]:
    """Returns (plaintext_token, sha256_hash_to_store)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256((settings.secret_key + raw).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Secret encryption at rest (AES-256-GCM)
# ---------------------------------------------------------------------------


def _aes_key() -> bytes:
    return hashlib.sha256(("enc:" + settings.secret_key).encode()).digest()


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    key = _aes_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return "v1:" + base64.b64encode(nonce + ct).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    if not ciphertext.startswith("v1:"):
        raise ValueError("unknown ciphertext version")
    blob = base64.b64decode(ciphertext[3:])
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(_aes_key()).decrypt(nonce, ct, None).decode()
