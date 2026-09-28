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
#  - 1 worker: each worker loads its own copy of the reranker (~2.4 GB), and the
#    shared B3 plan has 7 GB. One worker still serves several users at once -
#    generation runs as background jobs in threads.

gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 1 \
    --bind 0.0.0.0:${PORT:-8000} \
    --timeout 600 \
    --access-logfile '-' \
    --error-logfile '-'
