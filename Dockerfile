FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY verifier ./verifier

# Unprivileged user; /data is the SQLite volume mount point and inherits this ownership.
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown app:app /data
USER app

ENV EMAIL_VERIFIER_ENV=production \
    EMAIL_VERIFIER_DB=/data/jobs.sqlite3 \
    VERIFIER_SMTP_ENABLED=false

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

# One worker: jobs run as in-process tasks and the rate limiter is per-process.
CMD ["uvicorn", "verifier.api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
