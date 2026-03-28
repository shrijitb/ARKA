"""
workers/telegram_bot/main.py

MARA Telegram Command Bot — outbound polling, no inbound port required.

Commands:
  /status              — hypervisor capital + regime snapshot
  /regime              — current regime + confidence
  /watchlist           — list tickers on the dynamic watchlist
  /pause <worker>      — pause a specific worker
  /resume <worker>     — resume a specific worker

Free-text messages containing $TICKER add that ticker to the hypervisor watchlist.

Required env vars:
  TELEGRAM_BOT_TOKEN          from @BotFather
  TELEGRAM_ALLOWED_USER_ID    numeric Telegram user ID (all others are rejected)
  HYPERVISOR_URL              e.g. http://hypervisor:8000  (default shown)
"""

import logging
import os

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_UID = int(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "0"))
HYPER_URL   = os.environ.get("HYPERVISOR_URL", "http://hypervisor:8000")

if not BOT_TOKEN:
    logger.warning(
        "TELEGRAM_BOT_TOKEN not set — bot standing by. "
        "Set the token in .env and restart the service to activate."
    )
    import time
    while True:
        time.sleep(3600)


# ── Auth guard ────────────────────────────────────────────────────────────────

def _allowed(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ALLOWED_UID


async def _deny(update: Update):
    await update.message.reply_text("Unauthorized.")
    logger.warning(f"Rejected message from user {update.effective_user.id}")


# ── Hypervisor HTTP helpers ───────────────────────────────────────────────────

async def _get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(f"{HYPER_URL}{path}")
        r.raise_for_status()
        return r.json()


async def _post(path: str, payload: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.post(f"{HYPER_URL}{path}", json=payload or {})
        r.raise_for_status()
        return r.json()


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    try:
        s = await _get("/status")
        text = (
            f"*MARA Status*\n"
            f"Regime: `{s['regime']}` ({s['regime_confidence']:.0%})\n"
            f"Capital: ${s['total_capital']:.2f}  Free: ${s['free_capital']:.2f}\n"
            f"Mode: {'PAPER' if s.get('paper_trading') else 'LIVE'}\n"
            f"Cycles: {s['cycle_count']}  Halted: {s['halted']}\n"
        )
        workers = s.get("worker_health", {})
        if workers:
            text += "\n*Workers*\n"
            for w, healthy in workers.items():
                alloc = s.get("allocations", {}).get(w, 0.0)
                pnl   = s.get("worker_pnl", {}).get(w, 0.0)
                icon  = "✅" if healthy else "❌"
                text += f"{icon} `{w}` — ${alloc:.2f} alloc | PnL ${pnl:.2f}\n"
    except Exception as exc:
        text = f"❌ Hypervisor unreachable: {exc}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    try:
        r = await _get("/regime")
        text = f"*Regime:* `{r['regime']}` ({r['confidence']:.0%})"
    except Exception as exc:
        text = f"❌ {exc}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    try:
        r = await _get("/watchlist")
        tickers = r.get("watchlist", [])
        text = "*Watchlist:* " + (", ".join(f"`{t}`" for t in tickers) if tickers else "_empty_")
    except Exception as exc:
        text = f"❌ {exc}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /pause <worker>")
        return
    worker = args[0].lower()
    try:
        await _post(f"/workers/{worker}/pause")
        text = f"⏸ `{worker}` paused."
    except httpx.HTTPStatusError as exc:
        text = f"❌ {exc.response.status_code}: {exc.response.text}"
    except Exception as exc:
        text = f"❌ {exc}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /resume <worker>")
        return
    worker = args[0].lower()
    try:
        await _post(f"/workers/{worker}/resume")
        text = f"▶️ `{worker}` resumed."
    except httpx.HTTPStatusError as exc:
        text = f"❌ {exc.response.status_code}: {exc.response.text}"
    except Exception as exc:
        text = f"❌ {exc}"
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Free-text $TICKER handler ─────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    text = update.message.text or ""
    tickers = [w[1:].upper() for w in text.split() if w.startswith("$") and len(w) > 1]
    if not tickers:
        return
    added = []
    for ticker in tickers:
        try:
            await _post("/watchlist", {"ticker": ticker})
            added.append(ticker)
        except Exception as exc:
            logger.warning(f"Watchlist add {ticker} failed: {exc}")
    if added:
        await update.message.reply_text(
            f"Added to watchlist: {', '.join(f'`{t}`' for t in added)}",
            parse_mode="Markdown",
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("regime",    cmd_regime))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("pause",     cmd_pause))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info(f"MARA Telegram bot starting (allowed_uid={ALLOWED_UID}, hypervisor={HYPER_URL})")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
