"""ダッシュボードのパイプライン統計（直近N日表形式）の検証。

実行（本番D1誤接続を防ぐため FORCE_LOCAL_DB=1 + DB_PATH を必須）:
    FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db \
        .venv/bin/python -m pytest tests/test_dashboard_stats.py -v
"""
from __future__ import annotations

import json
import os

# --- 環境ガード: 本番D1へ繋がない（CLAUDE.md） ---
assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"


def _fresh_db():
    db_path = os.environ["DB_PATH"]
    if os.path.exists(db_path):
        os.remove(db_path)
    from fastapi.testclient import TestClient
    from webhook.app import app

    with TestClient(app):
        pass


def _seed():
    """3日分のデータを投入する。

    fetched_at は UTC 格納で +9h して JST日付になる。JST日付の境界を跨がないよう
    各日 UTC 03:00（= JST 12:00）で固定する。
    日付ごとに articles / analyzed(article_analyses) / signaled(signals) /
    notified(signals.notified_at) の件数を変える。
    """
    from store.db import execute

    # (jst_date, utc_datetime, articles, analyzed, signaled, notified)
    plan = [
        ("2026-06-18", "2026-06-18 03:00:00", 5, 0, 0, 0),
        ("2026-06-17", "2026-06-17 03:00:00", 4, 2, 1, 0),
        ("2026-06-16", "2026-06-16 03:00:00", 3, 3, 2, 1),
        ("2026-06-15", "2026-06-15 03:00:00", 2, 1, 1, 1),  # 4日目（days=3なら範囲外）
    ]
    uid = 0
    for (jst, utc_dt, n_art, n_ana, n_sig, n_notif) in plan:
        ids = []
        for _ in range(n_art):
            uid += 1
            res = execute(
                "INSERT INTO articles (title, body, url, fetched_at, is_read) "
                "VALUES (?, ?, ?, ?, 0)",
                (f"記事{uid}", "本文", f"http://x/{uid}", utc_dt),
            )
            ids.append(res.last_row_id)
        for aid in ids[:n_ana]:
            execute(
                "INSERT INTO article_analyses "
                "(article_id, sentiment, summary, reason, stocks, became_signal) "
                "VALUES (?, 'positive', 's', 'r', ?, 1)",
                (aid, json.dumps([{"name": "A", "code": "1111"}])),
            )
        for i, aid in enumerate(ids[:n_sig]):
            notified_at = "2026-06-18 22:00:00" if i < n_notif else None
            execute(
                "INSERT INTO signals (article_id, sentiment, summary, stocks, url, notified_at) "
                "VALUES (?, 'positive', 's', ?, ?, ?)",
                (aid, json.dumps([{"name": "A", "code": "1111"}]), f"http://x/{aid}", notified_at),
            )
    return {p[0]: {"articles": p[2], "analyzed": p[3], "signaled": p[4], "notified": p[5]}
            for p in plan}


def test_recent_days_order_and_counts():
    _fresh_db()
    expected = _seed()
    from store import analyses

    rows = analyses.get_stats_recent_days(3)

    # 新しい順3日分
    assert [r["date"] for r in rows] == ["2026-06-18", "2026-06-17", "2026-06-16"]
    assert len(rows) == 3

    for r in rows:
        exp = expected[r["date"]]
        assert r["articles"] == exp["articles"], r["date"]
        assert r["analyzed"] == exp["analyzed"], r["date"]
        assert r["signaled"] == exp["signaled"], r["date"]
        assert r["notified"] == exp["notified"], r["date"]


def test_matches_get_stats_by_date():
    """get_stats_by_date と完全一致すること（定義ズレ防止）。"""
    _fresh_db()
    _seed()
    from store import analyses

    rows = analyses.get_stats_recent_days(3)
    for r in rows:
        single = analyses.get_stats_by_date(r["date"])
        assert r["articles"] == single["articles"], r["date"]
        assert r["analyzed"] == single["analyzed"], r["date"]
        assert r["signaled"] == single["signaled"], r["date"]
        assert r["notified"] == single["notified"], r["date"]


def test_empty_db_returns_empty():
    _fresh_db()
    from store import analyses

    assert analyses.get_stats_recent_days(3) == []


def test_dashboard_renders_table_with_links():
    _fresh_db()
    _seed()
    from fastapi.testclient import TestClient
    from webhook.app import app

    token = os.environ["ADMIN_TOKEN"]
    with TestClient(app) as c:
        resp = c.get("/admin", cookies={"admin_session": token})
        assert resp.status_code == 200
        html = resp.text
        assert "パイプライン統計" in html
        # 日付ピッカーは廃止
        assert 'type="date"' not in html
        # 各日の記事一覧リンクが出る
        for d in ("2026-06-18", "2026-06-17", "2026-06-16"):
            assert f"/admin/articles?date={d}" in html, d
        # 4日目は範囲外
        assert "/admin/articles?date=2026-06-15" not in html
