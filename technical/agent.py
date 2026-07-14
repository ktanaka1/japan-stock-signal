"""
テクニカル版 朝のシグナル（価格/出来高 駆動の別系統スキャナ）
  - 前営業日の「値上がり率＋出来高急増」の複合で動いた中小型銘柄を寄り前にLINE配信する
  - ニュース起因の本体とは独立。本体のピックアップロジックには一切触れない（既存テーブルは読むだけ）
  - 本体ニュース版で当日配信済みの銘柄は除外（二重配信防止）→ 本体digest(7:30)の後に実行する
  - 実行: python -m technical.agent（平日 朝8:00 JST 想定＝本体digestの後・寄り前）
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import List

from store.db import migrate, execute
from store import settings as cfg
from store import technical_runs
from store.recipients import get_all as get_recipients
from monitor.notifier import send_text
from monitor import quotes
from feedback import ranking

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
_MAX_MESSAGE_CHARS = 4500


def _float_cfg(key: str, default: float) -> float:
    try:
        return float(cfg.get(key))
    except (TypeError, ValueError):
        return default


def _int_cfg(key: str, default: int) -> int:
    try:
        return int(cfg.get(key))
    except (TypeError, ValueError):
        return default


def _delivered_today_codes(today_jst: str) -> set:
    """本体ニュース版で当日(JST)配信済みのシグナル銘柄コード集合（二重配信防止用）。"""
    rows = execute(
        "SELECT stocks FROM signals WHERE notified_at IS NOT NULL "
        "AND date(notified_at, '+9 hours') = ?",
        (today_jst,),
    ).rows
    codes = set()
    for r in rows:
        try:
            for s in json.loads(r["stocks"]):
                c = str(s.get("code", "")).strip()
                if c:
                    codes.add(c)
        except (ValueError, TypeError):
            continue
    return codes


def _turnover(m: dict):
    """売買代金(円) = 終値 × 出来高。算出不能なら None。"""
    close, vol = m.get("close"), m.get("volume")
    if close is None or vol is None:
        return None
    return close * vol


def _make_pick(it: "ranking.RankItem", m: dict, source: str) -> dict:
    return {
        "code": it.code,
        "name": it.name,
        "change_pct": it.change_pct,
        "surge": m["surge"],
        "close": m.get("close"),
        "volume": m.get("volume"),
        "turnover": _turnover(m),
        "asof": m.get("asof"),
        "source": source,               # "up"=値上がり率源 / "vol"=出来高源
    }


def _scan(min_change: float, min_surge: float, top_n: int, exclude: set,
          min_turnover_yen: float, scan_volume: bool, vol_min_change: float,
          vol_min_surge: float) -> List[dict]:
    """2系統のランキングから候補を抽出する（同一コードは up 優先で1件に統合）。

    - 値上がり率(up): 値上がり率≥min_change かつ 出来高急増≥min_surge
    - 出来高(volume): 上昇方向(値上がり率≥vol_min_change) かつ 出来高急増≥vol_min_surge（任意・第2源）
      出来高上位は恒常的に商いが多く自平均比の急増が出にくいため、surge下限はup源と分離する
    両系統とも「売買代金≥min_turnover_yen」を必須化し、薄商いの見せかけ急増を弾く。
    """
    picks: dict[str, dict] = {}                     # code -> pick（dedup）
    metrics_cache: dict[str, object] = {}           # code -> get_daily_metrics 結果（重複取得回避）

    def _metrics(code: str):
        if code not in metrics_cache:
            metrics_cache[code] = quotes.get_daily_metrics(code)
        return metrics_cache[code]

    def _qualifies(m, surge_floor: float) -> bool:
        if not m or m.get("surge") is None or m["surge"] < surge_floor:
            return False
        t = _turnover(m)
        if t is None or t < min_turnover_yen:       # 流動性フロア（薄商い除外）
            return False
        return True

    # 値上がり率ランキング（既存ロジック）
    for it in ranking.fetch_ranking("up", top_n=top_n):
        if not it.in_universe or it.code in exclude or it.code in picks:
            continue
        if it.change_pct is None or it.change_pct < min_change:
            continue
        m = _metrics(it.code)
        if _qualifies(m, min_surge):
            picks[it.code] = _make_pick(it, m, "up")

    # 出来高ランキング（新規・第2源）。上昇方向のみ。up で既出のコードは飛ばす。
    if scan_volume:
        for it in ranking.fetch_ranking("volume", top_n=top_n):
            if not it.in_universe or it.code in exclude or it.code in picks:
                continue
            if it.change_pct is None or it.change_pct < vol_min_change:
                continue
            m = _metrics(it.code)
            if _qualifies(m, vol_min_surge):
                picks[it.code] = _make_pick(it, m, "vol")

    return list(picks.values())


def _header(count: int) -> str:
    now = datetime.now(JST)
    return f"📊 {now.month}/{now.day}({_WEEKDAYS[now.weekday()]}) 朝のテクニカル注目 {count}件"


def _format_pick(p: dict) -> str:
    tag = "🔊出来高" if p.get("source") == "vol" else "📈値上り"
    lines = [f"{tag} {p['name']}({p['code']})"]
    metrics = []
    if p.get("change_pct") is not None:
        metrics.append(f"前日 {p['change_pct']:+.1f}%")
    if p.get("surge") is not None:
        metrics.append(f"出来高 {p['surge']:.1f}倍")
    if p.get("turnover") is not None:
        metrics.append(f"代金 {p['turnover'] / 1e8:.1f}億")
    if metrics:
        lines.append("　" + " / ".join(metrics))
    if p.get("close") is not None:
        lines.append(f"　📊 {p['close']:,.0f}円")
    lines.append(f"　https://finance.yahoo.co.jp/quote/{p['code']}.T")
    return "\n".join(lines)


def _build_messages(picks: List[dict]) -> List[str]:
    header = _header(len(picks))
    subtitle = "（前日の出来高急増＋上昇。値上り率/出来高ランキング由来。材料の有無は問わず）"
    blocks = [_format_pick(p) for p in picks]
    messages = []
    current = header + "\n" + subtitle
    for block in blocks:
        if len(current) + len(block) + 2 > _MAX_MESSAGE_CHARS:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    messages.append(current)
    return messages


def _zero_message() -> str:
    return f"{_header(0)}\n\n前日の出来高急増を伴う大幅高の中小型銘柄はありませんでした。"


def run() -> None:
    migrate()
    now = datetime.now(JST)
    today_jst = now.strftime("%Y-%m-%d")
    status = "ok"
    total_ok = total_error = 0
    error_detail = None
    picks: List[dict] = []

    try:
        recipients = get_recipients()
        min_change = _float_cfg("tech_min_change_pct", 5.0)
        min_surge = _float_cfg("tech_min_volume_surge", 2.0)
        top_n = _int_cfg("tech_top_n", 30)
        dedup = cfg.get("tech_dedup_with_news").lower() == "true"
        exclude = _delivered_today_codes(today_jst) if dedup else set()
        min_turnover_yen = _float_cfg("tech_min_turnover_oku", 1.0) * 1e8
        scan_volume = cfg.get("tech_scan_volume").lower() == "true"
        vol_min_change = _float_cfg("tech_vol_min_change_pct", 3.0)
        vol_min_surge = _float_cfg("tech_vol_min_surge", 1.3)

        picks = _scan(min_change, min_surge, top_n, exclude,
                      min_turnover_yen, scan_volume, vol_min_change, vol_min_surge)
        logger.info(
            "Technical scan: %d picks (min_change=%.1f%% min_surge=%.1fx "
            "vol_min_surge=%.1fx turnover>=%.1f億 scan_volume=%s dedup=%s)",
            len(picks), min_change, min_surge, vol_min_surge,
            min_turnover_yen / 1e8, scan_volume, dedup,
        )

        if not recipients:
            status = "no_recipients"
        else:
            messages = _build_messages(picks) if picks else [_zero_message()]
            for text in messages:
                res = send_text(text, recipients)
                total_ok += res.ok
                total_error += res.error
            if not picks:
                status = "no_picks"
            elif total_error > 0 and total_ok == 0:
                status = "error"
            if total_ok == 0 and total_error == 0 and status == "ok":
                status = "no_recipients"  # 全員不正IDで送信先ゼロ
    except Exception:
        status = "error"
        error_detail = traceback.format_exc()
        logger.exception("technical.run() failed")

    target_date = picks[0]["asof"] if picks and picks[0].get("asof") else today_jst
    technical_runs.record(
        target_date=target_date,
        status=status,
        pick_count=len(picks),
        line_ok=total_ok,
        line_error=total_error,
        picks=picks,
        error_detail=error_detail,
    )
    logger.info("Technical done: status=%s picks=%d ok=%d err=%d",
                status, len(picks), total_ok, total_error)


if __name__ == "__main__":
    run()
