"""FastAPI entrypoint: /api/analyze, /api/health, /api/recent, and the static frontend."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import database
from .config import FRONTEND_DIR, GEMINI_MODEL, ConfigError, get_key
from .services import analyzer, cleaner, scraper

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worthit")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await database.init_db()
    yield


app = FastAPI(title="worth it? - Authentic Experience & Product Review Analyzer", version="1.0.0", lifespan=lifespan)

# One lock per cache key so identical concurrent requests share a single paid pipeline run.
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class AnalyzeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    type: Literal["product", "place"]
    force_refresh: bool = False

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("Query must be at least 2 characters.")
        return v


async def _run_pipeline(query: str, entity_type: str) -> analyzer.AnalysisResult:
    ingestion = await scraper.ingest(query, entity_type)
    cleaned = cleaner.clean(ingestion.reviews, ingestion.reddit)
    if cleaned.total == 0:
        raise scraper.InsufficientDataError(
            f'Found "{ingestion.entity.name}" but no usable reviews. '
            + (" ".join(ingestion.warnings) or "Try a more specific or more popular search.")
        )
    return await analyzer.analyze(ingestion.entity, entity_type, cleaned, ingestion.warnings)


@app.post("/api/analyze", response_model=analyzer.AnalysisResult)
async def analyze_endpoint(req: AnalyzeRequest) -> analyzer.AnalysisResult:
    missing = [name for name in ("SERPAPI_KEY", "GEMINI_API_KEY") if not get_key(name)]
    key = database.make_cache_key(req.query, req.type)

    if not req.force_refresh:
        hit = await database.get_cached(key)
        if hit:
            hit["cached"] = True
            return analyzer.AnalysisResult.model_validate(hit)

    if missing:
        raise HTTPException(503, f"Missing API key(s): {', '.join(missing)}. Add them to your .env file and restart the server.")

    async with _locks[key]:
        if not req.force_refresh:  # another request may have filled the cache while we waited
            hit = await database.get_cached(key)
            if hit:
                hit["cached"] = True
                return analyzer.AnalysisResult.model_validate(hit)
        try:
            result = await _run_pipeline(req.query, req.type)
        except (scraper.ScraperError, analyzer.AnalyzerError, ConfigError) as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled pipeline failure")
            raise HTTPException(500, "Unexpected error while analyzing. Check the server logs for details.") from exc

    await database.set_cached(key, req.query, req.type, result.model_dump())
    return result


@app.get("/api/health")
async def health() -> dict:
    serp, gem = bool(get_key("SERPAPI_KEY")), bool(get_key("GEMINI_API_KEY"))
    return {"status": "ok" if serp and gem else "degraded", "serpapi": serp, "gemini": gem, "model": GEMINI_MODEL}


@app.get("/api/recent")
async def recent() -> list[dict]:
    return await database.get_recent(5)


# Must be mounted last so it does not shadow the /api routes.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
