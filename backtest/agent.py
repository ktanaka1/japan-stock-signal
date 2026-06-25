"""元本シミュレーション役エージェント。

実配信シグナル(notified_at済み・impact≥4)を各取引ルールで約定したと仮定し、
ユニーク銘柄の日足OHLCを取得して元本の日次推移を再計算、backtest_runs に記録する。
既存テーブルは読み取りのみ（収集・分析・配信ロジックには一切触れない＝外付けの計測器）。
実行: python -m backtest.agent（平日 引け後 JST 16:35 想定 / feedback の直後）
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from store.db import migrate
from store import backtest_runs
from backtest import engine, quotes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


def run() -> None:
    migrate()
    now = datetime.now(JST)
    as_of = now.strftime("%Y-%m-%d")

    positions = engine.load_positions()
    if not positions:
        logger.warning("Backtest %s: no delivered signals; skip", as_of)
        return

    # 取得範囲: 最古の建玉基準日の少し前 〜 当日
    earliest = min(p["asof"] for p in positions)
    start = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")

    ohlc_by_code = {}
    codes = sorted({p["code"] for p in positions})
    for i, code in enumerate(codes):
        ohlc_by_code[code] = quotes.fetch_ohlc(code, start, as_of)
        time.sleep(0.05)  # Yahoo へ過度に連射しない
        if (i + 1) % 50 == 0:
            logger.info("Backtest %s: fetched %d/%d codes", as_of, i + 1, len(codes))

    fetched = sum(1 for v in ohlc_by_code.values() if v)
    snapshot = engine.compute(positions, ohlc_by_code, as_of)
    run_id = backtest_runs.record(as_of, engine.START_CAPITAL, snapshot)

    best = max(snapshot["rules"], key=lambda r: r["cap"])
    logger.info(
        "Backtest %s recorded (id=%s): %d positions / %d codes (OHLC %d ok). best=%s ¥%s (%+.1f%%)",
        as_of, run_id, len(positions), len(codes), fetched,
        best["label"], f"{best['cap']:,}", best["ret_pct"],
    )


if __name__ == "__main__":
    run()
