"""シグナル検証（デイトレ有効性の事実確認）

仮説「AIが拾った銘柄は当日の出来高が跳ね、十分な値幅が出る（＝旬になる）」を検証する。
正誤（上下）ではなく、デイトレで獲れる動きだったかを次の2軸で見る:

  ① 出来高スパイク: 当日出来高 ÷ 前日出来高、および ÷ 直近5営業日平均
  ② 日中値幅(True Range%): (当日高値 − 当日安値) ÷ 前日終値 × 100

参考にギャップ%(始値 vs 前日終値)・当日リターン%・同日の日経平均騰落も併記し、
逆行ケースが地合い起因(B)か判定の読解ミス(A)かの切り分けの材料にする。

評価する「日」は材料が出た日 = 記事取得日(articles.fetched_at の JST 日付)のセッション。
（バックログ一括配信で notified_at が固まっているため、通知日ではなく材料日で見る）

実行（読み取りのみ・DB書き込みなし）:
  python -m scripts.verify_signals --date 2026-06-18
  python -m scripts.verify_signals --date 2026-06-18 --sentiment positive
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from statistics import mean, median

import httpx

from store.db import execute

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)  # 1リクエスト毎のINFOログを抑制
logger = logging.getLogger("verify_signals")

JST = timezone(timedelta(hours=9))
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +verify)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_SLEEP = 1.5  # Yahoo へのレート制限配慮


def _fetch_ohlcv(symbol: str, target: datetime):
    """target日(JST)までの日足OHLCVを取り、(target当日, 前日, 直近5日volume) を返す。

    戻り: dict | None。target当日の確定バーが無ければ None。
    """
    p1 = int((target - timedelta(days=20)).timestamp())
    p2 = int((target + timedelta(days=1, hours=23)).timestamp())
    try:
        r = httpx.get(_CHART.format(sym=symbol), params={"interval": "1d", "period1": p1, "period2": p2},
                      headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            return None
        res0 = res[0]
        ts = res0.get("timestamp") or []
        q = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
        o, h, l, c, v = (q.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
        bars = []
        for i in range(len(ts)):
            if i >= len(c) or c[i] is None:
                continue
            bars.append({
                "date": datetime.fromtimestamp(ts[i], JST).strftime("%Y-%m-%d"),
                "open": o[i] if i < len(o) else None,
                "high": h[i] if i < len(h) else None,
                "low": l[i] if i < len(l) else None,
                "close": c[i],
                "volume": v[i] if i < len(v) else None,
            })
    except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError):
        return None

    tgt = target.strftime("%Y-%m-%d")
    idx = next((i for i, b in enumerate(bars) if b["date"] == tgt), None)
    if idx is None or idx == 0:
        return None
    day = bars[idx]
    prev = bars[idx - 1]
    prev5 = bars[max(0, idx - 5):idx]
    vols5 = [b["volume"] for b in prev5 if b["volume"]]
    return {"day": day, "prev": prev, "avg5_vol": mean(vols5) if vols5 else None}


def _pct(a, b):
    return (a - b) / b * 100 if (a is not None and b) else None


def run(date_str: str, sentiment: str | None = None) -> None:
    target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)

    sql = """
        SELECT s.id, s.sentiment, s.summary, s.stocks
        FROM signals s JOIN articles a ON a.id = s.article_id
        WHERE date(a.fetched_at,'+9 hours') = ?
    """
    params = [date_str]
    if sentiment:
        sql += " AND s.sentiment = ?"
        params.append(sentiment)
    sql += " ORDER BY s.id"
    rows = execute(sql, tuple(params)).rows

    # (code,name,sentiment,summary) を銘柄単位に展開し重複コードは集約
    seen = {}
    for r in rows:
        for st in (json.loads(r["stocks"]) if r["stocks"] else []):
            code = str(st.get("code", "")).strip()
            if code and code not in seen:
                seen[code] = {"name": st.get("name", ""), "sentiment": r["sentiment"], "summary": r["summary"]}

    logger.info("対象: %s の材料日シグナル（%s）／ユニーク銘柄 %d件\n", date_str, sentiment or "全sentiment", len(seen))

    # 日経平均（地合い参考）
    nk = _fetch_ohlcv("%5EN225", target)
    nk_ret = _pct(nk["day"]["close"], nk["prev"]["close"]) if nk else None
    time.sleep(_SLEEP)

    header = f"{'銘柄':<22}{'判定':<5}{'前日終値':>9}{'始値':>9}{'終値':>9}{'ギャップ%':>9}{'当日%':>8}{'値幅%':>8}{'出来高×前日':>11}{'×5日平均':>10}"
    logger.info(header)
    logger.info("-" * len(header) * 2)

    spikes_prev, spikes_5d, ranges = [], [], []
    rows_out = []
    for code, info in seen.items():
        data = _fetch_ohlcv(f"{code}.T", target)
        time.sleep(_SLEEP)
        if data is None:
            logger.info(f"{info['name'][:10]}({code})  データ取得不可")
            continue
        d, p, a5 = data["day"], data["prev"], data["avg5_vol"]
        gap = _pct(d["open"], p["close"])
        day_ret = _pct(d["close"], p["close"])
        rng = (d["high"] - d["low"]) / p["close"] * 100 if (d["high"] and d["low"] and p["close"]) else None
        sx = d["volume"] / p["volume"] if (d["volume"] and p["volume"]) else None
        s5 = d["volume"] / a5 if (d["volume"] and a5) else None
        if sx: spikes_prev.append(sx)
        if s5: spikes_5d.append(s5)
        if rng is not None: ranges.append(rng)
        name = (info["name"][:10] + f"({code})")
        def f(x, suf="", nd=1):
            return (f"{x:+.{nd}f}{suf}" if x is not None else "－")
        logger.info(
            f"{name:<22}{info['sentiment'][:4]:<5}{p['close']:>9,.0f}{d['open']:>9,.0f}{d['close']:>9,.0f}"
            f"{f(gap):>9}{f(day_ret):>8}{(f'{rng:.1f}%' if rng is not None else '－'):>8}"
            f"{(f'{sx:.1f}x' if sx else '－'):>11}{(f'{s5:.1f}x' if s5 else '－'):>10}"
        )
        rows_out.append((name, info["sentiment"], sx, s5, rng, day_ret, info["summary"]))

    logger.info("\n=== サマリ（%s 材料日） ===", date_str)
    if nk_ret is not None:
        logger.info("日経平均 当日リターン: %+.2f%%（地合い参考）", nk_ret)
    if spikes_prev:
        logger.info("出来高×前日: 中央値 %.1fx / 平均 %.1fx / 2倍以上 %d/%d銘柄",
                    median(spikes_prev), mean(spikes_prev), sum(1 for x in spikes_prev if x >= 2), len(spikes_prev))
    if spikes_5d:
        logger.info("出来高×5日平均: 中央値 %.1fx / 2倍以上 %d/%d銘柄",
                    median(spikes_5d), sum(1 for x in spikes_5d if x >= 2), len(spikes_5d))
    if ranges:
        logger.info("日中値幅%%: 中央値 %.1f%% / 5%%以上 %d/%d銘柄",
                    median(ranges), sum(1 for x in ranges if x >= 5), len(ranges))
    logger.info("\n判定: 出来高が跳ね(×2以上)かつ値幅5%以上の銘柄が多いほど、AIの選別はデイトレ有効。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="シグナルのデイトレ有効性を出来高・値幅で検証")
    ap.add_argument("--date", required=True, help="材料日(記事取得日) YYYY-MM-DD")
    ap.add_argument("--sentiment", default=None, choices=["positive", "negative"], help="判定で絞る")
    args = ap.parse_args()
    run(args.date, args.sentiment)
