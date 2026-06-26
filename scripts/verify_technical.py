"""テクニカル版 picks の精度計測（ノイズ率／旬ヒット率／売買代金フロアの効き目）

ステップ2「計測基盤」。今回追加した出来高ランキング探索＋売買代金フロアが、
「薄商いの空振り」を増やしていないか・拾った銘柄がちゃんと動いたかを定量で見る。

評価軸（verify_signals.py の計測関数を再利用。読み取りのみ・DB書き込み無し）:
  - 反応日: 配信日D（technical_runs.run_at のJST日付）とD+1のうち前日比スパイクが大きい方。
    テクニカル版は寄り前配信なので「その日ちゃんと旬になったか」を見る。
  - 旬ヒット: 反応日の日中値幅(TR%)≥5% かつ 出来高×前日≥1.5（verify_signals の「旬」定義に合わせる）。
  - ノイズ(空振り): 反応日の値幅<3% かつ 出来高×前日<1.5（ほぼ動かず＝薄商いの誤検知）。
  - セグメント: 探索元(up/vol) と 売買代金バケット で分け、フロアの効き目と出来高源の質を見る。

実行（読み取りのみ）:
  python -m scripts.verify_technical --from 2026-06-24 --to 2026-06-26
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from statistics import mean, median

from store.db import execute
from scripts.verify_signals import (
    JST, _SLEEP, _LARGE_CAP, _fetch_bars, _reaction_day,
)

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("verify_technical")

_HIT_RANGE = 5.0      # 旬ヒットの値幅(TR%)下限
_HIT_SPIKE = 1.5      # 旬ヒットの出来高×前日 下限
_NOISE_RANGE = 3.0    # ノイズ(空振り)判定: 値幅がこれ未満 かつ
_NOISE_SPIKE = 1.5    #                     出来高×前日がこれ未満


def _turnover_bucket(t) -> str:
    if t is None:
        return "不明"
    oku = t / 1e8
    if oku < 3:
        return "<3億"
    if oku < 10:
        return "3〜10億"
    return "≥10億"


def _is_hit(m: dict) -> bool:
    return (m.get("range") or 0) >= _HIT_RANGE and (m.get("spike_prev") or 0) >= _HIT_SPIKE


def _is_noise(m: dict) -> bool:
    return (m.get("range") or 0) < _NOISE_RANGE and (m.get("spike_prev") or 0) < _NOISE_SPIKE


def _summarize(label: str, evs: list) -> None:
    ev = [e for e in evs if e["m"] and not e["m"]["split"]]
    n_split = sum(1 for e in evs if e["m"] and e["m"]["split"])
    n_nodata = sum(1 for e in evs if not e["m"])
    if not ev:
        logger.info("  %s: 有効データなし（データ無%d/分割除外%d）", label, n_nodata, n_split)
        return
    n = len(ev)
    hits = sum(1 for e in ev if _is_hit(e["m"]))
    range_hits = sum(1 for e in ev if (e["m"].get("range") or 0) >= _HIT_RANGE)
    noise = sum(1 for e in ev if _is_noise(e["m"]))
    ranges = [e["m"]["range"] for e in ev if e["m"]["range"] is not None]
    spikes = [e["m"]["spike_prev"] for e in ev if e["m"]["spike_prev"]]
    parts = [
        f"{label}: n={n}",
        f"値幅≥{_HIT_RANGE:.0f}% {range_hits}件({100*range_hits/n:.0f}%)",
        f"旬ヒット(値幅&出来高) {hits}件({100*hits/n:.0f}%)",
        f"空振り {noise}件({100*noise/n:.0f}%)",
    ]
    if ranges:
        parts.append(f"値幅中央{median(ranges):.1f}%")
    if spikes:
        parts.append(f"出来高×前日中央{median(spikes):.1f}")
    extra = []
    if n_nodata:
        extra.append(f"データ無{n_nodata}")
    if n_split:
        extra.append(f"分割除外{n_split}")
    if extra:
        parts.append("(" + "/".join(extra) + ")")
    logger.info("  " + " | ".join(parts))


def run(date_from: str, date_to: str) -> None:
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=JST)
    end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=JST)

    # 配信日D = run_at のJST日付。picks のある run を対象にする。
    rows = execute(
        """
        SELECT date(run_at,'+9 hours') AS deliver_day, picks
        FROM technical_runs
        WHERE date(run_at,'+9 hours') BETWEEN ? AND ?
          AND picks IS NOT NULL AND pick_count > 0
        ORDER BY deliver_day
        """,
        (date_from, date_to),
    ).rows

    # (code, deliver_day) 単位に展開（同日同コードは1件）
    events: dict[tuple, dict] = {}
    for r in rows:
        for p in (json.loads(r["picks"]) if r["picks"] else []):
            code = str(p.get("code", "")).strip()
            if not code:
                continue
            key = (code, r["deliver_day"])
            events.setdefault(key, {
                "code": code, "name": p.get("name", ""),
                "deliver_day": r["deliver_day"],
                "source": p.get("source", "up"),
                "turnover": p.get("turnover"),
            })

    codes = sorted({e["code"] for e in events.values()})
    logger.info("期間 %s〜%s（配信日基準）／ pick %d件 ／ ユニーク銘柄 %d件\n",
                date_from, date_to, len(events), len(codes))
    if not events:
        logger.info("対象 picks がありません。")
        return

    cache: dict[str, list] = {}
    for i, code in enumerate(codes, 1):
        cache[code] = _fetch_bars(f"{code}.T", start, end)
        time.sleep(_SLEEP)
        if i % 25 == 0:
            logger.info("... 取得 %d/%d銘柄", i, len(codes))

    # 反応日メトリクスを各pickに付与
    all_ev = []
    for e in events.values():
        bars = cache.get(e["code"]) or []
        e["m"] = _reaction_day(bars, e["deliver_day"]) if bars else None
        e["seg"] = "大型" if e["code"] in _LARGE_CAP else "中小型"
        all_ev.append(e)

    logger.info("=== 全体 ===")
    _summarize("全picks", all_ev)
    logger.info("\n=== 探索元別 ===")
    _summarize("値上り源(up)", [e for e in all_ev if e["source"] == "up"])
    _summarize("出来高源(vol)", [e for e in all_ev if e["source"] == "vol"])
    logger.info("\n=== 売買代金バケット別（フロアの効き目） ===")
    for b in ("<3億", "3〜10億", "≥10億", "不明"):
        seg = [e for e in all_ev if _turnover_bucket(e["turnover"]) == b]
        if seg:
            _summarize(b, seg)
    logger.info(
        "\n判定基準: 旬ヒット=値幅≥%.0f%% かつ 出来高×前日≥%.1f ／ 空振り=値幅<%.0f%% かつ 出来高×前日<%.1f",
        _HIT_RANGE, _HIT_SPIKE, _NOISE_RANGE, _NOISE_SPIKE,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="テクニカル版 picks の精度計測")
    today = datetime.now(JST).strftime("%Y-%m-%d")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", default=today)
    args = ap.parse_args()
    run(args.date_from, args.date_to)


if __name__ == "__main__":
    main()
