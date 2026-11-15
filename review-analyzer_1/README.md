# worth it?: authentic experience and product review analyzer

Type a product or a local place. worth it? pulls Google Maps or Amazon reviews plus Reddit threads through SerpApi, removes spam and duplicates, and asks Gemini for a structured "is it worth it?" verdict with a 1-10 score, trust confidence, pros, cons and authenticity red flags.

## What you need

- Python 3.11 or newer
- A [SerpApi](https://serpapi.com/manage-api-key) key
- A [Google Gemini](https://aistudio.google.com/apikey) API key

## Quick start

**Windows:** double-click `start.bat`.

**macOS:** run `chmod +x start.sh start.command` once, then double-click `start.command`.

**Linux:** run `chmod +x start.sh start.command` once, then `./start.sh`.

The launcher creates `.venv`, installs `requirements.txt`, copies `.env.example` to `.env` if needed, opens http://localhost:8000 after two seconds, and starts Uvicorn with auto-reload.

On the first run it stops short of working analyses: open the new `.env`, paste both keys, and restart the launcher. The status pill in the top bar shows whether both keys are loaded.

### Manual start

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then edit .env
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

## Configuration (`.env`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `SERPAPI_KEY` | required | Entity resolution and review data |
| `GEMINI_API_KEY` | required | LLM synthesis |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model used for analysis |
| `CACHE_TTL_HOURS` | `24` | How long a result is served from SQLite |
| `AMAZON_DOMAIN` | `amazon.com` | Amazon marketplace for product lookups |
| `DB_PATH` | `data/cache.sqlite3` | SQLite cache location |

## API

- `POST /api/analyze` with `{"query": "...", "type": "product" | "place", "force_refresh": false}`
- `GET /api/health` reports which keys are loaded
- `GET /api/recent` returns the last 5 cached analyses

Interactive docs are at http://localhost:8000/docs.

## How it degrades

- No Reddit threads, or Reddit blocks direct access: the search snippet is used, or Reddit is skipped. The result lists this under `data_notes`.
- Amazon search or reviews fail: falls back to Google Shopping.
- One of the two Google review batches fails: the other is still analyzed.
- Zero usable reviews overall: a clear 422 error. Rate limits return 429, bad keys 401/503.
- Identical concurrent searches share one pipeline run, so you are not charged twice.

## Project layout

```
backend/
  main.py            FastAPI app, endpoints, static mount
  config.py          .env loading, key checks
  database.py        SQLite cache and recent searches
  services/
    scraper.py       SerpApi + Reddit ingestion
    cleaner.py       spam filter, de-duplication, credibility tags
    analyzer.py      Gemini structured-output analysis
frontend/            index.html, styles.css, app.js (no build step)
start.bat / start.sh / start.command
```

## Notes

- The cache key is the lower-cased query plus type, so "sony wh-1000xm5" and "Sony WH-1000XM5" share a result.
- Each uncached analysis spends roughly 4 to 8 SerpApi searches, so "Re-analyze live" uses credits.
