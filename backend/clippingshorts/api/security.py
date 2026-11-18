"""Authentication and authorization for the API.

* Sign-in with the admin password (full control) or the optional viewer password (read only). The session is an
  HMAC-signed, expiring, HTTP-only cookie; nothing secret is stored in the browser.
* Automation can use `Authorization: Bearer <CLIPPINGSHORTS_API_TOKEN>` (admin).
* Every state-changing request from a browser must carry the X-ClippingShorts header, which other websites cannot
  send cross-origin: together with SameSite=Lax cookies this blocks cross-site request forgery.
* Sign-in attempts are rate limited per client address.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import Depends, HTTPException, Request, Response

from .. import redis_bus as rb
from ..config import settings

COOKIE = "cs_session"
CSRF_HEADER = "x-clippingshorts"


def _sign(data: bytes) -> str:
    return hmac.new(settings.secret_key.encode(), data, hashlib.sha256).hexdigest()


def make_token(role: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"r": role, "exp": time.time() + settings.session_hours * 3600})
                                       .encode()).decode()
    return f"{payload}.{_sign(payload.encode())}"


def read_token(token: str) -> str | None:
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(payload.encode())):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data.get("r") if data.get("r") in ("admin", "viewer") else None


def client_ip(request: Request) -> str:
    # the frontend container is the only caller; it forwards the browser address
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "?")


def check_login_rate(request: Request) -> None:
    key = rb.k("ratelimit", "login", client_ip(request))
    n = rb.r().incr(key)
    if n == 1:
        rb.r().expire(key, 900)
    if n > 10:
        raise HTTPException(429, "Too many sign-in attempts. Wait 15 minutes.")


def login(password: str, response: Response) -> str:
    role = None
    if settings.admin_password and hmac.compare_digest(password.encode(), settings.admin_password.encode()):
        role = "admin"
    elif settings.viewer_password and hmac.compare_digest(password.encode(), settings.viewer_password.encode()):
        role = "viewer"
    if role is None:
        raise HTTPException(401, "Wrong password.")
    response.set_cookie(COOKIE, make_token(role), max_age=settings.session_hours * 3600, httponly=True,
                        samesite="lax", secure=settings.cookie_secure, path="/")
    return role


def role_of(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and settings.api_token:
        if hmac.compare_digest(auth[7:].strip().encode(), settings.api_token.encode()):
            return "admin"
    tok = request.cookies.get(COOKIE)
    return read_token(tok) if tok else None


def current_role(request: Request) -> str:
    role = role_of(request)
    if role is None:
        raise HTTPException(401, "Sign in first.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.headers.get("authorization"):
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "Missing X-ClippingShorts header.")
    return role


def viewer(role: str = Depends(current_role)) -> str:
    return role


def admin(role: str = Depends(current_role)) -> str:
    if role != "admin":
        raise HTTPException(403, "This account can only view.")
    return role
