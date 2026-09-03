#!/usr/bin/env bash
# Local development helper: creates a venv, installs deps, starts with reload.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  echo "No .env found - copying .env.example. Add your ANTHROPIC_API_KEY before generating."
  cp .env.example .env
fi

echo "Starting on http://localhost:${PORT:-8000}"
exec uvicorn app.main:app --reload --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
