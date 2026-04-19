# hypervisor/Dockerfile
#
# Build context: project root (~/mara)
# Set in docker-compose.yml as:
#   build:
#     context: .
#     dockerfile: hypervisor/Dockerfile
#
# Why project root context is required:
#   hypervisor/main.py imports:
#     from data.feeds.market_data import fetch_macro_snapshot
#     from hypervisor.allocator.capital import RegimeAllocator
#     from hypervisor.regime.classifier import RegimeClassifier
#     from hypervisor.risk.manager import RiskManager
#
#   data/ lives outside hypervisor/ so a narrow context would exclude it.
#   The WORKDIR is /app and the module layout mirrors ~/mara exactly:
#     /app/hypervisor/main.py
#     /app/data/feeds/market_data.py
#     /app/config/

FROM python:3.12-slim

# System deps: curl for healthcheck, gcc for yfinance C extensions.
# docker-cli is no longer installed — container restarts go through the
# scoped docker-socket-proxy sidecar via HTTP (requests library).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only what the hypervisor needs at runtime.
# Excludes: workers/, tests/, .env, .git, .venv
COPY hypervisor/ ./hypervisor/
COPY data/       ./data/
COPY config/     ./config/

# config/ is also mounted as a volume at runtime (read-only).
# The COPY here is a fallback for builds that don't use volumes.

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "hypervisor.main:app", "--host", "0.0.0.0", "--port", "8000"]
