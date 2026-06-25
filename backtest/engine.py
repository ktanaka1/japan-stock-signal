"""元本シミュレーションのエンジン（純粋計算 ＋ signals読み込み）。

実配信シグナル(notified_at済み・impact≥4)を銘柄×asofで集約し、各取引ルールで
約定したと仮定して元本の日次推移(エクイティカーブ)を計算する。

数値の出どころは「実シグナル × 実株価(日足OHLC)」のみ。前提は元本¥1,000,000・手数料往復0.2%。
compute() は ohlc_by_code を引数に取る純関数（テスト容易）。データ取得は agent.py が担う。
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta, timezone
from typing import Dict, List, Optional

from store.db import execute

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

START_CAPITAL = 1_000_000
FEE = 0.001              # 片道0.1%（往復0.2%）
GAP_MIN = 0.01           # 逆張り対象のギャップアップ閾値（+1%以上）
TAKE_PROFIT = 0.05       # 陰線利確: 始値→終値が -5%以下
STOP_LINE = 0.07         # 損切りライン: 引け値で建値比 -7%割れ
MAX_HOLD = 5             # 最大保有営業日

# 表示順・色（admin の折れ線と凡例で使う）
RULES = [
    {"key": "naive_long",      "label": "① 順張り（寄り買い→引け売り）", "color": "#F2555A"},
    {"key": "fade_sameday",    "label": "② 逆張り・当日引け（成り売り）", "color": "#E8B04B"},
    {"key": "fade_takeprofit", "label": "③ 逆張り＋陰線利確",            "color": "#6AA9FF"},
    {"key": "fade_stopline",   "label": "④ 逆張り＋損切りライン（最適）", "color": "#34D399"},
]


def load_positions() -> List[dict]:
    """配信済み(impact≥4)シグナルを銘柄×asofで集約したポジション一覧を返す（D1読み取りのみ）。"""
    rows = execute(
        """
        SELECT impact, stocks
        FROM signals
        WHERE notified_at IS NOT NULL AND impact >= 4
        ORDER BY id
        """
    ).rows
    groups: Dict[tuple, dict] = {}
    for row in rows:
        try:
            stocks = json.loads(row["stocks"]) if row.get("stocks") else []
        except (ValueError, TypeError):
            continue
        for s in stocks:
            code = (s.get("code") or "").strip()
            asof = (s.get("asof") or "")[:10]
            if not code or not asof:
                continue
            key = (code, asof)
            g = groups.setdefault(key, {"code": code, "name": s.get("name") or code,
                                        "asof": asof, "impact": 0})
            g["impact"] = max(g["impact"], row.get("impact") or 0)
    return list(groups.values())


def _entry(bars: List[dict], asof: str):
    """建玉日(asofより後の最初の足)の index と前営業日終値を返す。無ければ None。"""
    idx = next((i for i, b in enumerate(bars) if b["date"] > asof), None)
    if not idx:  # None または 0（前日足が無い）
        return None
    prev_close = bars[idx - 1]["close"]
    if not prev_close or not bars[idx]["open"]:
        return None
    return idx, prev_close


def _trade(rule: str, bars: List[dict], idx: int, prev_close: float) -> Optional[dict]:
    """1ルール×1ポジションの約定結果 {entry_date, ret, hold, reason} を返す。対象外は None。"""
    o = bars[idx]["open"]
    gap = (o - prev_close) / prev_close

    if rule == "naive_long":
        c = bars[idx]["close"]
        return {"entry_date": bars[idx]["date"], "ret": (c - o) / o - 2 * FEE,
                "hold": 1, "reason": "当日引け"}

    if gap < GAP_MIN:  # 逆張り3ルールはGUのみ
        return None

    if rule == "fade_sameday":
        c = bars[idx]["close"]
        return {"entry_date": bars[idx]["date"], "ret": (o - c) / o - 2 * FEE,
                "hold": 1, "reason": "当日引け"}

    # fade_takeprofit / fade_stopline: 最大MAX_HOLD日、ルール別の手仕舞い
    use_stop = (rule == "fade_stopline")
    exit_price = reason = None
    held = 0
    for j in range(idx, min(idx + MAX_HOLD, len(bars))):
        bj = bars[j]
        held = j - idx + 1
        if not bj["open"]:
            continue
        cum = (o - bj["close"]) / o                    # ショート累積損益(引け)
        body = (bj["close"] - bj["open"]) / bj["open"]  # ローソク足body
        if use_stop and cum <= -STOP_LINE:
            exit_price, reason = bj["close"], "損切"
            break
        if body <= -TAKE_PROFIT:
            exit_price, reason = bj["close"], "利確"
            break
    if exit_price is None:
        last = bars[min(idx + MAX_HOLD - 1, len(bars) - 1)]
        exit_price, reason = last["close"], "時間切"
        held = min(MAX_HOLD, len(bars) - idx)
    return {"entry_date": bars[idx]["date"], "ret": (o - exit_price) / o - 2 * FEE,
            "hold": held, "reason": reason}


def _equity_by_day(trades: List[dict]) -> Dict[str, float]:
    """建玉日ごとに等配分・複利した『日付→約定後元本』を返す。"""
    by_day: Dict[str, list] = {}
    for t in trades:
        by_day.setdefault(t["entry_date"], []).append(t)
    cap = float(START_CAPITAL)
    out: Dict[str, float] = {}
    for d in sorted(by_day):
        picks = by_day[d]
        cap *= 1 + sum(t["ret"] for t in picks) / len(picks)
        out[d] = cap
    return out


def _md(date: str) -> str:
    """'2026-06-19' -> '6/19'。"""
    return f"{int(date[5:7])}/{int(date[8:10])}"


def compute(positions: List[dict], ohlc_by_code: Dict[str, List[dict]], as_of: str) -> dict:
    """全ルールのエクイティと④の明細を含む snapshot dict を返す（純関数）。"""
    # ルールごとのトレード一覧
    per_rule: Dict[str, list] = {r["key"]: [] for r in RULES}
    stopline_detail: List[dict] = []
    for g in positions:
        bars = ohlc_by_code.get(g["code"])
        if not bars or len(bars) < 2:
            continue
        e = _entry(bars, g["asof"])
        if not e:
            continue
        idx, prev_close = e
        gap_pct = round((bars[idx]["open"] - prev_close) / prev_close * 100, 1)
        for r in RULES:
            tr = _trade(r["key"], bars, idx, prev_close)
            if tr is None:
                continue
            per_rule[r["key"]].append(tr)
            if r["key"] == "fade_stopline":
                stopline_detail.append({
                    "entry": _md(tr["entry_date"]), "name": g["name"], "code": g["code"],
                    "gap": gap_pct, "hold": tr["hold"], "reason": tr["reason"],
                    "ret": round(tr["ret"] * 100, 1),
                })

    # 共通の日付軸（全ルールの建玉日の和集合）
    curves = {k: _equity_by_day(v) for k, v in per_rule.items()}
    all_dates = sorted({d for c in curves.values() for d in c})
    axis = ["開始"] + [_md(d) for d in all_dates]

    rules_out = []
    for r in RULES:
        trades = per_rule[r["key"]]
        curve = curves[r["key"]]
        series = [START_CAPITAL]
        cap = float(START_CAPITAL)
        for d in all_dates:
            if d in curve:
                cap = curve[d]
            series.append(round(cap))
        n = len(trades)
        wins = sum(1 for t in trades if t["ret"] > 0)
        rules_out.append({
            "key": r["key"], "label": r["label"], "color": r["color"],
            "series": series,
            "cap": round(cap),
            "ret_pct": round((cap / START_CAPITAL - 1) * 100, 1),
            "n": n,
            "win": round(wins / n * 100) if n else 0,
            "avg_pct": round(sum(t["ret"] for t in trades) / n * 100, 2) if n else 0.0,
        })

    return {
        "as_of": as_of,
        "start_capital": START_CAPITAL,
        "axis": axis,
        "rules": rules_out,
        "ledger": sorted(stopline_detail, key=lambda x: -x["ret"]),
        "universe": {"positions": len(positions),
                     "codes": len({g["code"] for g in positions})},
        "params": {"gap_min_pct": int(GAP_MIN * 100), "take_profit_pct": int(TAKE_PROFIT * 100),
                   "stop_line_pct": int(STOP_LINE * 100), "max_hold": MAX_HOLD,
                   "fee_round_pct": round(2 * FEE * 100, 1)},
    }
