# ---------------------------------------------------------------------------
# Firstsource RFP Response Generator
# Single-stage image; the app is pure Python with no build step.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Python behaves better in containers with these set.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code, server-side knowledge base and brand assets.
COPY app/ ./app/
COPY knowledge/ ./knowledge/
COPY static/ ./static/
COPY access/ ./access/

# Run as a non-root user.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else sys.exit(1)"

# Keep --workers 1: Microsoft SSO sessions live in process memory (app/auth.py),
# so several workers would log users out at random. Generation runs in threads,
# so one worker still serves concurrent users. Move sessions to a shared store
# before raising this.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
