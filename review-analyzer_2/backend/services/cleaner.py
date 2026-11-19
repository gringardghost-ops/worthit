"""Pre-processing: drop junk, de-duplicate, tag credibility signals for the LLM."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .scraper import RawReview

MIN_TEXT_LENGTH = 15
NEAR_DUPLICATE_THRESHOLD = 0.8
MAX_PRIMARY_REVIEWS = 30
MAX_REDDIT_COMMENTS = 10
MAX_TEXT_CHARS = 420

SOURCE_LABELS = {
    "google_maps": "Google Reviews",
    "amazon": "Amazon Reviews",
    "google_shopping": "Google Shopping Reviews",
}
SOURCE_TAGS = {
    "google_maps": "GOOGLE MAPS",
    "amazon": "AMAZON",
    "google_shopping": "GOOGLE SHOPPING",
    "reddit": "REDDIT",
}

# Promotional / solicitation phrases (any source) and bare URLs (non-Reddit sources only).
_PROMO_RE = re.compile(
    r"(\bwhats ?app\b|\btelegram\b|\bt\.me/|\bdm me\b|\bpromo code\b|\bdiscount code\b|"
    r"\bclick (the )?link\b|\bcheck my profile\b|\bcontact me (on|at|via)\b|\bfollow me\b|\bearn \$?\d+)",
    re.I,
)
_URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)


@dataclass
class CleanedData:
    primary_lines: list[str] = field(default_factory=list)
    reddit_lines: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # google_or_amazon / reddit / filtered_spam
    by_platform: dict[str, int] = field(default_factory=dict)  # "Google Reviews" -> n
    stats: dict[str, Optional[float]] = field(default_factory=dict)
    spam_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.counts.get("google_or_amazon", 0) + self.counts.get("reddit", 0)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def _shingles(norm: str) -> set[tuple[str, ...]]:
    words = norm.split()
    if len(words) < 4:
        return {tuple(words)}
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _spam_reason(r: RawReview, text: str) -> Optional[str]:
    if len(text) < MIN_TEXT_LENGTH:
        return "too_short"
    if _PROMO_RE.search(text):
        return "promotional"
    if r.source != "reddit" and _URL_RE.search(text):
        return "contains_link"
    return None


def _tag_line(idx: int, r: RawReview, text: str) -> str:
    tags = [f"[SOURCE: {SOURCE_TAGS.get(r.source, r.source.upper())}]"]
    if r.rating is not None:
        tags.append(f"[RATING: {r.rating:g}/5]")
    if r.verified:
        tags.append("[VERIFIED]")
    if r.local_guide:
        tags.append("[LOCAL GUIDE]")
    if r.upvotes is not None:
        label = "REDDIT UPVOTES" if r.source == "reddit" else "HELPFUL VOTES"
        tags.append(f"[{label}: {r.upvotes}]")
    if r.date:
        tags.append(f"[DATE: {r.date}]")
    if r.batch:
        tags.append(f"[SAMPLE: {r.batch}]")
    if r.context:
        tags.append(f"[{'THREAD' if r.source == 'reddit' else 'NOTE'}: {r.context}]")
    safe = text[:MAX_TEXT_CHARS].replace("</reviews>", "<\\/reviews>")
    return f'#{idx} {" ".join(tags)} "{safe}"'


def clean(reviews: list[RawReview], reddit: list[RawReview]) -> CleanedData:
    """Filter, de-duplicate and tag. Returns counts matching the API's source breakdown."""
    out = CleanedData()
    spam = 0
    seen_hashes: set[str] = set()
    seen_shingles: list[set[tuple[str, ...]]] = []
    kept_primary: list[tuple[RawReview, str]] = []
    kept_reddit: list[tuple[RawReview, str]] = []

    def note(reason: str) -> None:
        nonlocal spam
        spam += 1
        out.spam_reasons[reason] = out.spam_reasons.get(reason, 0) + 1

    # Rank so that, on duplicates, the most credible copy survives.
    def credibility(r: RawReview) -> tuple[int, int]:
        return (1 if (r.verified or r.local_guide) else 0, r.upvotes or 0)

    for r in sorted(reviews + reddit, key=credibility, reverse=True):
        text = re.sub(r"\s+", " ", r.text or "").strip()
        reason = _spam_reason(r, text)
        if reason:
            note(reason)
            continue
        norm = _normalize(text)
        if norm in seen_hashes:
            note("duplicate")
            continue
        sh = _shingles(norm)
        if any(_jaccard(sh, other) >= NEAR_DUPLICATE_THRESHOLD for other in seen_shingles):
            note("near_duplicate")
            continue
        seen_hashes.add(norm)
        seen_shingles.append(sh)
        (kept_reddit if r.source == "reddit" else kept_primary).append((r, text))

    # Cap volume sent to the LLM; keep the most informative (credible, then upvoted) first.
    kept_reddit.sort(key=lambda p: p[0].upvotes or 0, reverse=True)
    kept_reddit = kept_reddit[:MAX_REDDIT_COMMENTS]
    if len(kept_primary) > MAX_PRIMARY_REVIEWS:
        kept_primary = kept_primary[:MAX_PRIMARY_REVIEWS]
    # Restore a stable, readable order for the prompt: lowest-rated cluster last.
    kept_primary.sort(key=lambda p: (p[0].batch or "", -(p[0].upvotes or 0)))

    for i, (r, text) in enumerate(kept_primary, 1):
        out.primary_lines.append(_tag_line(i, r, text))
        label = SOURCE_LABELS.get(r.source, "Reviews")
        out.by_platform[label] = out.by_platform.get(label, 0) + 1
    for i, (r, text) in enumerate(kept_reddit, 1):
        out.reddit_lines.append(_tag_line(i, r, text))

    rated = [r.rating for r, _ in kept_primary if r.rating is not None]
    out.stats = {
        "rated_reviews": float(len(rated)),
        "sample_average_rating": round(sum(rated) / len(rated), 2) if rated else None,
        "five_star_pct": round(100 * sum(1 for x in rated if x >= 4.5) / len(rated), 1) if rated else None,
        "one_two_star_pct": round(100 * sum(1 for x in rated if x <= 2.5) / len(rated), 1) if rated else None,
    }
    out.counts = {
        "google_or_amazon": len(kept_primary),
        "reddit": len(kept_reddit),
        "filtered_spam": spam,
    }
    return out
