"""
LINEウェブフックサーバー + スケジューラー
  - follow   → recipients に追加
  - unfollow → recipients から削除
  - 月〜金 9:00〜15:59 の間、5分ごとに収集・分析・通知を自動実行
  - 起動: uvicorn webhook.app:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from store.db import migrate
from store import recipients

logger = logging.getLogger(__name__)

JST = pytz.timezone("Asia/Tokyo")


def _collect_job() -> None:
    from collector.agent import run
    logger.info("[scheduler] collection started")
    try:
        run()
    except Exception:
        logger.exception("[scheduler] collection failed")


def _monitor_job() -> None:
    from monitor.agent import run
    logger.info("[scheduler] monitor started")
    try:
        run()
    except Exception:
        logger.exception("[scheduler] monitor failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    migrate()

    scheduler = BackgroundScheduler(timezone=JST)
    # 月〜金 9:00〜15:59 の間、5分ごとに収集
    scheduler.add_job(
        _collect_job, "cron",
        day_of_week="mon-fri", hour="9-15", minute="*/5",
        id="collector",
    )
    # 収集の2分後に分析・通知（9:02, 9:07, ...）
    scheduler.add_job(
        _monitor_job, "cron",
        day_of_week="mon-fri", hour="9-15",
        minute="2,7,12,17,22,27,32,37,42,47,52,57",
        id="monitor",
    )
    scheduler.start()
    logger.info("Scheduler started. Webhook server ready.")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not _verify_signature(body, signature):
        logger.warning("Invalid LINE signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for event in data.get("events", []):
        _handle_event(event)

    return JSONResponse({"status": "ok"})


def _handle_event(event: dict) -> None:
    event_type = event.get("type")
    user_id: str | None = event.get("source", {}).get("userId")
    if not user_id:
        return

    if event_type == "follow":
        recipients.add(user_id)
        logger.info("follow: %s registered", user_id)
    elif event_type == "unfollow":
        recipients.remove(user_id)
        logger.info("unfollow: %s removed", user_id)
    else:
        logger.debug("Unhandled event type: %s", event_type)


def _verify_signature(body: bytes, signature: str) -> bool:
    secret = os.environ.get("LINE_CHANNEL_SECRET", "")
    if not secret:
        logger.warning("LINE_CHANNEL_SECRET not set — skipping signature check")
        return True
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)
