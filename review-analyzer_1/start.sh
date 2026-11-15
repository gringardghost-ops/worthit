#!/usr/bin/env bash
# One-command launcher for macOS / Linux.
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: Python 3.11+ was not found. Install it from https://www.python.org/downloads/"
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "ERROR: Python 3.11 or newer is required (found: $("$PY" --version 2>&1))."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (.venv)..."
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing dependencies..."
pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "=================================================================="
  echo " WARNING: .env was just created from .env.example."
  echo " Open .env and paste your SERPAPI_KEY and GEMINI_API_KEY, then"
  echo " restart this script. The app will start, but analyses will fail"
  echo " until both keys are set."
  echo "=================================================================="
  echo ""
fi

( sleep 2; python -m webbrowser -t "http://localhost:8000" >/dev/null 2>&1 ) &

echo "Starting worth it? on http://localhost:8000  (Ctrl+C to stop)"
exec uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
