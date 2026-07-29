"""Native Vercel entry point for the Chhotu.ai FastAPI application.

Vercel now discovers FastAPI applications directly from a root ``app.py``.
Keeping the original request path intact is important because the authentication
middleware distinguishes public routes such as ``/`` and ``/api/auth/*`` from
tenant-protected API routes.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from backend.main import app  # noqa: E402

__all__ = ["app"]
