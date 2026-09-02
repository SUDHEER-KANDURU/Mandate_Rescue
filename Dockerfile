FROM python:3.11-slim

WORKDIR /app

# System deps: curl for the HEALTHCHECK; tini as PID-1 signal forwarder.
RUN apt-get update && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user — required for production hardening.
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Add backend/ to PYTHONPATH so bare imports (import db, import agent) resolve
# the same way they do when running `python backend/app.py` directly.
ENV PYTHONPATH=/app/backend
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5000/healthz || exit 1

# Use Gunicorn (production WSGI server) instead of Flask's dev server.
# --workers 2: suitable for a single-writer SQLite deployment (more workers
#   would create concurrent write contention on the DB).
# --threads 4: handles SSE long-polls and concurrent reads within each worker.
# --timeout 120: accommodates the SSE run-agent-stream which stays open for
#   the full duration of a 180-case agent run.
# tini is PID-1 so SIGTERM is forwarded correctly on container stop.
# PYTHONPATH=/app/backend means Gunicorn imports `app` directly (bare module),
# matching the bare-import style used throughout the backend.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "120", \
     "--bind", "0.0.0.0:5000", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
