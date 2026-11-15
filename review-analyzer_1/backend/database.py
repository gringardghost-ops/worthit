"""SQLite cache (24h TTL by default) and recent-search lookup."""
from __future__ import annotations

import json
import re
import time
from typing import Any

import aiosqlite

from .config import CACHE_TTL_SECONDS, DB_PATH

_SCHEMA = """
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


def make_cache_key(query: str, entity_type: str) -> str:
    """Stable key: case and whitespace-insensitive, scoped by entity type."""
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    return f"{entity_type}:{normalized}"


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def get_cached(key: str) -> dict[str, Any] | None:
    """Return the cached result if it exists and is younger than the TTL."""
    cutoff = time.time() - CACHE_TTL_SECONDS
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT result_json FROM analysis_cache WHERE key = ? AND created_at >= ?",
                (key, cutoff),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.Error:
        return None  # a broken cache must never break an analysis
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


async def set_cached(key: str, query: str, entity_type: str, result: dict[str, Any]) -> None:
    now = time.time()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO analysis_cache
                   (key, query, entity_type, entity_name, worth_it_score, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    query.strip(),
                    entity_type,
                    result.get("entity_name"),
                    result.get("worth_it_score"),
                    json.dumps(result, ensure_ascii=False),
                    now,
                ),
            )
            await db.execute("DELETE FROM analysis_cache WHERE created_at < ?", (now - CACHE_TTL_SECONDS,))
            await db.commit()
    except aiosqlite.Error:
        pass  # caching is best-effort


async def get_recent(limit: int = 5) -> list[dict[str, Any]]:
    cutoff = time.time() - CACHE_TTL_SECONDS
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """SELECT query, entity_type, entity_name, worth_it_score, created_at
                   FROM analysis_cache WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (cutoff, limit),
            ) as cur:
                rows = await cur.fetchall()
    except aiosqlite.Error:
        return []
    return [
        {
            "query": r[0],
            "type": r[1],
            "entity_name": r[2] or r[0],
            "worth_it_score": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]
