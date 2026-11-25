"""
app/api/dependencies.py
=======================
FastAPI dependency injection providers:
- Storage backend provider
- WorkflowEngine provider
- Application Settings provider
- REST API & Export authentication enforcement
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, is_placeholder
from app.storage.base import BaseStore
from app.services.workflow_engine import WorkflowEngine


def get_store(request: Request) -> BaseStore:
    """Return the initialized storage backend from application state."""
    return request.app.state.store


def get_engine(request: Request) -> WorkflowEngine:
    """Return the initialized WorkflowEngine from application state."""
    return request.app.state.engine


def get_settings(request: Request) -> Settings:
    """Return the application Settings from application state."""
    return getattr(request.app.state, "settings", None) or Settings()


def require_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Enforce authentication for sensitive REST API endpoints and financial exports.

    Authorizes the request if:
    1. An active UI user session exists (request.session.get('authenticated_user')).
    2. A valid API key is passed via Authorization: Bearer <key> or X-API-Key: <key>.
    3. In mock development/test mode without credentials configured, allows mock guest.

    Raises HTTPException(401) if authentication is enabled and missing/invalid.
    """
    # 1. Check session auth (browser users)
    if hasattr(request, "session"):
        session_user = request.session.get("authenticated_user")
        if session_user:
            return str(session_user)

    # 2. Extract Bearer token or X-API-Key header
    auth_header = request.headers.get("Authorization", "").strip()
    api_key_header = request.headers.get("X-API-Key", "").strip()
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif api_key_header:
        token = api_key_header

    expected_key = getattr(settings, "api_key", "").strip()
    has_api_key = bool(expected_key and not is_placeholder(expected_key))

    # If an API key is configured on the server
    if has_api_key:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required: missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(token, expected_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return "api_client"

    # If in live mode (mock_whatsapp is False) and no valid session/key:
    if not settings.mock_whatsapp:
        if token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API_KEY is not configured on server",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # In mock development/test mode without API key configured:
    # If a token was provided anyway, validate it against expected_key if present
    if token and expected_key:
        if not hmac.compare_digest(token, expected_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

    return "mock_guest"
