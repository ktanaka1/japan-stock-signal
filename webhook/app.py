"""
LINEウェブフックサーバー
  - follow   → recipients に追加
  - unfollow → recipients から削除
  - POST /jobs/collect, /jobs/monitor → 外部cronからの収集・分析トリガー
    （JOB_TRIGGER_TOKEN を設定した場合のみ有効）
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

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from store.db import migrate
from store import recipients
from admin.routes import router as admin_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    migrate()
    logger.info("Webhook server ready.")
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(admin_router)


# GET だけでなく HEAD も受ける。死活監視サービス（UptimeRobot 等）は既定で HEAD を
# 送るため、GET 専用だと 405 を返してしまう。D1 は触らない静的応答に保つ（ADR-005）。
@app.api_route("/health", methods=["GET", "HEAD"])
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


@app.post("/jobs/{job_name}")
async def trigger_job(job_name: str, request: Request, background: BackgroundTasks):
    token = os.environ.get("JOB_TRIGGER_TOKEN", "")
    if not token:
        raise HTTPException(status_code=404, detail="Job trigger disabled")
    if not hmac.compare_digest(request.headers.get("X-Job-Token", ""), token):
        raise HTTPException(status_code=401, detail="Invalid token")

    if job_name == "collect":
        from collector.agent import run
    elif job_name == "monitor":
        from monitor.agent import run
    elif job_name == "digest":
        from monitor.digest import run
    else:
        raise HTTPException(status_code=404, detail="Unknown job")

    background.add_task(run)
    logger.info("Job %s triggered", job_name)
    return JSONResponse({"status": "accepted", "job": job_name})


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
