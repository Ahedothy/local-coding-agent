"""Thin FastAPI transport layer for the local Agent Core."""

from .app import app, create_app

__all__ = ["app", "create_app"]
