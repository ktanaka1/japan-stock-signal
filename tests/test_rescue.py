"""逆引き昇格（rescue・signaled層の交差救済）のテスト。

「impact閾値未満でAIには凡庸に見えたが、値上がり率ランキング上位に入った」シグナルを
翌朝配信へ昇格する3段構えの第3レーン。検証観点:
  - snapshot昇格: signaled銘柄のimpact3シグナルに promoted_at/promote_reason が付く
  - 昇格しない: impact4(どうせ配信)/impact2(floor未満)/配信済み/rescue_enabled=false
  - 1銘柄1件: 候補複数なら impact最大→id最大 の1件のみ
  - digest配信: min_impact=4 でも昇格分は配信され 🔁 行が付く
  - finalize: 配信後 status=delivered_rescue・rescued別枠計上・capture_rate不算入
  - 冪等: snapshot再実行で二重昇格しない

ネットワーク非依存（ranking.fetch_ranking をモック）。
"""
import json
import os
import tempfile
import types

# --- 環境ガード: 本番D1へ繋がない（CLAUDE.md） ---
assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"

from store.db import migrate, execute
from store import signals
from feedback import ranking, agent


def _fresh_db():
    """テストごとに新品のローカルSQLiteへ切り替える（他テストの残置データと隔離）。"""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db_path)  # migrate に作らせる
    os.environ["DB_PATH"] = db_path
    import store.db as db
    db.DB_PATH = db_path
    migrate()


def _sig(article_id, code, created_at, impact=3, notified_at=None):
    execute(
        "INSERT INTO signals (article_id, sentiment, summary, stocks, url, impact,"
        " created_at, notified_at) VALUES (?,?,?,?,?,?,?,?)",
        (article_id, "positive", f"要約{article_id}", json.dumps([{"code": code, "name": code}]),
         f"u{article_id}", impact, created_at, notified_at),
    )
    return execute("SELECT MAX(id) AS m FROM signals").rows[0]["m"]


def _mock_ranking(monkeypatch, items):
    monkeypatch.setattr(agent.ranking, "fetch_ranking", lambda *a, **k: items)


def _freeze_now(monkeypatch, dt):
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.astimezone(tz) if tz else dt

    monkeypatch.setattr(agent, "datetime", _FixedDateTime)


def _snapshot(monkeypatch, items):
    """D=2026-06-26 16:30 JST の snapshot を実行する（窓: 6/25 06:00〜6/26 06:00 UTC）。"""
    import datetime as _dt
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 26, 16, 30, tzinfo=agent.JST))
    _mock_ranking(monkeypatch, items)
    agent.run()


def _promoted_rows():
    return execute(
        "SELECT id, article_id, impact, promoted_at, promote_reason FROM signals "
        "WHERE promoted_at IS NOT NULL ORDER BY id"
    ).rows


def test_snapshot_promotes_threshold_dropped_signal(monkeypatch):
    """impact3×ランキング上位（signaled）→ promoted_at と根拠付き reason が付く。"""
    _fresh_db()
    sid = _sig(9001, "R001", "2026-06-25 23:00:00", impact=3)
    items = [
        ranking.RankItem(1, "R001", "閾値落ちだが急騰", "東証STD", 20.0, False, False),
        ranking.RankItem(2, "R002", "未収集", "東証STD", 15.0, False, False),
    ]
    _snapshot(monkeypatch, items)

    rows = _promoted_rows()
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["promote_reason"] == "2026-06-26 値上がり率#1 +20.0%"
    # snapshot 時点の coverage 行は昇格前の素の状態（rescued=0・signaled=1）
    cov = execute("SELECT * FROM coverage_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert cov["rescued"] == 0 and cov["signaled"] == 1


def test_snapshot_does_not_promote_out_of_scope(monkeypatch):
    """impact4(どうせ配信)・impact2(floor未満)・配信済みは昇格しない。"""
    _fresh_db()
    _sig(9101, "R101", "2026-06-25 23:00:00", impact=4)                # 閾値以上→翌朝通常配信
    _sig(9102, "R102", "2026-06-25 23:00:00", impact=2)                # floor未満→ノイズ扱い
    _sig(9103, "R103", "2026-06-25 23:00:00", impact=3,
         notified_at="2026-06-26 05:00:00")                            # 配信済み→対象外
    items = [
        ranking.RankItem(1, "R101", "x", "東証STD", 20.0, False, False),
        ranking.RankItem(2, "R102", "y", "東証STD", 18.0, False, False),
        ranking.RankItem(3, "R103", "z", "東証STD", 16.0, False, False),
    ]
    _snapshot(monkeypatch, items)
    assert _promoted_rows() == []


def test_snapshot_noop_when_disabled(monkeypatch):
    """rescue_enabled=false なら一切昇格しない。"""
    _fresh_db()
    from store import settings as cfg
    cfg.set_value("rescue_enabled", "false")
    _sig(9201, "R201", "2026-06-25 23:00:00", impact=3)
    _snapshot(monkeypatch, [ranking.RankItem(1, "R201", "x", "東証STD", 20.0, False, False)])
    assert _promoted_rows() == []


def test_one_signal_per_stock(monkeypatch):
    """同一銘柄に候補が複数 → impact最大→id最大（最新）の1件だけ昇格する。"""
    _fresh_db()
    _sig(9301, "R301", "2026-06-25 22:00:00", impact=3)
    newest = _sig(9302, "R301", "2026-06-25 23:00:00", impact=3)
    _snapshot(monkeypatch, [ranking.RankItem(1, "R301", "x", "東証STD", 20.0, False, False)])
    rows = _promoted_rows()
    assert [r["id"] for r in rows] == [newest]


def test_snapshot_idempotent_no_double_promote(monkeypatch):
    """snapshot を同日に再実行しても昇格は増えず reason も変わらない。"""
    _fresh_db()
    _sig(9401, "R401", "2026-06-25 23:00:00", impact=3)
    items = [ranking.RankItem(1, "R401", "x", "東証STD", 20.0, False, False)]
    _snapshot(monkeypatch, items)
    first = _promoted_rows()
    _snapshot(monkeypatch, items)   # 再実行（cron遅延や手動での重複想定）
    assert _promoted_rows() == first


def test_digest_delivers_promoted_with_marker(monkeypatch):
    """min_impact=4 のままでも昇格シグナルは配信され、🔁 行が付き、非昇格impact3は残る。"""
    _fresh_db()
    promoted_id = _sig(9501, "R501", "2026-06-25 23:00:00", impact=3)
    signals.promote(promoted_id, "2026-06-26 値上がり率#1 +20.0%")
    stay_id = _sig(9502, "R502", "2026-06-25 23:00:00", impact=3)      # 非昇格→残留

    from monitor import digest
    sent = []

    def _fake_send(text, recipients):
        sent.append(text)
        return types.SimpleNamespace(ok=1, error=0, error_details=[])

    monkeypatch.setattr(digest, "send_text", _fake_send)
    monkeypatch.setattr(digest, "get_recipients", lambda: ["U" + "0" * 32])
    digest.run()

    body = "\n".join(sent)
    assert "🔁 逆引き昇格（2026-06-26 値上がり率#1 +20.0%）" in body
    got = {r["id"]: r["notified_at"] for r in
           execute("SELECT id, notified_at FROM signals").rows}
    assert got[promoted_id] is not None      # 昇格分は配信済み
    assert got[stay_id] is None              # 非昇格のimpact3は未配信のまま


def test_finalize_counts_rescued_separately(monkeypatch):
    """配信後の finalize で delivered_rescue に分類され、rescued 別枠計上・capture_rate 不算入。"""
    _fresh_db()
    sid = _sig(9601, "R601", "2026-06-25 23:00:00", impact=3)
    items = [
        ranking.RankItem(1, "R601", "救済対象", "東証STD", 20.0, False, False),
        ranking.RankItem(2, "R602", "未収集", "東証STD", 15.0, False, False),
    ]
    _snapshot(monkeypatch, items)            # 昇格される
    assert len(_promoted_rows()) == 1

    # D+1 朝: digest が昇格分を配信した想定
    execute("UPDATE signals SET notified_at = ? WHERE id = ?", ("2026-06-26 22:35:00", sid))
    import datetime as _dt
    _freeze_now(monkeypatch, _dt.datetime(2026, 6, 27, 7, 35, tzinfo=agent.JST))
    agent.finalize()

    cov = execute("SELECT * FROM coverage_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert cov["finalized"] == 1
    assert cov["rescued"] == 1               # 別枠計上
    assert cov["captured"] == 0              # capture_rate には入れない（自己言及防止）
    assert cov["signaled"] == 0              # signaled からは抜ける
    assert cov["capture_rate"] == 0.0
    st = {d["code"]: d["status"] for d in json.loads(cov["detail"])}
    assert st["R601"] == "delivered_rescue"
    assert "逆引き昇格で1件を翌朝救済" in cov["proposal"]
