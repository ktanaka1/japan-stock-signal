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

# 確定した成功基準（2026-06-26 ロック）。主指標は値幅(TR%)。
SC_RANGE_HIT_MIN = 70.0   # 主指標: 値上り可能性＝値幅≥5%率 (%) の下限
SC_NOISE_MAX = 15.0       # ノイズ: 空振り率 (%) の上限
SC_VOL_GAP_MAX = 15.0     # 出来高源: 値幅≥5%率が up源比 これ(pt)以内の劣化まで許容
SC_CAPTURE_MIN = 4.0      # 捕捉率: 合算捕捉率 (%) がこれ超で推移
SC_MIN_N = 10             # 有効pick がこれ未満なら小サンプルとして判定保留（誤アラート抑止）


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


def _stats(evs: list) -> dict | None:
    """分割除外後の有効picksの集計。有効0なら None。"""
    ev = [e for e in evs if e["m"] and not e["m"]["split"]]
    if not ev:
        return None
    n = len(ev)
    ranges = [e["m"]["range"] for e in ev if e["m"]["range"] is not None]
    spikes = [e["m"]["spike_prev"] for e in ev if e["m"]["spike_prev"]]
    return {
        "n": n,
        "range_hit_pct": 100 * sum(1 for e in ev if (e["m"].get("range") or 0) >= _HIT_RANGE) / n,
        "hit_pct": 100 * sum(1 for e in ev if _is_hit(e["m"])) / n,
        "noise_pct": 100 * sum(1 for e in ev if _is_noise(e["m"])) / n,
        "range_med": median(ranges) if ranges else None,
        "spike_med": median(spikes) if spikes else None,
        "n_split": sum(1 for e in evs if e["m"] and e["m"]["split"]),
        "n_nodata": sum(1 for e in evs if not e["m"]),
    }


def _summarize(label: str, evs: list) -> None:
    s = _stats(evs)
    if not s:
        n_split = sum(1 for e in evs if e["m"] and e["m"]["split"])
        n_nodata = sum(1 for e in evs if not e["m"])
        logger.info("  %s: 有効データなし（データ無%d/分割除外%d）", label, n_nodata, n_split)
        return
    parts = [
        f"{label}: n={s['n']}",
        f"値幅≥{_HIT_RANGE:.0f}% {s['range_hit_pct']:.0f}%",
        f"旬ヒット(値幅&出来高) {s['hit_pct']:.0f}%",
        f"空振り {s['noise_pct']:.0f}%",
    ]
    if s["range_med"] is not None:
        parts.append(f"値幅中央{s['range_med']:.1f}%")
    if s["spike_med"] is not None:
        parts.append(f"出来高×前日中央{s['spike_med']:.1f}")
    extra = []
    if s["n_nodata"]:
        extra.append(f"データ無{s['n_nodata']}")
    if s["n_split"]:
        extra.append(f"分割除外{s['n_split']}")
    if extra:
        parts.append("(" + "/".join(extra) + ")")
    logger.info("  " + " | ".join(parts))


def _verdict(all_ev: list) -> list:
    """ロック済み成功基準に対する PASS/FAIL を自動判定し、FAIL項目の説明リストを返す。

    返り値が空＝全PASS or 判定保留。データ無し/小サンプルはFAIL扱いしない（誤アラート抑止）。
    """
    def pf(ok: bool) -> str:
        return "✅PASS" if ok else "❌FAIL"

    fails: list[str] = []
    logger.info("\n=== 成功基準の判定（2026-06-26ロック） ===")
    overall = _stats(all_ev)
    if not overall:
        logger.info("  判定不可：有効データなし（本番稼働後に再評価）")
        return fails
    if overall["n"] < SC_MIN_N:
        logger.info("  サンプル不足（有効%d件 < %d）→ 判定保留（誤アラート抑止）", overall["n"], SC_MIN_N)
        return fails

    c1 = overall["range_hit_pct"] >= SC_RANGE_HIT_MIN
    logger.info("  [主指標] 値幅≥5%%率 %.0f%% （基準 ≥%.0f%%）→ %s",
                overall["range_hit_pct"], SC_RANGE_HIT_MIN, pf(c1))
    if not c1:
        fails.append(f"主指標 値幅≥5%%率 {overall['range_hit_pct']:.0f}% < {SC_RANGE_HIT_MIN:.0f}%")
    c2 = overall["noise_pct"] <= SC_NOISE_MAX
    logger.info("  [ノイズ] 空振り率 %.0f%% （基準 ≤%.0f%%）→ %s",
                overall["noise_pct"], SC_NOISE_MAX, pf(c2))
    if not c2:
        fails.append(f"ノイズ 空振り率 {overall['noise_pct']:.0f}% > {SC_NOISE_MAX:.0f}%")

    up, vol = _stats([e for e in all_ev if e["source"] == "up"]), _stats([e for e in all_ev if e["source"] == "vol"])
    if vol and up and vol["n"] >= SC_MIN_N:
        gap = up["range_hit_pct"] - vol["range_hit_pct"]
        c3 = gap <= SC_VOL_GAP_MAX and vol["noise_pct"] <= SC_NOISE_MAX
        logger.info("  [出来高源] vol値幅≥5%%率 %.0f%%（up比 -%.0fpt / 空振り %.0f%%）→ %s",
                    vol["range_hit_pct"], gap, vol["noise_pct"], pf(c3))
        if not c3:
            fails.append(f"出来高源 vol値幅≥5%%率 {vol['range_hit_pct']:.0f}%（up比-{gap:.0f}pt/空振り{vol['noise_pct']:.0f}%）")
    else:
        logger.info("  [出来高源] vol源データ不足 → 判定保留（本番稼働後に再評価）")

    try:
        from store import coverage_runs
        today = datetime.now(JST).strftime("%Y-%m-%d")
        # 当日分は引け後cron前だと未確定(場中0%)になりうるので除外。日ごと最新1件にして直近確定日を採る。
        seen, confirmed = set(), []
        for c in coverage_runs.get_recent(20):
            d = c.get("ranking_date")
            if d in seen or d == today or c.get("capture_rate") is None:
                continue
            seen.add(d)
            confirmed.append(c)
        if confirmed:
            latest = confirmed[0]["capture_rate"] * 100
            c4 = latest > SC_CAPTURE_MIN
            logger.info("  [捕捉率] 直近確定 %s %.1f%% （基準 >%.0f%%）→ %s",
                        confirmed[0]["ranking_date"], latest, SC_CAPTURE_MIN, pf(c4))
            if not c4:
                fails.append(f"捕捉率 直近 {latest:.1f}% ≤ {SC_CAPTURE_MIN:.0f}%")
        else:
            logger.info("  [捕捉率] 確定データ無し → 判定保留")
    except Exception:
        logger.info("  [捕捉率] 取得失敗 → 判定保留")

    return fails


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
        return []

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
    fails = _verdict(all_ev)
    logger.info(
        "\n指標定義: 値幅=日中TR%%(高-安)/前日終値 ／ 空振り=値幅<%.0f%% かつ 出来高×前日<%.1f",
        _NOISE_RANGE, _NOISE_SPIKE,
    )
    return fails


def _send_alert(fails: list) -> None:
    """成功基準割れを運用者へメール通知する（REPORT_EMAIL宛・既存アラートと同経路）。"""
    from monitor import mailer

    run_at_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    try:
        mailer.send_technical_alert(fails, run_at_jst)
        logger.info("FAIL通知メールを送信（REPORT_EMAIL未設定ならスキップ）")
    except Exception:
        logger.exception("FAIL通知メールの送信に失敗（GHAのジョブ失敗通知で代替）")


def main() -> None:
    ap = argparse.ArgumentParser(description="テクニカル版 picks の精度計測")
    today = datetime.now(JST).strftime("%Y-%m-%d")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", default=today)
    ap.add_argument("--alert", action="store_true",
                    help="成功基準割れ時に REPORT_EMAIL へメール通知し exit 1（GHA監視用）")
    args = ap.parse_args()
    fails = run(args.date_from, args.date_to)
    if fails:
        if args.alert:
            _send_alert(fails)
        sys.exit(1)   # 基準割れ＝ジョブを赤にして異常を可視化（例外ベース監視）


if __name__ == "__main__":
    main()
