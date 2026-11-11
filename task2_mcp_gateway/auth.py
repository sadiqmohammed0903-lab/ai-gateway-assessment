"""Bearer-token (JWT, HS256) authentication and role extraction."""

import time
from typing import Literal

import jwt

Role = Literal["admin", "viewer"]
VALID_ROLES: frozenset[str] = frozenset({"admin", "viewer"})
ALGORITHM = "HS256"


class AuthError(Exception):
    """Raised for any authentication failure. The message is safe to show to clients."""


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("Authorization must use the Bearer scheme")
    return token.strip()


def decode_role(token: str, secret: str) -> Role:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["exp"]})
    except jwt.ExpiredSignatureError:
        raise AuthError("Token expired") from None
    except jwt.PyJWTError:
        raise AuthError("Invalid token") from None
    role = claims.get("role")
    if not isinstance(role, str) or role not in VALID_ROLES:
        raise AuthError("Unknown role")
    return "admin" if role == "admin" else "viewer"


def authenticate(authorization: str | None, secret: str) -> Role:
    return decode_role(extract_bearer(authorization), secret)


def mint_token(role: str, secret: str, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {"role": role, "iat": now, "exp": now + ttl_seconds}, secret, algorithm=ALGORITHM
    )
