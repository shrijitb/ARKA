# workers/telegram_bot/Dockerfile
# Polling bot — no inbound port needed.
# Talks outbound to Telegram API and to the hypervisor via HYPERVISOR_URL.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    "python-telegram-bot>=21.0" \
    httpx \
    python-dotenv

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
