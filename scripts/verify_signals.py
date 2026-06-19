"""シグナル検証（デイトレ有効性のベースライン確定／複数日横断・時価総額分離・分割ガード）

仮説「AIが拾った銘柄は出来高が跳ね、十分な値幅が出る（＝旬になる）」を検証する。
正誤（上下）ではなく、デイトレで獲れる動きだったかを次の2軸で見る:

  ① 出来高スパイク: 当日出来高 ÷ 前日出来高、および ÷ 直近5営業日平均
  ② 日中値幅(True Range%): (高値 − 安値) ÷ 前日終値 × 100

設計の要点:
- 【2日window】開示は15:00以降が多く、引け後開示は翌朝に窓を開けて反応する。材料日Dと
  翌営業日D+1の2セッションのうち、出来高スパイク(前日比)が大きい方を「反応日(旬の日)」に採る。
- 【複数日横断】--from〜--to の材料日(記事取得日)を横断集計し、ポジ/ネガ十分なサンプルを確保。
- 【時価総額分離】TOPIX Core30相当の超大型株を「大型」、他を「中小型」に分離。大型は材料が良くても
  デイトレサイズでは動かずノイズになるため、中小型セグメントの真の実力値を見る。
- 【分割ガード】adjclose/close の調整係数が反応日と前日で大きく変わる銘柄は株式分割の調整ズレ
  （偽のギャップ）とみなし統計から除外する。配当調整は<1%、分割は大きく動くため閾値3%で判定。

実行（読み取りのみ・DB書き込みなし）:
  python -m scripts.verify_signals --from 2026-06-10 --to 2026-06-18
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
_SLEEP = 1.5
_SPLIT_THRESHOLD = 0.03  # adjclose/close 係数の段差がこれ以上なら分割疑いで除外

# TOPIX Core30 相当の超大型株（デイトレで動きにくいノイズとして分離する）。
# marketCap API が使えない代替の簡易リスト。完全網羅ではない（floor として運用）。
_LARGE_CAP = {
    "2914", "3382", "4063", "4502", "4503", "4519", "4543", "4568", "4661", "6098",
    "6146", "6273", "6367", "6501", "6503", "6594", "6702", "6752", "6758", "6861",
    "6902", "6954", "6981", "7011", "7203", "7267", "7741", "7751", "7974", "8001",
    "8031", "8035", "8053", "8058", "8306", "8316", "8411", "8591", "8604", "8725",
    "8766", "8801", "9020", "9022", "9432", "9433", "9434", "9983", "9984",
}


def _pct(a, b):
    return (a - b) / b * 100 if (a is not None and b) else None


def _fetch_bars(symbol: str, start: datetime, end: datetime) -> list:
    """[start,end] を覆う日足を昇順で返す。close/adjclose/OHLCV を含む。失敗時 []。"""
    p1 = int((start - timedelta(days=10)).timestamp())
    p2 = int((end + timedelta(days=4)).timestamp())
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
        adj = ((res0.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
        o, h, l, c, v = (q.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
        bars = []
        for i in range(len(ts)):
            if i >= len(c) or c[i] is None:
                continue
            bars.append({
                "date": datetime.fromtimestamp(ts[i], JST).strftime("%Y-%m-%d"),
                "open": o[i] if i < len(o) else None, "high": h[i] if i < len(h) else None,
                "low": l[i] if i < len(l) else None, "close": c[i],
                "adj": adj[i] if i < len(adj) else None,
                "volume": v[i] if i < len(v) else None,
            })
        return bars
    except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError):
        return []


def _settled(date_str: str) -> bool:
    now = datetime.now(JST)
    if date_str != now.strftime("%Y-%m-%d"):
        return True
    return now.hour > 15 or (now.hour == 15 and now.minute >= 10)


def _split_suspected(bars: list, j: int) -> bool:
    """bars[j] と bars[j-1] の adjclose/close 係数に大きな段差→分割の調整ズレ。"""
    a, b = bars[j], bars[j - 1]
    if not (a.get("adj") and a["close"] and b.get("adj") and b["close"]):
        return False
    fa, fb = a["adj"] / a["close"], b["adj"] / b["close"]
    return fb != 0 and abs(fa / fb - 1) > _SPLIT_THRESHOLD


def _day_metrics(bars: list, j: int) -> dict | None:
    if j <= 0 or j >= len(bars):
        return None
    d, p = bars[j], bars[j - 1]
    if not _settled(d["date"]):
        return None
    vols5 = [b["volume"] for b in bars[max(0, j - 5):j] if b["volume"]]
    pc = p["close"]
    return {
        "date": d["date"], "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "prev_close": pc, "split": _split_suspected(bars, j),
        "gap": _pct(d["open"], pc), "ret": _pct(d["close"], pc),
        "range": (d["high"] - d["low"]) / pc * 100 if (d["high"] and d["low"] and pc) else None,
        "spike_prev": d["volume"] / p["volume"] if (d["volume"] and p["volume"]) else None,
        "spike_5d": d["volume"] / mean(vols5) if (d["volume"] and vols5) else None,
    }


def _reaction_day(bars: list, news_day: str) -> dict | None:
    """材料日D(=news_day以降の最初の営業日)とD+1のうち、前日比スパイクが大きい方。"""
    idx = next((i for i, b in enumerate(bars) if b["date"] >= news_day), None)
    if idx is None:
        return None
    cands = [m for m in (_day_metrics(bars, idx), _day_metrics(bars, idx + 1)) if m]
    if not cands:
        return None
    return max(cands, key=lambda m: (m["spike_prev"] or 0))


def _summarize(label: str, events: list) -> None:
    """events: [{sentiment, m, ...}] のうち分割除外後を集計して出力。"""
    ev = [e for e in events if not e["m"]["split"]]
    n_split = len(events) - len(ev)
    if not ev:
        logger.info("  %s: 有効データなし（分割除外 %d）", label, n_split)
        return
    sp = [e["m"]["spike_prev"] for e in ev if e["m"]["spike_prev"]]
    rg = [e["m"]["range"] for e in ev if e["m"]["range"] is not None]
    parts = [f"{label}: n={len(ev)}"]
    if n_split:
        parts.append(f"(分割除外{n_split})")
    if sp:
        parts.append(f"出来高×前日 中央{median(sp):.1f}/平均{mean(sp):.1f}/2倍以上{sum(1 for x in sp if x>=2)}件")
    if rg:
        parts.append(f"値幅 中央{median(rg):.1f}%/平均{mean(rg):.1f}%/5%以上{sum(1 for x in rg if x>=5)}件({100*sum(1 for x in rg if x>=5)/len(rg):.0f}%)")
    logger.info("  " + " | ".join(parts))


def run(date_from: str, date_to: str) -> None:
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=JST)
    end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=JST)

    rows = execute(
        """
        SELECT s.sentiment, s.stocks, s.impact, date(a.fetched_at,'+9 hours') AS news_day
        FROM signals s JOIN articles a ON a.id = s.article_id
        WHERE date(a.fetched_at,'+9 hours') BETWEEN ? AND ?
        ORDER BY news_day
        """,
        (date_from, date_to),
    ).rows
    # (code, news_day) 単位のイベントに展開（同日同コードは1件）
    events = {}
    for r in rows:
        for st in (json.loads(r["stocks"]) if r["stocks"] else []):
            code = str(st.get("code", "")).strip()
            if not code:
                continue
            key = (code, r["news_day"])
            if key not in events:
                events[key] = {"code": code, "name": st.get("name", ""),
                               "sentiment": r["sentiment"], "news_day": r["news_day"],
                               "impact": r["impact"]}
    codes = sorted({e["code"] for e in events.values()})
    logger.info("期間 %s〜%s ／ イベント %d件 ／ ユニーク銘柄 %d件\n", date_from, date_to, len(events), len(codes))

    # 日経（地合い参考、反応日の騰落を引く用）
    nk_bars = _fetch_bars("%5EN225", start, end)
    nk = {nk_bars[i]["date"]: _pct(nk_bars[i]["close"], nk_bars[i - 1]["close"]) for i in range(1, len(nk_bars))}
    time.sleep(_SLEEP)

    # 銘柄ごとに1回だけ広域取得してキャッシュ
    cache = {}
    for i, code in enumerate(codes, 1):
        cache[code] = _fetch_bars(f"{code}.T", start, end)
        time.sleep(_SLEEP)
        if i % 25 == 0:
            logger.info("... 取得 %d/%d銘柄", i, len(codes))

    # 各イベントを評価
    notable = []          # 旬（値幅5%以上 かつ 出来高1.5倍以上、分割除外）
    buckets = {("positive", "中小型"): [], ("positive", "大型"): [],
               ("negative", "中小型"): [], ("negative", "大型"): []}
    no_data = 0
    for e in events.values():
        bars = cache.get(e["code"]) or []
        m = _reaction_day(bars, e["news_day"]) if bars else None
        if m is None:
            no_data += 1
            continue
        seg = "大型" if e["code"] in _LARGE_CAP else "中小型"
        rec = {**e, "m": m, "seg": seg}
        buckets[(e["sentiment"], seg)].append(rec)
        if (not m["split"]) and m["range"] is not None and m["range"] >= 5 and (m["spike_prev"] or 0) >= 1.5:
            notable.append(rec)

    # 旬銘柄リスト（値幅降順）
    logger.info("=== 旬だった銘柄（値幅5%以上 & 出来高1.5倍以上 / 反応日） ===")
    logger.info(f"{'銘柄':<20}{'判定':<5}{'区分':<5}{'材料日':>11}{'反応日':>11}{'ギャップ':>8}{'当日%':>7}{'値幅%':>7}{'×前日':>7}{'日経%':>7}")
    for e in sorted(notable, key=lambda x: -(x["m"]["range"] or 0)):
        m = e["m"]
        name = e["name"][:9] + "(" + e["code"] + ")"
        gap_s = f"{m['gap']:+.1f}" if m["gap"] is not None else "－"
        ret_s = f"{m['ret']:+.1f}" if m["ret"] is not None else "－"
        nkv = nk.get(m["date"])
        nk_s = f"{nkv:+.1f}" if nkv is not None else "－"
        logger.info(
            f"{name:<20}{e['sentiment'][:4]:<5}{e['seg']:<5}{e['news_day']:>11}{m['date']:>11}"
            f"{gap_s:>8}{ret_s:>7}{m['range']:>6.1f}%{m['spike_prev']:>6.1f}x{nk_s:>7}"
        )

    logger.info("\n=== ベースライン（反応日・分割除外後） ===")
    for sent, label in (("positive", "ポジ"), ("negative", "ネガ")):
        logger.info("[%s]", label)
        _summarize(f"  中小型", buckets[(sent, "中小型")])
        _summarize(f"  大型  ", buckets[(sent, "大型")])
        _summarize(f"  全体  ", buckets[(sent, "中小型")] + buckets[(sent, "大型")])
    if no_data:
        logger.info("\n（データ取得不可/未確定: %d件）", no_data)

    # 効果測定: 中小型ポジ を インパクト帯で分割（ベースライン＝中小型ポジ全体 値幅5%超28%）
    logger.info("\n=== 効果測定: 中小型ポジ × インパクト帯（ベースライン 値幅5%超=28%）===")
    smallcap_pos = buckets[("positive", "中小型")]
    tiers = [
        ("インパクト4〜5（最優先通知対象）", lambda i: i is not None and i >= 4),
        ("インパクト1〜3", lambda i: i is not None and i <= 3),
        ("impact未設定（NULL）", lambda i: i is None),
    ]
    for label, pred in tiers:
        _summarize(label, [e for e in smallcap_pos if pred(e.get("impact"))])
    # ネガも参考表示
    logger.info("--- 参考: 中小型ネガ × インパクト帯 ---")
    smallcap_neg = buckets[("negative", "中小型")]
    for label, pred in tiers[:2]:
        _summarize(label, [e for e in smallcap_neg if pred(e.get("impact"))])

    logger.info("\n仮説検証: 『インパクト4〜5・中小型ポジ』の値幅5%超ヒット率が"
                "ベースライン28%を上回れば、スコアによる選別が有効。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="シグナルのデイトレ有効性を検証（複数日・時価総額分離・分割ガード）")
    ap.add_argument("--from", dest="date_from", required=True, help="材料日の開始 YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", required=True, help="材料日の終了 YYYY-MM-DD")
    args = ap.parse_args()
    run(args.date_from, args.date_to)
