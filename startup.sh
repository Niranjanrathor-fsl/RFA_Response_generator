#!/bin/bash
# Azure App Service startup command for the Firstsource RFP Response Generator.
#
# Set this file as the "Startup Command" in the App Service (Configuration ->
# General settings -> Startup Command), or paste the gunicorn line directly.
#
# Notes:
#  - -k uvicorn.workers.UvicornWorker runs our ASGI FastAPI app under gunicorn.
#  - --timeout 600 stops gunicorn from killing long LLM calls (default is 30s).
#  - Azure sets $PORT; bind to it. Falls back to 8000 for local parity.
#  - 2 workers suits a team-sized load on a B-tier plan.

gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 2 \
    --bind 0.0.0.0:${PORT:-8000} \
    --timeout 600 \
    --access-logfile '-' \
    --error-logfile '-'
