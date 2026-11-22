"""
app/api/dependencies.py
=======================
FastAPI dependency injection providers.
"""

from __future__ import annotations

from fastapi import Request
from app.config import Settings
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
    return request.app.state.settings
