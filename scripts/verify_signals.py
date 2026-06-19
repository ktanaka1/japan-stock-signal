"""シグナル検証（デイトレ有効性の事実確認・2日window版）

仮説「AIが拾った銘柄は出来高が跳ね、十分な値幅が出る（＝旬になる）」を検証する。
正誤（上下）ではなく、デイトレで獲れる動きだったかを次の2軸で見る:

  ① 出来高スパイク: 当日出来高 ÷ 前日出来高、および ÷ 直近5営業日平均
  ② 日中値幅(True Range%): (高値 − 安値) ÷ 前日終値 × 100

【2日window】開示は15:00以降が多く、引け後開示は翌朝に窓を開けて反応する。そこで
材料日(D)と翌営業日(D+1)の2セッションを取得し、**出来高スパイク(前日比)が大きい方の日**を
「旬の日（反応日）」として採用、その日のギャップ・値幅・OHLCを出力する。
（ザラ場開示=当日反応／引け後開示=翌日反応 の両方を取りこぼさない）

ポジ・ネガ両方を対象にし、サマリは sentiment 別に出す（ネガの方が値幅が出る、という
仮説も同時に検証できる）。評価日は材料日 = 記事取得日(articles.fetched_at の JST 日付)。

実行（読み取りのみ・DB書き込みなし）:
  python -m scripts.verify_signals --date 2026-06-17
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("verify_signals")

JST = timezone(timedelta(hours=9))
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +verify)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_SLEEP = 1.5  # Yahoo へのレート制限配慮


def _fetch_bars(symbol: str, target: datetime) -> list:
    """target日(JST)を含む前後の日足OHLCVを昇順リストで返す。失敗時は []。"""
    p1 = int((target - timedelta(days=20)).timestamp())
    p2 = int((target + timedelta(days=6)).timestamp())
    try:
        r = httpx.get(_CHART.format(sym=symbol), params={"interval": "1d", "period1": p1, "period2": p2},
                      headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return []
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            return []
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
        return bars
    except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError):
        return []


def _settled(date_str: str) -> bool:
    """その日付の日足が確定済みか（当日かつ15:10 JST前なら未確定とみなす）。"""
    now = datetime.now(JST)
    if date_str != now.strftime("%Y-%m-%d"):
        return True
    return now.hour > 15 or (now.hour == 15 and now.minute >= 10)


def _day_metrics(bars: list, j: int) -> dict | None:
    """bars[j] のセッション指標（スパイク・ギャップ・値幅・リターン）を組む。"""
    if j <= 0 or j >= len(bars):
        return None
    d, p = bars[j], bars[j - 1]
    if not _settled(d["date"]):
        return None
    vols5 = [b["volume"] for b in bars[max(0, j - 5):j] if b["volume"]]
    pc = p["close"]
    return {
        "date": d["date"], "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "prev_close": pc,
        "gap": _pct(d["open"], pc),
        "ret": _pct(d["close"], pc),
        "range": (d["high"] - d["low"]) / pc * 100 if (d["high"] and d["low"] and pc) else None,
        "spike_prev": d["volume"] / p["volume"] if (d["volume"] and p["volume"]) else None,
        "spike_5d": d["volume"] / mean(vols5) if (d["volume"] and vols5) else None,
    }


def _pct(a, b):
    return (a - b) / b * 100 if (a is not None and b) else None


def _reaction_day(bars: list, target: datetime) -> dict | None:
    """材料日D(=target以降の最初の営業日)と翌営業日D+1のうち、前日比スパイクが大きい方を返す。"""
    tgt = target.strftime("%Y-%m-%d")
    idx = next((i for i, b in enumerate(bars) if b["date"] >= tgt), None)
    if idx is None:
        return None
    cands = [m for m in (_day_metrics(bars, idx), _day_metrics(bars, idx + 1)) if m]
    if not cands:
        return None
    return max(cands, key=lambda m: (m["spike_prev"] or 0))


def _nikkei_returns(target: datetime) -> dict:
    bars = _fetch_bars("%5EN225", target)
    out = {}
    for j in range(1, len(bars)):
        out[bars[j]["date"]] = _pct(bars[j]["close"], bars[j - 1]["close"])
    return out


def _fmt(x, suf="", nd=1):
    return f"{x:+.{nd}f}{suf}" if x is not None else "－"


def run(date_str: str) -> None:
    target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
    rows = execute(
        """
        SELECT s.sentiment, s.summary, s.stocks
        FROM signals s JOIN articles a ON a.id = s.article_id
        WHERE date(a.fetched_at,'+9 hours') = ?
        ORDER BY s.id
        """,
        (date_str,),
    ).rows
    # 銘柄単位に展開、同一コードは初出のsentiment/summaryを採用
    seen = {}
    for r in rows:
        for st in (json.loads(r["stocks"]) if r["stocks"] else []):
            code = str(st.get("code", "")).strip()
            if code and code not in seen:
                seen[code] = {"name": st.get("name", ""), "sentiment": r["sentiment"], "summary": r["summary"]}

    logger.info("対象材料日: %s ／ ユニーク銘柄 %d件（2日window: D と D+1 の大スパイク日を採用）\n", date_str, len(seen))
    nk = _nikkei_returns(target)
    time.sleep(_SLEEP)

    header = (f"{'銘柄':<22}{'判定':<5}{'反応日':>6}{'前日終値':>9}{'始値':>9}{'終値':>9}"
              f"{'ギャップ':>8}{'当日%':>7}{'値幅%':>7}{'出来高×前':>9}{'×5日':>7}{'日経%':>7}")
    logger.info(header)
    logger.info("-" * 130)

    by_sent = {"positive": [], "negative": []}
    for code, info in seen.items():
        bars = _fetch_bars(f"{code}.T", target)
        time.sleep(_SLEEP)
        m = _reaction_day(bars, target) if bars else None
        if m is None:
            logger.info(f"{info['name'][:10]}({code})  データ取得不可/未確定")
            continue
        which = "D" if m["date"] == _first_trading(bars, target) else "D+1"
        nkret = nk.get(m["date"])
        name = info["name"][:10] + "(" + code + ")"
        rng_s = f"{m['range']:.1f}%" if m["range"] is not None else "－"
        sp_s = f"{m['spike_prev']:.1f}x" if m["spike_prev"] else "－"
        s5_s = f"{m['spike_5d']:.1f}x" if m["spike_5d"] else "－"
        logger.info(
            f"{name:<22}{info['sentiment'][:4]:<5}{which:>6}"
            f"{m['prev_close']:>9,.0f}{m['open']:>9,.0f}{m['close']:>9,.0f}"
            f"{_fmt(m['gap']):>8}{_fmt(m['ret']):>7}{rng_s:>7}{sp_s:>9}{s5_s:>7}{_fmt(nkret):>7}"
        )
        if info["sentiment"] in by_sent:
            by_sent[info["sentiment"]].append(m)

    for sent, label in (("positive", "ポジティブ"), ("negative", "ネガティブ")):
        ms = by_sent[sent]
        if not ms:
            continue
        sp = [m["spike_prev"] for m in ms if m["spike_prev"]]
        rg = [m["range"] for m in ms if m["range"] is not None]
        logger.info("\n=== サマリ: %s（%d銘柄／反応日ベース） ===", label, len(ms))
        if sp:
            logger.info("  出来高×前日: 中央値 %.1fx / 平均 %.1fx / 2倍以上 %d/%d",
                        median(sp), mean(sp), sum(1 for x in sp if x >= 2), len(sp))
        if rg:
            logger.info("  日中値幅%%: 中央値 %.1f%% / 5%%以上 %d/%d / 平均 %.1f%%",
                        median(rg), sum(1 for x in rg if x >= 5), len(rg), mean(rg))
    logger.info("\n判定: ネガ/ポジ別に『×2以上の出来高 & 値幅5%以上』の比率を比較。高い側がデイトレ向き。")


def _first_trading(bars: list, target: datetime) -> str:
    tgt = target.strftime("%Y-%m-%d")
    b = next((b for b in bars if b["date"] >= tgt), None)
    return b["date"] if b else ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="シグナルのデイトレ有効性を出来高・値幅で検証（2日window）")
    ap.add_argument("--date", required=True, help="材料日(記事取得日) YYYY-MM-DD")
    args = ap.parse_args()
    run(args.date)
