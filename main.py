"""
main.py — FastAPI entry point for the DSR Slack Bot.

- Socket Mode (WebSocket) handles all Slack events — exactly as before
- FastAPI adds a single GET /health endpoint
- Background scheduler handles 8 PM reminders

Run with:
    uvicorn main:app --host 0.0.0.0 --port 3000
Or:
    ./start.sh
"""

import os
import logging
import time
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from slack_bolt.adapter.socket_mode import SocketModeHandler

log = logging.getLogger(__name__)

_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Import triggers bot.py startup (DB init, portal login, agent init)
    from bot import app as slack_bolt_app, db, check_and_remind_dsr

    app.state.db = db

    # Start Socket Mode in a background thread — listens to Slack via WebSocket
    slack_app_token = os.getenv("SLACK_APP_TOKEN")
    if not slack_app_token:
        raise RuntimeError("SLACK_APP_TOKEN is missing from .env")

    socket_handler = SocketModeHandler(slack_bolt_app, slack_app_token)
    threading.Thread(target=socket_handler.start, daemon=True).start()
    app.state.socket_handler = socket_handler
    log.info("✅ Socket Mode handler started.")

    # 8 PM IST Mon–Fri reminder scheduler
    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(check_and_remind_dsr, "cron", day_of_week="mon-fri", hour=20, minute=0)
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("✅ Scheduler started.")

    log.info("🚀 DSR Bot is running.")
    yield

    scheduler.shutdown(wait=False)
    socket_handler.close()


app = FastAPI(title="DSR Slack Bot", version="2.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    """Simple liveness check."""
    uptime = int(time.time() - _start_time)
    h, r = divmod(uptime, 3600)
    m, s = divmod(r, 60)
    return {
        "status": "ok",
        "uptime": f"{h}h {m}m {s}s",
        "registered_users": len(app.state.db.get_all_users()),
    }
