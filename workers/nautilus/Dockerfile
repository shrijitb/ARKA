# workers/nautilus/Dockerfile
#
# Build context: project root (.)
# After COPY the container layout is:
#   /app/worker_api.py
#   /app/indicators/adx.py
#   /app/strategies/swing_macd.py
#   /app/data/feeds/funding_rates.py  (live OKX feed)
#   /app/data/feeds/order_book.py     (live OKX order book)
#
# nautilus_trader notes:
#   Requires Rust toolchain to build from source on ARM64 slim images.
#   The install is attempted but does NOT fail the build on error — worker_api.py
#   already handles ImportError gracefully and falls back to the internal
#   paper trading simulator (engine_ready=False, engine_error set).
#   For the Raspberry Pi 5 (ARM64 Python 3.12) pre-built wheels are available
#   from PyPI as of nautilus_trader >= 1.200. If they're missing for this exact
#   platform/Python combo, the fallback activates automatically.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       gcc \
       g++ \
       make \
       libssl-dev \
       pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Rust toolchain — required to build nautilus_trader from source when a
# pre-built wheel is not available for this platform/Python combination.
# On ARM64 (Raspberry Pi 5, Python 3.12), pre-built wheels exist as of
# nautilus_trader >= 1.200 and this path is only hit as a fallback.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:$PATH"

WORKDIR /app

# Core runtime deps — these must succeed
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    httpx \
    structlog \
    yfinance \
    pandas \
    requests \
    pyyaml

# nautilus_trader — best-effort.  Pre-built wheel tried first; source build
# is the fallback (Rust toolchain above makes this work on any platform).
# If the install still fails the worker starts in paper simulation mode.
RUN pip install --no-cache-dir nautilus_trader \
    || echo "WARNING: nautilus_trader install failed. Starting in paper simulation mode."

# Copy worker source from project root build context.
COPY workers/nautilus/ .

# Copy data/feeds/ so strategies can import live OKX feeds (funding_rates,
# order_book) without triggering the ImportError synthetic fallback.
# The feeds are placed at /app/data/feeds/ and data/__init__.py is created
# so Python treats data as a package under PYTHONPATH=/app.
COPY data/feeds/ data/feeds/
RUN touch data/__init__.py data/feeds/__init__.py

# Also copy ccxt so OKX feed calls work inside the container.
RUN pip install --no-cache-dir ccxt

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8001

CMD ["uvicorn", "worker_api:app", "--host", "0.0.0.0", "--port", "8001"]
