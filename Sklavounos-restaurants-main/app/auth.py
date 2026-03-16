from __future__ import annotations

import os
from fastapi import Request
from itsdangerous import URLSafeSerializer, BadSignature


def _serializer() -> URLSafeSerializer:
    secret = os.getenv("SECRET_KEY", "dev-secret-change-me")
    return URLSafeSerializer(secret_key=secret, salt="skl-auth")


def sign_session(data: dict) -> str:
    return _serializer().dumps(data)


def read_session(token: str) -> dict | None:
    try:
        return _serializer().loads(token)
    except BadSignature:
        return None


def get_admin_username(request: Request) -> str | None:
    token = request.cookies.get("admin_session")
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    return data.get("u")


def get_portal_customer(request: Request) -> str | None:
    token = request.cookies.get("portal_session")
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    return data.get("c")
