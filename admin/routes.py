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
# 株価フィルタの直近入力値を記憶するCookie（次回クエリ無しアクセス時に復元）
_PRICE_MIN_COOKIE = "flt_price_min"
_PRICE_MAX_COOKIE = "flt_price_max"
_PER_PAGE = 50
_PER_PAGE_OPTIONS = [20, 50, 100, 200]
_DASHBOARD_DAYS = 3  # ダッシュボードのパイプライン統計の表示日数

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


def _to_float(value: str):
    """フィルタ入力文字列を float に変換する。空・不正値は None（＝フィルタ無効）。"""
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


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
async def dashboard(request: Request, msg: str = None, stats_page: int = 0):
    if not _is_authed(request):
        return _login_redirect()

    stats_page = max(0, stats_page)

    from store import analyses, digest_runs, signals
    daily_stats, has_older = analyses.get_stats_recent_days(_DASHBOARD_DAYS, page=stats_page)

    # ダッシュボードは直近サマリだけ。全件は /admin/digest-log で見る。
    runs = digest_runs.get_recent(3)
    for run in runs:
        run["run_at_jst"] = _to_jst_time(run.get("run_at", "") or "")
        # 配信したシグナルの内容（銘柄・要約・URL）を復元して展開表示できるようにする
        run["signals"] = signals.get_by_ids(run.get("notified_ids") or [])

    # 捕捉率フィードバック（読み取りのみ。テーブル未作成・取得失敗でもダッシュボードを壊さない）
    # ダッシュボードは直近3営業日だけ。全件は /admin/coverage で見る。
    coverage = []
    try:
        from store import coverage_runs
        # 同一営業日に feedback が複数回走ると行が重複するため、対象日ごと最新1件だけ表示する。
        # get_recent は ranking_date DESC, id DESC 順なので、各日の先頭＝最新を採用。
        seen_dates = set()
        for c in coverage_runs.get_recent(60):
            d = c.get("ranking_date")
            if d in seen_dates:
                continue
            seen_dates.add(d)
            coverage.append(c)
            if len(coverage) >= 3:
                break
    except Exception:
        logger.warning("coverage_runs unavailable; skip section", exc_info=True)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "daily_stats": daily_stats,
        "stats_page": stats_page,
        "has_older": has_older,
        "runs": runs,
        "coverage": coverage,
        "msg": msg,
        "pipeline_running": _pipeline_running,
    })


_LOG_PER_PAGE = 20  # 専用ページ（配信ログ・捕捉率）の1ページ件数


@router.get("/digest-log", response_class=HTMLResponse)
async def digest_log_view(request: Request, page: int = 1):
    """配信ログ専用ページ。20件/ページで prev/next ページャ。"""
    if not _is_authed(request):
        return _login_redirect()

    page = max(1, page)
    from store import digest_runs, signals
    runs, has_next = digest_runs.get_page(_LOG_PER_PAGE, (page - 1) * _LOG_PER_PAGE)
    for run in runs:
        run["run_at_jst"] = _to_jst_time(run.get("run_at", "") or "")
        run["signals"] = signals.get_by_ids(run.get("notified_ids") or [])

    return templates.TemplateResponse("digest_log.html", {
        "request": request,
        "runs": runs,
        "page": page,
        "has_next": has_next,
    })


@router.get("/coverage", response_class=HTMLResponse)
async def coverage_view(request: Request, page: int = 1):
    """捕捉率フィードバック専用ページ。20営業日/ページで prev/next ページャ。"""
    if not _is_authed(request):
        return _login_redirect()

    page = max(1, page)
    coverage = []
    has_next = False
    try:
        from store import coverage_runs
        coverage, has_next = coverage_runs.get_page(_LOG_PER_PAGE, (page - 1) * _LOG_PER_PAGE)
    except Exception:
        logger.warning("coverage_runs unavailable; skip section", exc_info=True)

    return templates.TemplateResponse("coverage.html", {
        "request": request,
        "coverage": coverage,
        "page": page,
        "has_next": has_next,
    })


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_view(request: Request):
    """元本シミュレーション: 実配信シグナルを各ルールで約定した場合の元本推移を表示する。

    引け後 cron（python -m backtest.agent）が記録した最新スナップショットを読むだけ。
    Yahoo は叩かない（高速）。テーブル未作成・記録なしでも画面を壊さない。
    """
    if not _is_authed(request):
        return _login_redirect()

    snapshot = None
    run_at_jst = ""
    try:
        from store import backtest_runs
        latest = backtest_runs.get_latest()
        if latest:
            snapshot = latest.get("snapshot")
            run_at_jst = _to_jst_time(latest.get("run_at", "") or "")
    except Exception:
        logger.warning("backtest_runs unavailable; skip section", exc_info=True)

    return templates.TemplateResponse("backtest.html", {
        "request": request,
        "snapshot": snapshot,
        "run_at_jst": run_at_jst,
    })


@router.get("/articles", response_class=HTMLResponse)
async def articles_view(
    request: Request,
    date: str = "",
    year: str = "",
    sentiment: str = "",
    code: str = "",
    notified: str = "",
    price_min: str = "",
    price_max: str = "",
    page: int = 1,
    per_page: int = _PER_PAGE,
):
    if not _is_authed(request):
        return _login_redirect()

    # クエリ無し（ブックマーク/メニューからの素のアクセス）なら株価フィルタを前回値で復元。
    # フォーム送信やページャ経由はクエリが付くので、その場合は明示値（空=クリア）を尊重する。
    if not request.query_params:
        price_min = request.cookies.get(_PRICE_MIN_COOKIE, "")
        price_max = request.cookies.get(_PRICE_MAX_COOKIE, "")

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
    notified_filter = "yes" if notified == "yes" else None
    pmin, pmax = _to_float(price_min), _to_float(price_max)
    rows, has_next = analyses.get_articles(
        date_str=date or None,
        year=year_filter,
        sentiment=sentiment or None,
        code=code or None,
        notified=notified_filter,
        price_min=pmin,
        price_max=pmax,
        limit=per_page,
        offset=offset,
    )
    total = analyses.count_articles(
        date_str=date or None,
        year=year_filter,
        sentiment=sentiment or None,
        code=code or None,
        notified=notified_filter,
        price_min=pmin,
        price_max=pmax,
    )
    # 表示レンジ（rowsが0件なら0〜0。範囲外ページでもendが暴走しないよう保護）
    start = offset + 1 if rows else 0
    end = offset + len(rows) if rows else 0

    for row in rows:
        row["fetched_at_jst"] = _to_jst_time(row.get("fetched_at", "") or "")

    resp = templates.TemplateResponse("articles.html", {
        "request": request,
        "date": date,
        "year": year,
        "available_years": available_years,
        "sentiment": sentiment,
        "code": code,
        "notified": notified,
        "price_min": price_min,
        "price_max": price_max,
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
    # 今回の株価フィルタ値を記憶（空送信=クリアもそのまま空で保存）
    resp.set_cookie(_PRICE_MIN_COOKIE, price_min, max_age=_COOKIE_MAX_AGE, httponly=True, samesite="lax")
    resp.set_cookie(_PRICE_MAX_COOKIE, price_max, max_age=_COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return resp


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request, msg: str = None, err: str = None):
    if not _is_authed(request):
        return _login_redirect()

    from store import settings as cfg
    current = cfg.get_all()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": current,
        "msg": msg,
        "err": err,
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
    max_articles_per_run: str = Form("200"),
    tdnet_title_prefilter_enabled: str = Form(None),
    tdnet_title_prefilter_denylist: str = Form(""),
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
        "max_articles_per_run": max_articles_per_run.strip() or "200",
        "tdnet_title_prefilter_enabled": "true" if tdnet_title_prefilter_enabled else "false",
        "tdnet_title_prefilter_denylist": tdnet_title_prefilter_denylist.strip(),
    })

    return RedirectResponse("/admin/settings?msg=設定を保存しました", status_code=303)


# テクニカル設定の数値項目: key -> (型, 最小, 最大, ラベル)。範囲外・型不正は保存を中止する。
_TECH_NUMERIC_SPECS = [
    ("tech_min_change_pct", float, 0.0, 100.0, "値上がり率の下限(%)"),
    ("tech_min_volume_surge", float, 0.0, 100.0, "出来高急増倍率の下限(倍)"),
    ("tech_top_n", int, 1, 100, "ランキング上位件数"),
    ("tech_min_turnover_oku", float, 0.0, 10000.0, "売買代金フロア(億円)"),
    ("tech_vol_min_change_pct", float, 0.0, 100.0, "出来高源の値上がり率下限(%)"),
]
_TECH_BOOL_KEYS = ["tech_scan_volume", "tech_dedup_with_news"]


@router.post("/settings/technical")
async def settings_technical_save(request: Request):
    """テクニカル版の閾値を保存する。DB直書きを廃し、型/範囲を検証してから永続化する。

    既存の Gemini/収集設定とは独立フォーム。1項目でも不正なら全体を保存せず（アトミック）、
    エラー内容を flash で返す。サイレントな誤チューニング（桁間違い等）を弾くのが目的。
    """
    if not _is_authed(request):
        return _login_redirect()

    form = await request.form()
    parsed: dict[str, str] = {}
    errors: list[str] = []

    for key, caster, lo, hi, label in _TECH_NUMERIC_SPECS:
        raw = (form.get(key) or "").strip().replace(",", "")
        try:
            val = caster(raw)
        except (ValueError, TypeError):
            errors.append(f"{label}=「{raw or '空'}」は{'整数' if caster is int else '数値'}で入力してください")
            continue
        if not (lo <= val <= hi):
            errors.append(f"{label}=「{val}」は {lo}〜{hi} の範囲で入力してください")
            continue
        parsed[key] = str(val)

    # チェックボックス（未チェックは送信されないので false 固定で確実に反映する）
    for key in _TECH_BOOL_KEYS:
        parsed[key] = "true" if form.get(key) else "false"

    if errors:
        return RedirectResponse(
            "/admin/settings?err=保存しませんでした（" + " / ".join(errors) + "）",
            status_code=303,
        )

    from store import settings as cfg
    cfg.save_all(parsed)
    return RedirectResponse("/admin/settings?msg=テクニカル設定を保存しました", status_code=303)


@router.post("/settings/edinet")
async def settings_edinet_save(
    request: Request,
    edinet_enabled: str = Form(None),
    edinet_new_only: str = Form(None),
    edinet_filer_denylist: str = Form(""),
):
    """EDINET大量保有アラートの設定を保存する（別レーン。キー設定後に有効化）。"""
    if not _is_authed(request):
        return _login_redirect()

    from store import settings as cfg
    cfg.save_all({
        "edinet_enabled": "true" if edinet_enabled else "false",
        "edinet_new_only": "true" if edinet_new_only else "false",
        "edinet_filer_denylist": edinet_filer_denylist.strip(),
    })
    return RedirectResponse("/admin/settings?msg=EDINET設定を保存しました", status_code=303)


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
