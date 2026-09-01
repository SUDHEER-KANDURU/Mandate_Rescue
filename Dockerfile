FROM python:3.11-slim

WORKDIR /app

# System deps for scikit-learn/pandas wheels are generally not needed on slim with
# recent manylinux wheels, but curl is handy for the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/healthz || exit 1

# Run with Flask's own dev server for parity with local `python backend/app.py`
# (the project explicitly disables the debug reloader's DB-file watching — see
# app.py's __main__ block — so debug mode is safe to keep here for the demo).
CMD ["python", "backend/app.py"]
