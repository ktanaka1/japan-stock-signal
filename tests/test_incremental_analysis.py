"""逐次保存・実行上限・リトライ修正の検証。

実行（本番D1誤接続を防ぐため FORCE_LOCAL_DB=1 + DB_PATH を必須）:
    FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db DRY_RUN=true \
        .venv/bin/python -m pytest tests/test_incremental_analysis.py -v
"""
from __future__ import annotations

import os

import httpx
import pytest


# --- 環境ガード: 本番D1へ繋がない（CLAUDE.md） ---
assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"


def _fresh_db():
    """テストDBを初期化してから lifespan(migrate) を発火させる。"""
    db_path = os.environ["DB_PATH"]
    if os.path.exists(db_path):
        os.remove(db_path)
    from fastapi.testclient import TestClient
    from webhook.app import app

    # with ブロックで lifespan を発火させ migrate（新設定の既定値INSERT含む）を走らせる
    with TestClient(app):
        pass


def _make_article(repo, title: str, url: str):
    from store.models import Article

    art, _ = repo.save(Article(title=title, body="本文", url=url))
    return art


# --------------------------------------------------------------------------
# 新設定の既定値INSERT
# --------------------------------------------------------------------------
def test_migration_seeds_max_articles_per_run():
    _fresh_db()
    from store.db import execute

    rows = execute(
        "SELECT value FROM settings WHERE key = ?", ("max_articles_per_run",)
    ).rows
    assert rows, "migration 006 が max_articles_per_run をINSERTしていない"
    assert rows[0]["value"] == "200"

    # settings.get_all のデフォルトにも含まれること
    from store import settings as cfg

    assert cfg.get_all()["max_articles_per_run"] == "200"


# --------------------------------------------------------------------------
# 逐次保存（途中打ち切り耐性）
# --------------------------------------------------------------------------
def test_incremental_save_on_abort(monkeypatch):
    """複数バッチのうち先頭は成功・後続は連続失敗で打ち切り。
    成功バッチだけが analyses に保存され is_read=1 になっていること。"""
    _fresh_db()
    os.environ.pop("DRY_RUN", None)  # 本テストは analyze_batch をモックするので実LLMは呼ばない

    from store import repository, analyses
    from store.db import execute
    from monitor import agent
    from monitor.analyzer import AnalysisResult

    arts = [_make_article(repository, f"記事{i}", f"http://x/{i}") for i in range(6)]
    ok_ids = {arts[0].id, arts[1].id}

    def fake_batches(articles, **kwargs):
        # バッチ1: 成功（先頭2件）
        yield [(arts[0], AnalysisResult("positive", "s", "r", [{"name": "A", "code": "1111"}])),
               (arts[1], AnalysisResult("neutral", "s", "r", []))]
        # バッチ2,3: 連続失敗（result=None）→打ち切り想定（max_failures=2を模す）
        yield [(arts[2], None), (arts[3], None)]
        yield [(arts[4], None), (arts[5], None)]

    monkeypatch.setattr(agent, "analyze_batch", fake_batches)

    agent.run()

    # 成功バッチの2件だけ保存・既読
    saved = {r["article_id"] for r in execute("SELECT article_id FROM article_analyses").rows}
    assert saved == ok_ids
    read = {r["id"] for r in execute("SELECT id FROM articles WHERE is_read = 1").rows}
    assert read == ok_ids
    # 失敗分は未読のまま
    unread = repository.get_unread()
    assert {a.id for a in unread} == {arts[2].id, arts[3].id, arts[4].id, arts[5].id}
    # シグナルは positive+stocks の1件だけ
    sig = execute("SELECT article_id FROM signals").rows
    assert {r["article_id"] for r in sig} == {arts[0].id}


# --------------------------------------------------------------------------
# analyze_batch ジェネレータの打ち切り（max_failures到達でyield停止）
# --------------------------------------------------------------------------
def test_analyze_batch_aborts_after_max_failures(monkeypatch):
    os.environ.pop("DRY_RUN", None)
    from monitor import analyzer
    from store.models import Article

    # 全バッチを失敗させる
    monkeypatch.setattr(
        analyzer, "_llm_analyze_batch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    monkeypatch.setattr(analyzer.time, "sleep", lambda s: None)

    # 本文なし記事10件 → batch_size=2 で5チャンク。max_failures=2 なら2チャンクで打ち切り。
    arts = [Article(id=i, title=f"t{i}", body="b", url=f"u{i}") for i in range(10)]
    gen = analyzer.analyze_batch(arts, batch_size=2, max_failures=2, batch_interval=0)
    chunks = list(gen)

    assert len(chunks) == 2, "max_failures=2 で2チャンク後に停止していない"
    # 全 result が None（失敗）であること
    assert all(r is None for c in chunks for (_, r) in c)


# --------------------------------------------------------------------------
# 冪等性（同じ未読集合で2回 run しても重複INSERTされない）
# --------------------------------------------------------------------------
def test_idempotent_rerun():
    _fresh_db()
    os.environ["DRY_RUN"] = "true"  # Geminiをモック化

    from store import repository
    from store.db import execute
    from monitor import agent

    arts = [_make_article(repository, f"決算{i}", f"http://y/{i}") for i in range(3)]

    agent.run()
    n1 = execute("SELECT COUNT(*) AS c FROM article_analyses").rows[0]["c"]
    s1 = execute("SELECT COUNT(*) AS c FROM signals").rows[0]["c"]
    assert n1 == 3  # DRY_RUNは全件positive+stocks

    # 一旦未読へ戻して同じ集合を再実行 → INSERT OR IGNORE で重複しない
    execute("UPDATE articles SET is_read = 0", ())
    agent.run()
    n2 = execute("SELECT COUNT(*) AS c FROM article_analyses").rows[0]["c"]
    s2 = execute("SELECT COUNT(*) AS c FROM signals").rows[0]["c"]
    assert n2 == n1, "article_analyses が重複INSERTされている"
    assert s2 == s1, "signals が重複INSERTされている"

    del os.environ["DRY_RUN"]


# --------------------------------------------------------------------------
# 実行上限（未読がN件あっても max_articles_per_run 件だけ処理）
# --------------------------------------------------------------------------
def test_max_articles_per_run_limit():
    _fresh_db()
    os.environ["DRY_RUN"] = "true"

    from store import repository, settings as cfg
    from store.db import execute
    from monitor import agent

    cfg.set_value("max_articles_per_run", "3")

    for i in range(8):
        _make_article(repository, f"記事{i}", f"http://z/{i}")

    agent.run()

    processed = execute("SELECT COUNT(*) AS c FROM article_analyses").rows[0]["c"]
    assert processed == 3, f"上限3のはずが{processed}件処理された"
    read = execute("SELECT COUNT(*) AS c FROM articles WHERE is_read = 1").rows[0]["c"]
    assert read == 3
    assert len(repository.get_unread()) == 5

    del os.environ["DRY_RUN"]


# --------------------------------------------------------------------------
# fetcher: リトライ例外の修正
# --------------------------------------------------------------------------
def test_fetcher_404_no_retry(monkeypatch):
    """404 は HTTPStatusError として即raise。sleep は呼ばれない（リトライしない）。"""
    from collector import fetcher

    sleeps = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: sleeps.append(s))

    req = httpx.Request("GET", "http://example/x")

    def fake_get(url, **kwargs):
        return httpx.Response(404, request=req)

    monkeypatch.setattr(fetcher.httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        fetcher._http_get_json("http://example/x")
    assert sleeps == [], "404でリトライ（sleep）が発生している"


def test_fetcher_connect_timeout_three_attempts(monkeypatch):
    """ConnectTimeout（RequestError）は計3試行（sleep 2回）後に raise。"""
    from collector import fetcher

    sleeps = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        raise httpx.ConnectTimeout("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(fetcher.httpx, "get", fake_get)

    with pytest.raises(httpx.ConnectTimeout):
        fetcher._http_get_json("http://example/x")
    assert calls["n"] == 3, "計3試行ではない"
    assert len(sleeps) == 2, "バックオフsleepが2回でない"


def test_fetcher_5xx_three_attempts(monkeypatch):
    """5xx は status判定で計3試行後、枯渇 raise_for_status で HTTPStatusError。"""
    from collector import fetcher

    sleeps = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}
    req = httpx.Request("GET", "http://example/x")

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return httpx.Response(503, request=req)

    monkeypatch.setattr(fetcher.httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        fetcher._http_get_json("http://example/x")
    assert calls["n"] == 3, "5xxで計3試行ではない"
    assert len(sleeps) == 2, "5xxバックオフsleepが2回でない"
