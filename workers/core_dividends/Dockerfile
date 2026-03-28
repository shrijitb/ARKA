# workers/core_dividends/Dockerfile
# Passive dividend sleeve — SCHD + VYM buy-and-hold paper tracker.
# No platform: flag — Docker pulls native arch automatically.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    structlog \
    yfinance \
    pandas \
    requests

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PORT=8006

EXPOSE 8006

CMD ["sh", "-c", "uvicorn worker_api:app --host 0.0.0.0 --port ${PORT}"]
