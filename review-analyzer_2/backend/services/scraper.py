"""Entity resolution and multi-source review ingestion via SerpApi (+ Reddit .json)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import AMAZON_DOMAIN, ConfigError, get_key

log = logging.getLogger("worthit.scraper")

SERPAPI_URL = "https://serpapi.com/search.json"
REDDIT_USER_AGENT = "Mozilla/5.0 (compatible; ReviewAnalyzerBot/1.0)"

PLACE_RELEVANT_TARGET = 20
PLACE_LOWEST_TARGET = 15
MAX_PLACE_REVIEW_PAGES = 2
REDDIT_TIMEOUT_SECONDS = 35
MAX_REDDIT_THREADS = 3
MAX_COMMENTS_PER_THREAD = 12


# --------------------------------------------------------------------------- errors
class ScraperError(Exception):
    status_code = 502

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class SerpApiError(ScraperError):
    """SerpApi returned an error, was unreachable, or rate-limited us."""


class EntityNotFoundError(ScraperError):
    status_code = 404


class InsufficientDataError(ScraperError):
    status_code = 422


# --------------------------------------------------------------------------- models
@dataclass
class RawReview:
    source: str  # google_maps | amazon | google_shopping | reddit
    text: str
    rating: Optional[float] = None
    date: Optional[str] = None
    verified: bool = False
    local_guide: bool = False
    upvotes: Optional[int] = None
    author: Optional[str] = None
    batch: Optional[str] = None  # e.g. "most relevant", "lowest rated"
    context: Optional[str] = None  # e.g. "r/headphones: <thread title>"


@dataclass
class Entity:
    name: str
    subtitle: str
    kind: str  # place | product
    identifier: str
    platform_rating: Optional[float] = None
    platform_review_count: Optional[int] = None
    context: str = ""  # extra platform-level facts for the LLM (star histogram, etc.)


@dataclass
class IngestionResult:
    entity: Entity
    reviews: list[RawReview]  # Google Maps / Amazon / Shopping
    reddit: list[RawReview]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"\d[\d,]*", str(value))
    return int(m.group().replace(",", "")) if m else None


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _msg(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__


def _is_fatal(exc: BaseException) -> bool:
    """Key/config problems should fail the request instead of degrading."""
    return isinstance(exc, ConfigError) or (isinstance(exc, SerpApiError) and exc.status_code == 401)


async def _serp(client: httpx.AsyncClient, params: dict[str, Any], retries: int = 1) -> dict[str, Any]:
    """One SerpApi call with retry on 429/5xx/network errors. Empty results -> {}."""
    key = get_key("SERPAPI_KEY")
    if not key:
        raise ConfigError("SERPAPI_KEY is missing. Add it to your .env file and restart the server.")
    query = {**params, "api_key": key}

    started = time.perf_counter()
    engine = str(params.get("engine", "unknown"))
    for attempt in range(retries + 1):
        try:
            resp = await client.get(SERPAPI_URL, params=query)
        except httpx.TimeoutException as exc:
            # Retrying an already-slow search makes the user wait twice as long.
            log.warning("SerpApi %s timed out after %.1fs", engine, time.perf_counter() - started)
            raise SerpApiError("SerpApi took too long to respond.", 504) from exc
        except httpx.HTTPError as exc:
            if attempt < retries:
                log.warning("SerpApi %s attempt %d failed (%s); retrying once", engine, attempt + 1, exc.__class__.__name__)
                await asyncio.sleep(0.5)
                continue
            log.warning("SerpApi %s failed after %.1fs (%s)", engine, time.perf_counter() - started, exc.__class__.__name__)
            raise SerpApiError(f"Could not reach SerpApi ({exc.__class__.__name__}).", 504) from exc

        try:
            data = resp.json()
        except ValueError:
            data = None

        if resp.status_code == 200 and isinstance(data, dict):
            err = data.get("error")
            if err:
                low = str(err).lower()
                if "hasn't returned any results" in low or "no results" in low:
                    return {}
                raise SerpApiError(f"SerpApi: {err}", 502)
            log.info("SerpApi %s completed in %.1fs", engine, time.perf_counter() - started)
            return data

        detail = (data.get("error") if isinstance(data, dict) else None) or resp.text[:200] or f"HTTP {resp.status_code}"
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            log.warning("SerpApi %s returned %d; retrying once", engine, resp.status_code)
            await asyncio.sleep(0.5)
            continue
        if resp.status_code in (401, 403):
            raise SerpApiError("SerpApi rejected the API key. Check SERPAPI_KEY in your .env file.", 401)
        if resp.status_code == 429:
            raise SerpApiError("SerpApi rate limit or search quota reached. Try again later.", 429)
        raise SerpApiError(f"SerpApi error: {detail}", 502)

    raise SerpApiError("SerpApi request failed.", 502)


def _dedupe_pairs(reviews: list[RawReview]) -> list[RawReview]:
    """Drop the same review fetched twice (e.g. in both Google batches)."""
    seen: set[tuple[str, str, str]] = set()
    out: list[RawReview] = []
    for r in reviews:
        k = (r.source, (r.author or "").lower(), r.text[:200].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# --------------------------------------------------------------------------- places
async def _resolve_place(client: httpx.AsyncClient, query: str) -> Entity:
    data = await _serp(client, {"engine": "google_maps", "q": query, "type": "search", "hl": "en"})
    place: Optional[dict[str, Any]] = None
    pr = data.get("place_results")
    if isinstance(pr, dict) and pr.get("data_id"):
        place = pr
    else:
        for item in data.get("local_results") or []:
            if isinstance(item, dict) and item.get("data_id"):
                place = item
                break
    if not place:
        raise EntityNotFoundError(f'No Google Maps place matched "{query}". Try adding the city or area.')

    name = _clean(place.get("title")) or query
    address = _clean(place.get("address")) or _clean(place.get("type")) or "Address unavailable"
    return Entity(
        name=name,
        subtitle=address,
        kind="place",
        identifier=str(place["data_id"]),
        platform_rating=_to_float(place.get("rating")),
        platform_review_count=_to_int(place.get("reviews")),
    )


def _parse_place_review(r: dict[str, Any], batch: str) -> Optional[RawReview]:
    extracted = r.get("extracted_snippet") if isinstance(r.get("extracted_snippet"), dict) else {}
    text = _clean(r.get("snippet") or extracted.get("original") or extracted.get("translated"))
    user = r.get("user") if isinstance(r.get("user"), dict) else {}
    iso = _clean(r.get("iso_date"))
    return RawReview(
        source="google_maps",
        text=text,
        rating=_to_float(r.get("rating")),
        date=iso[:10] if iso else (_clean(r.get("date")) or None),
        local_guide=bool(user.get("local_guide")),
        upvotes=_to_int(r.get("likes")),
        author=_clean(user.get("name")) or None,
        batch=batch,
    )


async def _place_reviews(client: httpx.AsyncClient, data_id: str, sort_by: str, target: int, batch: str) -> list[RawReview]:
    out: list[RawReview] = []
    token: Optional[str] = None
    for _ in range(MAX_PLACE_REVIEW_PAGES):  # bound pagination latency per batch
        params: dict[str, Any] = {"engine": "google_maps_reviews", "data_id": data_id, "sort_by": sort_by, "hl": "en"}
        if token:
            params["next_page_token"] = token
            params["num"] = 20
        data = await _serp(client, params)
        for raw in data.get("reviews") or []:
            if isinstance(raw, dict):
                parsed = _parse_place_review(raw, batch)
                if parsed:
                    out.append(parsed)
        token = (data.get("serpapi_pagination") or {}).get("next_page_token")
        if len(out) >= target or not token:
            break
    return out[:target]


async def _ingest_place(client: httpx.AsyncClient, query: str, warnings: list[str]) -> tuple[Entity, list[RawReview]]:
    entity = await _resolve_place(client, query)
    results = await asyncio.gather(
        _place_reviews(client, entity.identifier, "qualityScore", PLACE_RELEVANT_TARGET, "most relevant"),
        _place_reviews(client, entity.identifier, "ratingLow", PLACE_LOWEST_TARGET, "lowest rated"),
        return_exceptions=True,
    )
    reviews: list[RawReview] = []
    for label, res in zip(("most relevant", "lowest-rated"), results):
        if isinstance(res, BaseException):
            if _is_fatal(res):
                raise res
            warnings.append(f"Could not load the {label} Google reviews ({_msg(res)}).")
        else:
            reviews.extend(res)
    return entity, reviews


# --------------------------------------------------------------------------- products
def _pick_amazon_result(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    candidates = [r for r in (data.get("organic_results") or []) if isinstance(r, dict) and r.get("asin")]
    for r in candidates:
        if not r.get("sponsored"):
            return r
    return candidates[0] if candidates else None


async def _resolve_amazon(client: httpx.AsyncClient, query: str) -> Entity:
    data = await _serp(client, {"engine": "amazon", "k": query, "amazon_domain": AMAZON_DOMAIN})
    item = _pick_amazon_result(data)
    if not item:
        raise EntityNotFoundError(f'No Amazon product matched "{query}".')
    price = _clean(item.get("price"))
    brand = _clean(item.get("brand"))
    parts = [p for p in (brand, price and f"{price} on {AMAZON_DOMAIN}") if p]
    return Entity(
        name=_clean(item.get("title")) or query,
        subtitle=" - ".join(parts) or f"Listed on {AMAZON_DOMAIN}",
        kind="product",
        identifier=str(item["asin"]),
        platform_rating=_to_float(item.get("rating")),
        platform_review_count=_to_int(item.get("reviews")),
    )


def _parse_amazon_review(r: dict[str, Any], batch: str) -> Optional[RawReview]:
    title = _clean(r.get("title"))
    body = _clean(r.get("text") or r.get("body"))
    text = f"{title}. {body}" if title and body else (body or title)
    country = r.get("country") if isinstance(r.get("country"), dict) else {}
    return RawReview(
        source="amazon",
        text=text,
        rating=_to_float(r.get("rating")),
        date=_clean(r.get("date")) or None,
        verified=bool(r.get("verified_purchase")),
        upvotes=_to_int(r.get("helpful_votes")),
        author=_clean(r.get("author")) or None,
        batch=batch,
        context=f"review from {_clean(country.get('name'))}" if country.get("name") else None,
    )


async def _amazon_product_reviews(client: httpx.AsyncClient, asin: str) -> tuple[list[RawReview], str]:
    """SerpApi has no standalone Amazon reviews engine; reviews come with the product page."""
    data = await _serp(client, {"engine": "amazon_product", "asin": asin, "amazon_domain": AMAZON_DOMAIN})
    info = data.get("reviews_information") if isinstance(data.get("reviews_information"), dict) else {}

    reviews: list[RawReview] = []
    for key, batch in (("authors_reviews", "top reviews"), ("other_countries_reviews", "reviews from other countries")):
        for raw in info.get(key) or []:
            if isinstance(raw, dict):
                parsed = _parse_amazon_review(raw, batch)
                if parsed:
                    reviews.append(parsed)

    summary = info.get("summary") if isinstance(info.get("summary"), dict) else {}
    context: list[str] = []
    hist = summary.get("customer_reviews")
    if isinstance(hist, dict) and hist:
        context.append("Star distribution shown on the Amazon page (approx. % of ratings): " + ", ".join(f"{k}: {v}" for k, v in hist.items()) + ".")
    summary_text = _clean(summary.get("text"))
    if summary_text:
        context.append(f"Amazon's own auto-generated summary of customer reviews: {summary_text[:500]}")
    return reviews, " ".join(context)


async def _resolve_shopping(client: httpx.AsyncClient, query: str) -> Entity:
    data = await _serp(client, {"engine": "google_shopping", "q": query, "hl": "en"})
    for item in data.get("shopping_results") or []:
        if isinstance(item, dict) and item.get("product_id"):
            seller = _clean(item.get("source"))
            price = _clean(item.get("price"))
            return Entity(
                name=_clean(item.get("title")) or query,
                subtitle=" - ".join(p for p in (price, seller and f"sold by {seller}") if p) or "Google Shopping listing",
                kind="product",
                identifier=str(item["product_id"]),
                platform_rating=_to_float(item.get("rating")),
                platform_review_count=_to_int(item.get("reviews")),
            )
    raise EntityNotFoundError(f'No Google Shopping product matched "{query}".')


async def _shopping_reviews(client: httpx.AsyncClient, product_id: str) -> list[RawReview]:
    data = await _serp(client, {"engine": "google_product", "product_id": product_id, "reviews": 1, "hl": "en"})
    block = data.get("reviews_results") if isinstance(data.get("reviews_results"), dict) else {}
    out: list[RawReview] = []
    for r in block.get("reviews") or []:
        if not isinstance(r, dict):
            continue
        title = _clean(r.get("title"))
        body = _clean(r.get("content") or r.get("snippet"))
        out.append(
            RawReview(
                source="google_shopping",
                text=f"{title}. {body}" if title and body else (body or title),
                rating=_to_float(r.get("rating")),
                date=_clean(r.get("date")) or None,
                author=_clean(r.get("source")) or None,
                batch="shopping reviews",
            )
        )
    return out


async def _ingest_product(client: httpx.AsyncClient, query: str, warnings: list[str]) -> tuple[Entity, list[RawReview]]:
    entity: Optional[Entity] = None
    reviews: list[RawReview] = []

    try:
        entity = await _resolve_amazon(client, query)
    except (ScraperError, ConfigError) as exc:
        if _is_fatal(exc):
            raise
        warnings.append(f"Amazon lookup failed ({_msg(exc)}); trying Google Shopping.")

    if entity:
        try:
            reviews, entity.context = await _amazon_product_reviews(client, entity.identifier)
        except (ScraperError, ConfigError) as exc:
            if _is_fatal(exc):
                raise
            warnings.append(f"Could not load Amazon reviews ({_msg(exc)}).")
        if not reviews:
            warnings.append("Amazon returned no readable reviews for this product; falling back to Google Shopping.")

    if not reviews:
        try:
            shop_entity = await _resolve_shopping(client, query)
            shop_reviews = await _shopping_reviews(client, shop_entity.identifier)
            if shop_reviews:
                reviews.extend(shop_reviews)
                entity = entity or shop_entity
            else:
                warnings.append("Google Shopping had no reviews for this product either.")
                entity = entity or shop_entity
        except (ScraperError, ConfigError) as exc:
            if _is_fatal(exc):
                raise
            warnings.append(f"Google Shopping fallback failed ({_msg(exc)}).")

    if entity is None:
        raise EntityNotFoundError(f'No product matched "{query}" on Amazon or Google Shopping.')
    return entity, reviews


# --------------------------------------------------------------------------- reddit
def _reddit_json_url(link: str) -> Optional[str]:
    m = re.search(r"reddit\.com(/r/[^/?#]+/comments/[^/?#]+(?:/[^/?#]*)?)", link or "")
    if not m:
        return None
    return f"https://www.reddit.com{m.group(1).rstrip('/')}.json"


def _utc_date(ts: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


async def _fetch_thread(client: httpx.AsyncClient, url: str) -> list[RawReview]:
    resp = await client.get(
        url,
        params={"limit": 100, "sort": "top", "raw_json": 1},
        headers={"User-Agent": REDDIT_USER_AGENT, "Accept": "application/json"},
    )
    if resp.status_code != 200:
        raise ScraperError(f"Reddit returned HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, list) or len(payload) < 2:
        raise ScraperError("Unexpected Reddit response shape")

    post_children = (payload[0].get("data") or {}).get("children") or []
    post = (post_children[0].get("data") if post_children else {}) or {}
    subreddit = post.get("subreddit_name_prefixed") or "r/unknown"
    context = f"{subreddit}: {_clean(post.get('title'))[:140]}"

    out: list[RawReview] = []
    selftext = _clean(post.get("selftext"))
    if len(selftext) >= 15 and selftext not in ("[removed]", "[deleted]"):
        out.append(
            RawReview(
                source="reddit",
                text=selftext[:1200],
                upvotes=_to_int(post.get("score")),
                date=_utc_date(post.get("created_utc")),
                author=_clean(post.get("author")) or None,
                batch="thread post",
                context=context,
            )
        )

    comments: list[RawReview] = []
    for child in (payload[1].get("data") or {}).get("children") or []:
        if child.get("kind") != "t1":
            continue
        d = child.get("data") or {}
        body = _clean(d.get("body"))
        author = d.get("author")
        if d.get("stickied") or author in ("AutoModerator", "[deleted]") or body in ("[removed]", "[deleted]", ""):
            continue
        comments.append(
            RawReview(
                source="reddit",
                text=body[:1200],
                upvotes=_to_int(d.get("score")),
                date=_utc_date(d.get("created_utc")),
                author=_clean(author) or None,
                batch="top-level comment",
                context=context,
            )
        )
    comments.sort(key=lambda c: c.upvotes or 0, reverse=True)
    return out + comments[:MAX_COMMENTS_PER_THREAD]


_SPEC_TOKEN_RE = re.compile(r"^(\d+|ram|rom|storage|gb|tb|ssd|memory|\d+\s?(gb|tb|mah|mp|hz))$", re.I)


def _reddit_queries(query: str) -> list[str]:
    """Precise query first; then a short version with spec noise (12GB RAM, 256 GB storage...) removed."""
    queries = [f"site:reddit.com {query} review worth it"]
    tokens = [t for t in re.split(r"[\s,;]+", query) if t and not _SPEC_TOKEN_RE.match(t)]
    short = " ".join(tokens[:5])
    if short and short.lower() != query.lower().strip():
        queries.append(f"site:reddit.com {short} review")
    return queries


async def _fetch_reddit(
    serp_client: httpx.AsyncClient, reddit_client: httpx.AsyncClient, query: str, warnings: list[str]
) -> list[RawReview]:
    """Never raises: Reddit is a bonus source, so every failure degrades to a warning."""
    threads: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for q in _reddit_queries(query):
        try:
            data = await _serp(serp_client, {"engine": "google", "q": q, "num": 10, "hl": "en"})
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Reddit search failed ({_msg(exc)}); continuing without community sentiment.")
            return []
        for item in data.get("organic_results") or []:
            url = _reddit_json_url(item.get("link", "")) if isinstance(item, dict) else None
            if url and url not in seen:
                seen.add(url)
                threads.append((item, url))
            if len(threads) >= MAX_REDDIT_THREADS:
                break
        if threads:
            break  # only widen the search when the precise query found nothing

    if not threads:
        warnings.append("No Reddit threads were found for this query; the analysis uses review sites only.")
        return []

    fetched = await asyncio.gather(*(_fetch_thread(reddit_client, url) for _, url in threads), return_exceptions=True)
    out: list[RawReview] = []
    fallback_used = 0
    for (item, _url), res in zip(threads, fetched):
        if isinstance(res, list) and res:
            out.extend(res)
            continue
        snippet = _clean(item.get("snippet"))
        title = _clean(item.get("title"))
        if len(snippet) >= 15:  # fall back to the Google snippet if Reddit blocked us
            out.append(RawReview(source="reddit", text=f"{title}: {snippet}" if title else snippet, batch="search snippet", context=title or None))
            fallback_used += 1
    if fallback_used:
        warnings.append(f"Reddit blocked or rate-limited direct access for {fallback_used} thread(s); search-result snippets were used instead.")
    if not out:
        warnings.append("Reddit threads were found but no readable comments could be retrieved.")
    return out


# --------------------------------------------------------------------------- public API
async def ingest(query: str, entity_type: str) -> IngestionResult:
    """Resolve the entity and gather reviews + Reddit sentiment concurrently."""
    warnings: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0)) as serp_client, httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True
    ) as reddit_client:
        # Reddit does not depend on entity resolution, so it runs alongside it.
        reddit_task = asyncio.create_task(_fetch_reddit(serp_client, reddit_client, query, warnings))
        try:
            if entity_type == "place":
                entity, reviews = await _ingest_place(serp_client, query, warnings)
            else:
                entity, reviews = await _ingest_product(serp_client, query, warnings)
        except BaseException:
            reddit_task.cancel()
            await asyncio.gather(reddit_task, return_exceptions=True)
            raise
        try:
            reddit_reviews = await asyncio.wait_for(reddit_task, timeout=REDDIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            warnings.append("Reddit took too long to respond; the verdict uses the review-site data available.")
            await asyncio.gather(reddit_task, return_exceptions=True)
            reddit_reviews = []

    return IngestionResult(entity=entity, reviews=_dedupe_pairs(reviews), reddit=_dedupe_pairs(reddit_reviews), warnings=warnings)
