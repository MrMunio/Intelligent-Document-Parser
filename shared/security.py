"""Password hashing, JWT handling and API key helpers."""
import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from shared.config import settings

API_KEY_PREFIX = "idp_"
TOKEN_ACCESS = "access"
TOKEN_REFRESH = "refresh"


class TokenError(Exception):
    pass


# ---- Passwords ---------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def password_policy_error(password: str) -> str | None:
    """Returns a human readable reason when the password is too weak, else None."""
    if len(password) < 10:
        return "Password must be at least 10 characters long."
    if len(password) > 128:
        return "Password must be at most 128 characters long."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain at least one letter and one number."
    return None


def generate_temp_password() -> str:
    return secrets.token_urlsafe(12) + "9a"


# ---- JWT ---------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_token(user_id: str, token_version: int, token_type: str) -> tuple[str, int]:
    """Returns (token, lifetime_seconds)."""
    if token_type == TOKEN_ACCESS:
        ttl = timedelta(minutes=settings.jwt_access_ttl_minutes)
    else:
        ttl = timedelta(days=settings.jwt_refresh_ttl_days)
    now = _now()
    payload = {
        "sub": user_id,
        "ver": token_version,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), int(ttl.total_seconds())


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("Token is invalid.") from exc
    if payload.get("typ") != expected_type:
        raise TokenError("Token type is not valid for this operation.")
    return payload


# ---- API keys ----------------------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    """Returns (plaintext_key, display_prefix, sha256_hash). Only the hash is stored."""
    public = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    key = f"{API_KEY_PREFIX}{public}_{secret}"
    return key, f"{API_KEY_PREFIX}{public}", hash_api_key(key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def looks_like_api_key(value: str) -> bool:
    return value.startswith(API_KEY_PREFIX)
