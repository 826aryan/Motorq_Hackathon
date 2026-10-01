"""JWT per request (SPEC §7): the token carries lender_id, and every query is filtered to that lender.

Demo login: username = a lender's short name (alpha / beta / gamma), password = DEMO_PASSWORD from .env.
"""
import hmac
import os
import time

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

ALGORITHM = "HS256"
TOKEN_TTL_S = 12 * 3600
_bearer = HTTPBearer(auto_error=False)


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 16:
        raise RuntimeError("JWT_SECRET must be set (see .env.example)")
    return secret


def check_password(password: str) -> bool:
    expected = os.environ.get("DEMO_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(password.encode(), expected.encode())


def issue_token(lender_id: int, lender_name: str) -> str:
    now = int(time.time())
    return jwt.encode({"lender_id": lender_id, "lender": lender_name, "iat": now, "exp": now + TOKEN_TTL_S},
                      _secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc


def current_lender(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> int:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return int(decode_token(creds.credentials)["lender_id"])
