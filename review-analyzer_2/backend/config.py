"""Environment-driven configuration shared by every backend module."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(ROOT_DIR / ".env")

_PLACEHOLDER_PREFIXES = ("your_", "paste_", "changeme", "xxx")


class ConfigError(Exception):
    """A required API key is missing or still set to its placeholder value."""

    status_code = 503


def get_key(name: str) -> str | None:
    """Return a configured API key, or None if unset / still a placeholder."""
    value = (os.getenv(name) or "").strip().strip('"').strip("'")
    if not value or value.lower().startswith(_PLACEHOLDER_PREFIXES):
        return None
    return value


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
# A compact fallback chain keeps transient model issues from multiplying request time.
GEMINI_FALLBACK_MODELS = [
    m.strip() for m in (os.getenv("GEMINI_FALLBACK_MODELS") or "gemini-3.6-flash").split(",") if m.strip()
]
AMAZON_DOMAIN = (os.getenv("AMAZON_DOMAIN") or "amazon.in").strip()
CACHE_TTL_SECONDS = int(_get_float("CACHE_TTL_HOURS", 24) * 3600)

# Vercel's deployment filesystem is read-only. /tmp is writable but ephemeral,
# so this cache is best-effort and may reset between function instances.
_default_db_path = "/tmp/worthit-cache.sqlite3" if os.getenv("VERCEL") else "data/cache.sqlite3"
_db_path = Path(os.getenv("DB_PATH") or _default_db_path)
DB_PATH = _db_path if _db_path.is_absolute() else ROOT_DIR / _db_path
