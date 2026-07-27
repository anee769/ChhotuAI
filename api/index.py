"""Vercel serverless entry point.

Vercel routes every request to this module and expects an ASGI `app`. The
backend/ directory is added to sys.path because its modules import each other
by bare name ("import repo", "import clock"), the same way uvicorn loads them
locally.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from main import app  # noqa: E402  (path setup must run first)

__all__ = ["app"]
