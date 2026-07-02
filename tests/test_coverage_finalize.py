"""捕捉率フィードバックの2フェーズ化（snapshot→finalize）のテスト。

snapshot は D夕方の配信前に測るため未配信を過小評価する。finalize を配信完了後に
走らせ、最新の notified_at を反映して暫定行を確定する。既存テーブルは読み取りのみ
（書き込みは計測器自身の coverage_runs のみ）。
ネットワーク非依存（ranking.fetch_ranking をモック）。
"""
import json
import os

# --- 環境ガード: 本番D1へ繋がない（CLAUDE.md） ---
assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"

from store.db import migrate, execute
from store import coverage_runs
from feedback import ranking, agent


def _sig(article_id, code, created_at, notified_at=None):
    """signals を1行入れる。notified_at が None なら未配信。"""
    execute(
        "INSERT INTO signals (article_id, sentiment, summary, stocks, url, impact,"
        " created_at, notified_at) VALUES (?,?,?,?,?,?,?,?)",
        (article_id, "positive", "s", json.dumps([{"code": code, "name": code}]),
         f"u{article_id}", 4, created_at, notified_at),
    )


def _mock_ranking(monkeypatch, items):
    monkeypatch.setattr(agent.ranking, "fetch_ranking", lambda *a, **k: items)


def _freeze_now(monkeypatch, dt):
    """agent.datetime.now(JST) を固定する。"""
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.astimezone(tz) if tz else dt

    monkeypatch.setattr(agent, "datetime", _FixedDateTime)


def test_snapshot_records_provisional(monkeypatch):
    """snapshot: finalized=0 で記録され、当日created・notified NULL は signaled に暫定計上。"""
    migrate()
    import datetime as _dt
    # D = 2026-06-26 16:30 JST（snapshot 時刻）
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 26, 16, 30, tzinfo=agent.JST))
    items = [
        ranking.RankItem(1, "S001", "拾ったが未配信", "東証STD", 20.0, False, False),
        ranking.RankItem(2, "S002", "未収集", "東証STD", 15.0, False, False),
    ]
    _mock_ranking(monkeypatch, items)
    # 窓内(D-1 15:00〜D 15:00 JST = 6/25 06:00〜6/26 06:00 UTC)に created・notified NULL
    _sig(8101, "S001", "2026-06-25 23:00:00", notified_at=None)

    agent.run()

    row = execute("SELECT * FROM coverage_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert row["ranking_date"] == "2026-06-26"
    assert row["finalized"] == 0
    assert row["universe"] == 2
    assert row["captured"] == 0                # まだ配信されていない
    assert row["signaled"] == 1               # S001 は暫定で未配信扱い
    assert row["not_collected"] == 1          # S002
    assert row["capture_rate"] == 0.0


def test_finalize_promotes_signaled_to_delivered(monkeypatch):
    """finalize の核心: 暫定で signaled だった銘柄に notified_at が付くと delivered へ移り
    capture_rate が上がり finalized=1 になる。"""
    migrate()
    import datetime as _dt
    # 1) D=6/26 夕方に snapshot（配信前）
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 26, 16, 30, tzinfo=agent.JST))
    items = [
        ranking.RankItem(1, "F001", "翌朝配信される", "東証STD", 20.0, False, False),
        ranking.RankItem(2, "F002", "未収集", "東証STD", 15.0, False, False),
    ]
    _mock_ranking(monkeypatch, items)
    _sig(8201, "F001", "2026-06-25 23:00:00", notified_at=None)  # 窓内・未配信
    agent.run()

    snap = execute("SELECT * FROM coverage_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert snap["finalized"] == 0 and snap["captured"] == 0 and snap["signaled"] == 1

    # 2) D+1=6/27 朝、配信完了で notified_at が付く
    execute("UPDATE signals SET notified_at = ? WHERE article_id = ?",
            ("2026-06-26 22:35:00", 8201))
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 27, 7, 35, tzinfo=agent.JST))

    agent.finalize()

    fin = execute("SELECT * FROM coverage_runs WHERE id = ?", (snap["id"],)).rows[0]
    assert fin["finalized"] == 1
    assert fin["captured"] == 1               # delivered に移った
    assert fin["signaled"] == 0
    assert fin["capture_rate"] == 0.5         # 1/2 へ上昇（snapshot は 0.0 だった）
    detail = json.loads(fin["detail"])
    st = {d["code"]: d["status"] for d in detail}
    assert st["F001"] == "delivered"


def test_finalize_only_targets_past_unfinalized(monkeypatch):
    """finalize は ranking_date < today かつ finalized=0 の行のみ対象（当日の暫定行は触らない）。"""
    migrate()
    # 当日(6/27)の暫定行を直接INSERT
    coverage_runs.record(
        ranking_date="2026-06-27", ranking_type="up", top_n=30, universe=1,
        captured=0, captured_tech=0, signaled=1, neutral=0, not_collected=0,
        capture_rate=0.0, detail=[{"rank": 1, "code": "T001", "name": "x",
                                   "market": "東証STD", "pct": 10.0, "in_universe": True,
                                   "status": "signaled_not_delivered"}], proposal="p",
    )
    today_id = execute("SELECT MAX(id) AS m FROM coverage_runs").rows[0]["m"]

    import datetime as _dt
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 27, 7, 35, tzinfo=agent.JST))
    agent.finalize()

    row = execute("SELECT finalized FROM coverage_runs WHERE id = ?", (today_id,)).rows[0]
    assert row["finalized"] == 0              # 当日行は確定対象外


def test_finalize_picks_max_id_per_date(monkeypatch):
    """同一 ranking_date に暫定行が複数あっても MAX(id) の1行だけ確定する。"""
    migrate()
    detail = [{"rank": 1, "code": "M001", "name": "x", "market": "東証STD",
               "pct": 10.0, "in_universe": True, "status": "not_collected"}]
    for _ in range(2):
        coverage_runs.record(
            ranking_date="2026-06-25", ranking_type="up", top_n=30, universe=1,
            captured=0, captured_tech=0, signaled=0, neutral=0, not_collected=1,
            capture_rate=0.0, detail=detail, proposal="p",
        )
    ids = [r["id"] for r in
           execute("SELECT id FROM coverage_runs WHERE ranking_date='2026-06-25' ORDER BY id").rows]
    older_id, newer_id = ids[0], ids[1]

    import datetime as _dt
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 27, 7, 35, tzinfo=agent.JST))
    agent.finalize()

    older = execute("SELECT finalized FROM coverage_runs WHERE id=?", (older_id,)).rows[0]
    newer = execute("SELECT finalized FROM coverage_runs WHERE id=?", (newer_id,)).rows[0]
    assert older["finalized"] == 0           # 旧暫定行は触らない
    assert newer["finalized"] == 1           # 最新だけ確定


def test_finalize_does_not_resurrect_older_provisional(monkeypatch, caplog):
    """最新行を確定した後、同日の旧い暫定行が翌日以降の finalize で浮上しない（ゾンビ行回帰）。

    確定対象の MAX(id) を「未確定行の中」で取ると、最新行の確定後に旧暫定行が
    「未確定の中の最大id」として翌日の finalize に選ばれ二重確定される。
    """
    migrate()
    # 他テストの残置行を除外して隔離
    execute("UPDATE coverage_runs SET finalized = 1")
    detail = [{"rank": 1, "code": "Z001", "name": "x", "market": "東証STD",
               "pct": 10.0, "in_universe": True, "status": "not_collected"}]
    for _ in range(2):
        coverage_runs.record(
            ranking_date="2026-06-25", ranking_type="up", top_n=30, universe=1,
            captured=0, captured_tech=0, signaled=0, neutral=0, not_collected=1,
            capture_rate=0.0, detail=detail, proposal="p",
        )
    ids = [r["id"] for r in
           execute("SELECT id FROM coverage_runs WHERE ranking_date='2026-06-25'"
                   " AND finalized=0 ORDER BY id").rows]
    older_id, newer_id = ids[0], ids[1]

    import datetime as _dt
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 27, 7, 35, tzinfo=agent.JST))
    agent.finalize()
    assert execute("SELECT finalized FROM coverage_runs WHERE id=?", (newer_id,)).rows[0]["finalized"] == 1

    # 翌日もう一度 finalize: 旧暫定行が確定対象に浮上せず「nothing to do」で終わること
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 28, 7, 35, tzinfo=agent.JST))
    import logging
    with caplog.at_level(logging.INFO):
        agent.finalize()
    assert any("nothing to do" in r.message for r in caplog.records)
    assert execute("SELECT finalized FROM coverage_runs WHERE id=?", (older_id,)).rows[0]["finalized"] == 0


def test_finalize_is_idempotent(monkeypatch):
    """finalize を2回呼んでも finalized=1 は対象外なので二重更新で壊れない。"""
    migrate()
    import datetime as _dt
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 26, 16, 30, tzinfo=agent.JST))
    items = [ranking.RankItem(1, "I001", "x", "東証STD", 20.0, False, False)]
    _mock_ranking(monkeypatch, items)
    _sig(8301, "I001", "2026-06-25 23:00:00", notified_at="2026-06-26 22:35:00")
    agent.run()
    row_id = execute("SELECT MAX(id) AS m FROM coverage_runs").rows[0]["m"]

    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 27, 7, 35, tzinfo=agent.JST))
    agent.finalize()
    first = execute("SELECT * FROM coverage_runs WHERE id=?", (row_id,)).rows[0]
    assert first["finalized"] == 1 and first["captured"] == 1

    # 2回目: もう finalized=1 なので対象にならず値は不変
    agent.finalize()
    second = execute("SELECT * FROM coverage_runs WHERE id=?", (row_id,)).rows[0]
    assert second["finalized"] == 1
    assert second["captured"] == first["captured"]
    assert second["capture_rate"] == first["capture_rate"]


def test_finalize_noop_when_nothing(monkeypatch, caplog):
    """確定対象が無いとき silent でなくログを出して終了する。"""
    migrate()
    import datetime as _dt
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 27, 7, 35, tzinfo=agent.JST))
    # 暫定行が無い状態（過去の未確定なし）
    execute("UPDATE coverage_runs SET finalized = 1")
    import logging
    with caplog.at_level(logging.INFO):
        agent.finalize()
    assert any("nothing to do" in r.message for r in caplog.records)
