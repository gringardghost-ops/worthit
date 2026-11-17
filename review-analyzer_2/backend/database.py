"""Result cache (24h TTL by default) and recent searches.

Two interchangeable backends:
  * Upstash Redis over its REST API, used automatically when its env vars are present
    (required on Vercel, where the filesystem is ephemeral).
  * SQLite, used for local development.
Every cache operation is best-effort: a cache failure must never break an analysis.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from .config import CACHE_TTL_SECONDS, DB_PATH

log = logging.getLogger("worthit.database")

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    key            TEXT PRIMARY KEY,
    query          TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    entity_name    TEXT,
    worth_it_score REAL,
    result_json    TEXT NOT NULL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_cache_created ON analysis_cache (created_at DESC);
"""
_sqlite_ready = False
_RECENT_KEY = "wi:recent"


def make_cache_key(query: str, entity_type: str) -> str:
    """Stable key: case and whitespace-insensitive, scoped by entity type."""
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    return f"{entity_type}:{normalized}"


# --------------------------------------------------------------------------- backend selection
def _redis_config() -> tuple[str, str] | None:
    url = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL") or "").strip().rstrip("/")
    token = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN") or "").strip()
    return (url, token) if url and token else None


def _sqlite_path() -> Path:
    # Vercel's filesystem is read-only except /tmp (ephemeral, per instance).
    return Path("/tmp/worthit-cache.sqlite3") if os.getenv("VERCEL") else DB_PATH


# --------------------------------------------------------------------------- Redis (Upstash REST)
def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]


def _result_key(key: str) -> str:
    return f"wi:c:{_hash(key)}"


def _meta_key(key: str) -> str:
    return f"wi:m:{_hash(key)}"


async def _redis_pipeline(commands: list[list[str]]) -> list[Any]:
    """Run several Redis commands in one HTTP round trip."""
    cfg = _redis_config()
    if not cfg:
        raise RuntimeError("Redis is not configured")
    url, token = cfg
    async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
        resp = await client.post(f"{url}/pipeline", json=commands, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    out: list[Any] = []
    for item in resp.json():
        if isinstance(item, dict):
            if item.get("error"):
                raise RuntimeError(str(item["error"]))
            out.append(item.get("result"))
        else:
            out.append(item)
    return out


# --------------------------------------------------------------------------- SQLite
async def _ensure_sqlite() -> None:
    global _sqlite_ready
    if _sqlite_ready:
        return
    path = _sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_SQLITE_SCHEMA)
        await db.commit()
    _sqlite_ready = True


# --------------------------------------------------------------------------- public API
async def init_db() -> None:
    """Prepare the backend. Also done lazily, since serverless hosts may skip startup events."""
    if _redis_config():
        return
    try:
        await _ensure_sqlite()
    except Exception as exc:  # noqa: BLE001
        log.warning("SQLite cache unavailable: %s", exc)


async def get_cached(key: str) -> dict[str, Any] | None:
    """Return the cached result if it exists and is younger than the TTL."""
    try:
        if _redis_config():
            (raw,) = await _redis_pipeline([["GET", _result_key(key)]])
            return json.loads(raw) if raw else None
        await _ensure_sqlite()
        cutoff = time.time() - CACHE_TTL_SECONDS
        async with aiosqlite.connect(_sqlite_path()) as db:
            async with db.execute(
                "SELECT result_json FROM analysis_cache WHERE key = ? AND created_at >= ?", (key, cutoff)
            ) as cur:
                row = await cur.fetchone()
        return json.loads(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001
        log.warning("cache read failed: %s", exc)
        return None


async def set_cached(key: str, query: str, entity_type: str, result: dict[str, Any]) -> None:
    now = time.time()
    payload = json.dumps(result, ensure_ascii=False)
    try:
        if _redis_config():
            meta = json.dumps(
                {
                    "query": query.strip(),
                    "type": entity_type,
                    "entity_name": result.get("entity_name"),
                    "worth_it_score": result.get("worth_it_score"),
                    "created_at": now,
                }
            )
            ttl = str(max(1, CACHE_TTL_SECONDS))
            await _redis_pipeline(
                [
                    ["SET", _result_key(key), payload, "EX", ttl],
                    ["SET", _meta_key(key), meta, "EX", ttl],
                    ["ZADD", _RECENT_KEY, str(now), _meta_key(key)],
                    ["ZREMRANGEBYSCORE", _RECENT_KEY, "-inf", str(now - CACHE_TTL_SECONDS)],
                ]
            )
            return
        await _ensure_sqlite()
        async with aiosqlite.connect(_sqlite_path()) as db:
            await db.execute(
                """INSERT OR REPLACE INTO analysis_cache
                   (key, query, entity_type, entity_name, worth_it_score, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (key, query.strip(), entity_type, result.get("entity_name"), result.get("worth_it_score"), payload, now),
            )
            await db.execute("DELETE FROM analysis_cache WHERE created_at < ?", (now - CACHE_TTL_SECONDS,))
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("cache write failed: %s", exc)


async def get_recent(limit: int = 5) -> list[dict[str, Any]]:
    cutoff = time.time() - CACHE_TTL_SECONDS
    try:
        if _redis_config():
            (members,) = await _redis_pipeline(
                [["ZREVRANGEBYSCORE", _RECENT_KEY, "+inf", str(cutoff), "LIMIT", "0", str(limit * 2)]]
            )
            if not members:
                return []
            (metas,) = await _redis_pipeline([["MGET", *members]])
            out: list[dict[str, Any]] = []
            for raw in metas or []:
                if not raw:
                    continue
                m = json.loads(raw)
                out.append(
                    {
                        "query": m.get("query"),
                        "type": m.get("type"),
                        "entity_name": m.get("entity_name") or m.get("query"),
                        "worth_it_score": m.get("worth_it_score"),
                        "created_at": m.get("created_at"),
                    }
                )
            return out[:limit]
        await _ensure_sqlite()
        async with aiosqlite.connect(_sqlite_path()) as db:
            async with db.execute(
                """SELECT query, entity_type, entity_name, worth_it_score, created_at
                   FROM analysis_cache WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (cutoff, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"query": r[0], "type": r[1], "entity_name": r[2] or r[0], "worth_it_score": r[3], "created_at": r[4]}
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("recent read failed: %s", exc)
        return []
