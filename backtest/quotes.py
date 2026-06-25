"""バックテスト用の日足OHLC取得（Yahoo Finance chart API）。

monitor/quotes.py と同じエンドポイント方針だが、こちらは始値(open)込みの
日足系列が必要なので別ヘルパを置く（値ごろ表示用の monitor 側は終値中心）。
取得失敗は空リストを返し、呼び出し側（エンジン）を止めない。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List

import httpx

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +backtest)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


def fetch_ohlc(code: str, start_jst: str, end_jst: str) -> List[dict]:
    """[start_jst, end_jst] (JST 'YYYY-MM-DD') の日足を昇順 [{date,open,close}] で返す。

    場中(15:10 JST前)の当日未確定足は除外する。取得失敗時は空リスト。
    """
    code = (code or "").strip()
    if not code:
        return []
    try:
        p1 = int(datetime.strptime(start_jst, "%Y-%m-%d").replace(tzinfo=JST).timestamp())
        p2 = int((datetime.strptime(end_jst, "%Y-%m-%d").replace(tzinfo=JST)
                  + timedelta(days=1)).timestamp())
    except (ValueError, TypeError):
        return []
    try:
        resp = httpx.get(
            _CHART_URL.format(code=code),
            params={"interval": "1d", "period1": p1, "period2": p2},
            headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning("ohlc %s: HTTP %s", code, resp.status_code)
            return []
        data = resp.json()
    except (httpx.RequestError, ValueError) as exc:
        logger.warning("ohlc %s: failed (%s)", code, exc)
        return []

    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return []
    res0 = result[0]
    ts = res0.get("timestamp") or []
    quote = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    closes = quote.get("close") or []
    bars: List[dict] = []
    for i, t in enumerate(ts):
        o = opens[i] if i < len(opens) else None
        c = closes[i] if i < len(closes) else None
        if o is None or c is None:
            continue
        bars.append({
            "date": datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d"),
            "open": float(o), "close": float(c),
        })
    return _drop_unsettled_today(bars)


def _drop_unsettled_today(bars: List[dict]) -> List[dict]:
    """東証クローズ(15:10 JST)前は当日の未確定足を落とす（暫定値を確定終値にしない）。"""
    now = datetime.now(JST)
    if now.hour > 15 or (now.hour == 15 and now.minute >= 10):
        return bars
    today = now.strftime("%Y-%m-%d")
    return [b for b in bars if b["date"] != today]
