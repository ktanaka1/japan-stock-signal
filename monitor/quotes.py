"""株価取得モジュール（Yahoo Finance chart API）

  - 証券コード4桁に .T を付けて Yahoo Finance の chart エンドポイントを叩く
  - 直近2営業日の日足から 終値 / 前日比% / 出来高 を算出する
  - 取得失敗（通信エラー・4xx・パース不能・データ不足）は None を返し、
    呼び出し側（シグナル生成・配信）を止めない（株価はあくまで付加情報）
  - 新規依存を増やさないため既存の httpx を使う（collector/fetcher.py と同方針）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +quotes)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


class Quote:
    """1銘柄の株価スナップショット。"""

    __slots__ = ("close", "change_pct", "volume", "asof")

    def __init__(self, close: float, change_pct: Optional[float], volume: Optional[int], asof: str):
        self.close = close
        self.change_pct = change_pct
        self.volume = volume
        self.asof = asof


def get_quote(code: str) -> Optional[Quote]:
    """証券コードの直近終値・前日比%・出来高を返す。取得できなければ None。"""
    return _get_quote_and_status(code)[0]


def _get_quote_and_status(code: str) -> "tuple[Optional[Quote], Optional[int]]":
    """(Quote, HTTPステータス) を返す。Quote が無ければ None。

    ステータスは証券コードの実在判定に使う（404 = そのコードは Yahoo 上に存在しない）。
    通信エラー等でステータスが取れない場合は None（= 実在判定不能・transient）を返し、
    呼び出し側が「実在しない」と誤判定しないようにする。
    """
    code = (code or "").strip()
    if not code:
        return None, None
    url = _CHART_URL.format(code=code)
    try:
        resp = httpx.get(
            url,
            params={"interval": "1d", "range": "5d"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.RequestError as exc:
        logger.warning("quote %s: request failed (%s)", code, exc)
        return None, None
    if resp.status_code != 200:
        logger.warning("quote %s: HTTP %s", code, resp.status_code)
        return None, resp.status_code
    try:
        series = _drop_unsettled_today(_extract_series(resp.json()))
        return (_build_quote(series) if series else None), resp.status_code
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("quote %s: parse failed (%s)", code, exc)
        return None, resp.status_code


def enrich(stocks: List[dict]) -> None:
    """銘柄リストの各dictに close/change_pct/volume/asof をインプレースで付与する。

    取得できなかった銘柄にはキーを付けない（価格なしのまま）。
    Yahoo が 404 を返したコード（= 実在しない証券コード）には stock["_nonexistent"]=True を
    付ける（呼び出し側が捏造コードの除外に使う一時フラグ。保存前に取り除くこと）。
    404 以外の取得失敗（transient）では付けない＝正しい銘柄を誤って実在しない扱いにしない。
    """
    for stock in stocks:
        q, status = _get_quote_and_status(stock.get("code", ""))
        if status == 404:
            stock["_nonexistent"] = True
        if q is None:
            continue
        stock["close"] = q.close
        if q.change_pct is not None:
            stock["change_pct"] = q.change_pct
        if q.volume is not None:
            stock["volume"] = q.volume
        stock["asof"] = q.asof


def get_daily_metrics(code: str) -> Optional[dict]:
    """直近確定足の close / change_pct / volume と出来高急増倍率(surge) を返す。

    range=10d の日足を取り、_drop_unsettled_today で当日場中の未確定足を除く。
    早朝(寄り前)は最新確定足＝前営業日になるため、「前日の動意」をそのまま評価できる。
    surge = 最新確定足の出来高 / それ以前 最大4営業日の平均出来高。取得不能なら None。
    """
    code = (code or "").strip()
    if not code:
        return None
    try:
        resp = httpx.get(
            _CHART_URL.format(code=code),
            params={"interval": "1d", "range": "10d"},
            headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning("metrics %s: HTTP %s", code, resp.status_code)
            return None
        series = _drop_unsettled_today(_extract_series(resp.json()))
    except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("metrics %s: failed (%s)", code, exc)
        return None
    if len(series) < 2:
        return None
    last_close, last_vol, last_ts = series[-1]
    prev_close = series[-2][0]
    change_pct = round((last_close - prev_close) / prev_close * 100, 2) if prev_close else None
    prior_vols = [v for _, v, _ in series[:-1] if v is not None][-4:]
    surge = None
    if last_vol and prior_vols:
        avg = sum(prior_vols) / len(prior_vols)
        surge = round(last_vol / avg, 2) if avg else None
    asof = datetime.fromtimestamp(last_ts, JST).strftime("%Y-%m-%d") if last_ts else ""
    return {
        "close": round(float(last_close), 1),
        "change_pct": change_pct,
        "volume": int(last_vol) if last_vol is not None else None,
        "surge": surge,
        "asof": asof,
    }


def format_price(stock: dict) -> str:
    """enrich 済みの銘柄dictを配信向けの株価文字列に整形する。

    値ごろ感の判断に使う「直近終値（円）」のみを返す。価格が無ければ空文字。
    前日比%・出来高は『旬になる直前の値＝ノイズ』のため表示しない（インパクトスコアに全振り）。
    例: "2,850円"
    """
    close = stock.get("close")
    if close is None:
        return ""
    return f"{close:,.0f}円"


def get_quote_asof(code: str, date_str: str) -> Optional[Quote]:
    """指定日(JST, 'YYYY-MM-DD')時点の終値・前日比%・出来高を返す。

    その日が休場なら直前の営業日にフォールバックする（範囲内の最新営業日を採用）。
    既存シグナルへのバックフィル用。取得できなければ None。
    """
    code = (code or "").strip()
    if not code:
        return None
    try:
        target = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=JST)
    except (ValueError, TypeError):
        return None
    # 休場・連休を跨いでも直前営業日と更にその前日が取れるよう余裕をもって範囲を取る
    p1 = int((target - timedelta(days=12)).timestamp())
    p2 = int((target + timedelta(days=1, hours=23)).timestamp())
    try:
        resp = httpx.get(
            _CHART_URL.format(code=code),
            params={"interval": "1d", "period1": p1, "period2": p2},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning("quote(asof) %s @%s: HTTP %s", code, date_str, resp.status_code)
            return None
        series = _extract_series(resp.json())
    except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("quote(asof) %s @%s: failed (%s)", code, date_str, exc)
        return None

    # target日の終わり以前の最新営業日を採用。さらに当日が場中なら未確定バーを除く。
    cutoff = int((target + timedelta(days=1)).timestamp())
    upto = _drop_unsettled_today([s for s in series if s[2] is not None and s[2] < cutoff])
    if not upto:
        return None
    return _build_quote(upto)


def _parse_chart(data: dict) -> Optional[Quote]:
    """Yahoo chart レスポンスから直近2点の日足を取り出して Quote を組む。"""
    series = _extract_series(data)
    if not series:
        return None
    return _build_quote(series)


def _extract_series(data: dict) -> List[tuple]:
    """chart レスポンスを null 除去済みの (close, volume, ts) 昇順リストにする。"""
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return []
    res0 = result[0]
    timestamps = res0.get("timestamp") or []
    quote = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    series = []
    for i, c in enumerate(closes):
        if c is None:
            continue
        ts = timestamps[i] if i < len(timestamps) else None
        vol = volumes[i] if i < len(volumes) else None
        series.append((c, vol, ts))
    return series


def _drop_unsettled_today(series: List[tuple]) -> List[tuple]:
    """場中（東証クローズ前）は当日の途中経過バー（部分出来高・暫定値）を除く。

    Yahoo は取引時間中も当日分の日足バーを暫定値として返すため、これを終値として
    保存すると後場で値が動き不正確になる。15:10 JST 以降は当日バーを確定とみなし残す。
    """
    now = datetime.now(JST)
    settled = now.hour > 15 or (now.hour == 15 and now.minute >= 10)
    if settled:
        return series
    today = now.strftime("%Y-%m-%d")
    return [
        s for s in series
        if s[2] is None or datetime.fromtimestamp(s[2], JST).strftime("%Y-%m-%d") != today
    ]


def _build_quote(series: List[tuple]) -> Quote:
    """(close, volume, ts) 系列の末尾を終値、その1つ前を前日として Quote を組む。"""
    close, volume, ts = series[-1]
    change_pct = None
    if len(series) >= 2:
        prev_close = series[-2][0]
        if prev_close:
            change_pct = round((close - prev_close) / prev_close * 100, 2)
    asof = datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d") if ts is not None else ""
    return Quote(
        close=round(float(close), 1),
        change_pct=change_pct,
        volume=int(volume) if volume is not None else None,
        asof=asof,
    )
