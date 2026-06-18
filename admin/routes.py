"""管理画面ルート。ADMIN_TOKEN を HttpOnly Cookie に保存して認証する。"""
from __future__ import annotations

import hmac
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

_COOKIE_NAME = "admin_session"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30日
_PER_PAGE = 50
_PER_PAGE_OPTIONS = [20, 50, 100, 200]

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# 即時配信パイプラインの多重実行ガード。
# Render Free は単一プロセスなのでインメモリのフラグ＋ロックで十分。
_pipeline_lock = threading.Lock()
_pipeline_running = False


def _is_authed(request: Request) -> bool:
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        return False
    cookie = request.cookies.get(_COOKIE_NAME, "")
    return bool(cookie) and hmac.compare_digest(cookie, admin_token)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def _today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def _to_jst_time(fetched_at: str) -> str:
    if not fetched_at:
        return ""
    try:
        dt = datetime.strptime(fetched_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).strftime("%m/%d %H:%M")
    except Exception:
        return fetched_at[:16]


# --- 認証 ---


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, err: str = None):
    if not os.environ.get("ADMIN_TOKEN", ""):
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN not configured")
    if _is_authed(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "err": err})


@router.post("/login")
async def login_submit(request: Request, token: str = Form("")):
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN not configured")
    if not (token and hmac.compare_digest(token, admin_token)):
        return RedirectResponse("/admin/login?err=トークンが違います", status_code=303)

    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        _COOKIE_NAME,
        token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(_COOKIE_NAME)
    return resp


# --- 画面 ---


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, date: str = None, msg: str = None):
    if not _is_authed(request):
        return _login_redirect()
    if not date:
        date = _today_jst()

    from store import analyses, digest_runs
    stats = analyses.get_stats_by_date(date)

    runs = digest_runs.get_recent(30)
    for run in runs:
        run["run_at_jst"] = _to_jst_time(run.get("run_at", "") or "")

    return templates.TemplateResponse("index.html", {
        "request": request,
        "date": date,
        "stats": stats,
        "runs": runs,
        "msg": msg,
        "pipeline_running": _pipeline_running,
    })


@router.get("/articles", response_class=HTMLResponse)
async def articles_view(
    request: Request,
    date: str = "",
    year: str = "",
    sentiment: str = "",
    code: str = "",
    page: int = 1,
    per_page: int = _PER_PAGE,
):
    if not _is_authed(request):
        return _login_redirect()

    page = max(1, page)
    if per_page not in _PER_PAGE_OPTIONS:
        per_page = _PER_PAGE
    offset = (page - 1) * per_page

    # 年フィルタ: 未指定なら当年がデフォルト。"all" で全期間。
    # 特定日(date)を指定したときは年フィルタを無効にする（日付が優先）。
    current_year = datetime.now(JST).strftime("%Y")
    if not year:
        year = current_year
    year_filter = None if (date or year == "all") else year

    code = code.strip()
    from store import analyses
    available_years = analyses.get_available_years()
    if current_year not in available_years:
        available_years = [current_year] + available_years
    rows, has_next = analyses.get_articles(
        date_str=date or None,
        year=year_filter,
        sentiment=sentiment or None,
        code=code or None,
        limit=per_page,
        offset=offset,
    )
    total = analyses.count_articles(
        date_str=date or None,
        year=year_filter,
        sentiment=sentiment or None,
        code=code or None,
    )
    # 表示レンジ（rowsが0件なら0〜0。範囲外ページでもendが暴走しないよう保護）
    start = offset + 1 if rows else 0
    end = offset + len(rows) if rows else 0

    for row in rows:
        row["fetched_at_jst"] = _to_jst_time(row.get("fetched_at", "") or "")

    return templates.TemplateResponse("articles.html", {
        "request": request,
        "date": date,
        "year": year,
        "available_years": available_years,
        "sentiment": sentiment,
        "code": code,
        "page": page,
        "has_next": has_next,
        "per_page": per_page,
        "per_page_options": _PER_PAGE_OPTIONS,
        "offset": offset,
        "total": total,
        "start": start,
        "end": end,
        "articles": rows,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request, msg: str = None):
    if not _is_authed(request):
        return _login_redirect()

    from store import settings as cfg
    current = cfg.get_all()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": current,
        "msg": msg,
    })


@router.post("/settings")
async def settings_save(
    request: Request,
    gemini_model: str = Form(""),
    gemini_system_prompt: str = Form(""),
    sentiment_positive: str = Form(None),
    sentiment_negative: str = Form(None),
    signal_require_stocks: str = Form(None),
    rss_feeds: str = Form(""),
    batch_interval_seconds: str = Form("7"),
    max_consecutive_failures: str = Form("3"),
    body_max_chars: str = Form("4000"),
):
    if not _is_authed(request):
        return _login_redirect()

    sentiments = []
    if sentiment_positive:
        sentiments.append("positive")
    if sentiment_negative:
        sentiments.append("negative")

    from store import settings as cfg
    cfg.save_all({
        "gemini_model": gemini_model.strip() or "gemini-2.5-flash",
        "gemini_system_prompt": gemini_system_prompt.strip(),
        "signal_sentiments": ",".join(sentiments) if sentiments else "positive,negative",
        "signal_require_stocks": "true" if signal_require_stocks else "false",
        "rss_feeds": rss_feeds.strip(),
        "batch_interval_seconds": batch_interval_seconds.strip() or "7",
        "max_consecutive_failures": max_consecutive_failures.strip() or "3",
        "body_max_chars": body_max_chars.strip() or "4000",
    })

    return RedirectResponse("/admin/settings?msg=設定を保存しました", status_code=303)


@router.post("/notify")
async def notify_now(request: Request, background: BackgroundTasks):
    if not _is_authed(request):
        return _login_redirect()

    global _pipeline_running
    # 多重実行ガード: 既に走っていれば起動せず案内だけ返す。
    with _pipeline_lock:
        if _pipeline_running:
            logger.info("Immediate pipeline already running; skip duplicate trigger")
            return RedirectResponse(
                "/admin?msg=すでに実行中です。完了までお待ちください",
                status_code=303,
            )
        _pipeline_running = True

    background.add_task(_run_full_pipeline)
    logger.info("Immediate pipeline (collect→analyze→deliver) triggered from admin")

    return RedirectResponse(
        "/admin?msg=収集→分析→配信を開始しました（バックグラウンドで実行中）",
        status_code=303,
    )


def _run_full_pipeline() -> None:
    """本番の朝digestと同じ収集→分析→配信を直列実行する。"""
    from collector.agent import run as collect
    from monitor.agent import run as analyze
    from monitor.digest import run as deliver

    global _pipeline_running
    try:
        for step, fn in (("collect", collect), ("analyze", analyze), ("deliver", deliver)):
            try:
                fn()
            except Exception:
                logger.exception("Immediate pipeline step '%s' failed", step)
                if step == "deliver":
                    raise  # 配信失敗は致命的なので再送出（収集・分析の失敗は配信を妨げない）
    finally:
        # 成否に関わらず必ずフラグを解放する（次回起動を許可）。
        with _pipeline_lock:
            _pipeline_running = False
