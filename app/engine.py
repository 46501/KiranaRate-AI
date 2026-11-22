"""
app/engine.py
=============
Re-exports WorkflowEngine from app.services.workflow_engine for backwards compatibility.
"""

from __future__ import annotations

from app.services.workflow_engine import WorkflowEngine

__all__ = ["WorkflowEngine"]
