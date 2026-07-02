"""捕捉率フィードバック（feedback パッケージ）のテスト。既存テーブルは読み取りのみ。"""
import json
import os

# --- 環境ガード: 本番D1へ繋がない（CLAUDE.md） ---
assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"

from store.db import migrate, execute
from feedback import ranking, matcher, proposals


# ---- ランキングのパース・正規化 -------------------------------------------------

def _fixture_html(results):
    state = {"mainRankingList": {"results": results}}
    return (
        "<html><body><script>window.__PRELOADED_STATE__ = "
        + json.dumps(state, ensure_ascii=False)
        + ";</script></body></html>"
    )


def _result(rank, code, name, market, pct):
    return {
        "rank": str(rank), "stockCode": code, "stockName": name, "marketName": market,
        "rankingResult": {"changePriceRate": {"changePriceRate": pct, "volume": "1,000"}},
    }


def test_parse_state_extracts_results():
    html = _fixture_html([_result(1, "6533", "オーケストラHD", "東証PRM", "+20.97")])
    results = ranking._parse_state(html)
    assert len(results) == 1 and results[0]["stockCode"] == "6533"


def test_to_item_flags_etf_and_large_cap():
    html = _fixture_html([
        _result(1, "6659", "メディアリンクス", "東証STD", "+33.96"),  # 中小型・対象
        _result(2, "1360", "日経ベア2倍ETF", "東証ETF", "+2.18"),       # ETF→対象外
        _result(3, "9432", "NTT", "東証PRM", "+0.14"),                  # 大型株→対象外
    ])
    items = [ranking._to_item(r) for r in ranking._parse_state(html)]
    by_code = {it.code: it for it in items}
    assert by_code["6659"].in_universe is True
    assert by_code["1360"].is_etf is True and by_code["1360"].in_universe is False
    assert by_code["9432"].is_large_cap is True and by_code["9432"].in_universe is False
    assert by_code["6659"].change_pct == 33.96


# ---- 分類ロジック（純関数） ----------------------------------------------------

def _items(*codes):
    return [ranking.RankItem(rank=i + 1, code=c, name=c, market="東証STD",
                             change_pct=10.0, is_etf=False, is_large_cap=False)
            for i, c in enumerate(codes)]


def test_classify_priority():
    items = _items("1111", "2222", "3333", "4444", "5555")
    detail = matcher.classify(
        items,
        delivered={"1111"},
        filtered={"2222"},
        analyzed={"1111", "3333"},  # 1111は配信済みが優先される
        tech_delivered={"5555", "2222"},  # 5555はテク配信。2222はニュース側未配信より優先
    )
    st = {d["code"]: d["status"] for d in detail}
    assert st["1111"] == matcher.STATUS_DELIVERED
    assert st["5555"] == matcher.STATUS_DELIVERED_TECH
    assert st["2222"] == matcher.STATUS_DELIVERED_TECH  # テク配信 > 未配信
    assert st["3333"] == matcher.STATUS_NEUTRAL
    assert st["4444"] == matcher.STATUS_NOT_COLLECTED


def test_count_universe_excludes_non_universe():
    items = [
        ranking.RankItem(1, "1111", "a", "東証STD", 10.0, False, False),   # 対象・delivered
        ranking.RankItem(2, "1360", "etf", "東証ETF", 9.0, True, False),    # 対象外
        ranking.RankItem(3, "9432", "ntt", "東証PRM", 8.0, False, True),    # 対象外(大型)
        ranking.RankItem(4, "2222", "b", "東証STD", 7.0, False, False),     # 対象・not_collected
    ]
    detail = matcher.classify(items, delivered={"1111"}, filtered=set(), analyzed=set())
    summary = matcher.count_universe(detail)
    assert summary["universe"] == 2          # ETF・大型は母集団から除外
    assert summary["counts"][matcher.STATUS_DELIVERED] == 1
    assert summary["capture_rate"] == 0.5    # 1/2


def test_count_universe_combines_news_and_technical():
    # 母集団3件: ニュース配信1・テク配信1・未収集1 → 捕捉率 2/3
    items = [
        ranking.RankItem(1, "1111", "a", "東証STD", 10.0, False, False),
        ranking.RankItem(2, "2222", "b", "東証STD", 9.0, False, False),
        ranking.RankItem(3, "3333", "c", "東証STD", 8.0, False, False),
    ]
    detail = matcher.classify(items, delivered={"1111"}, filtered=set(),
                              analyzed=set(), tech_delivered={"2222"})
    summary = matcher.count_universe(detail)
    assert summary["counts"][matcher.STATUS_DELIVERED] == 1
    assert summary["counts"][matcher.STATUS_DELIVERED_TECH] == 1
    assert summary["capture_rate"] == round(2 / 3, 4)  # 2チャンネル合算


# ---- 窓内ロード（ローカルDB・読み取り） -----------------------------------------

def test_load_window_reads_signals_and_analyses():
    migrate()
    # 共有DBに前回実行の行が残っていると article_id UNIQUE で衝突するため自浄する
    execute("DELETE FROM signals WHERE article_id IN (9001, 9002, 9003)")
    # 窓: 2026-06-22 06:00 〜 2026-06-23 06:00 (UTC)
    since, until = "2026-06-22 06:00:00", "2026-06-23 06:00:00"
    # 配信済みシグナル（窓内・notified あり）
    execute("INSERT INTO signals (article_id, sentiment, summary, stocks, url, impact, created_at, notified_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (9001, "positive", "s", json.dumps([{"code": "6533", "name": "x"}]), "u1", 4,
             "2026-06-22 23:00:00", "2026-06-22 23:30:00"))
    # 未配信シグナル（窓内・notified なし）
    execute("INSERT INTO signals (article_id, sentiment, summary, stocks, url, impact, created_at, notified_at)"
            " VALUES (?,?,?,?,?,?,?,NULL)",
            (9002, "positive", "s", json.dumps([{"code": "7777", "name": "y"}]), "u2", 3,
             "2026-06-22 22:00:00"))
    # 窓外シグナル（古すぎ）→ 拾わない
    execute("INSERT INTO signals (article_id, sentiment, summary, stocks, url, impact, created_at, notified_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (9003, "positive", "s", json.dumps([{"code": "8888", "name": "z"}]), "u3", 4,
             "2026-06-20 00:00:00", "2026-06-20 01:00:00"))
    # 中立分析（窓内・article fetched_at で絞る）
    execute("INSERT INTO articles (title, body, url, fetched_at, is_read) VALUES (?,?,?,?,1)",
            ("t", "", "ua1", "2026-06-22 20:00:00"))
    aid = execute("SELECT id FROM articles WHERE url='ua1'").rows[0]["id"]
    execute("INSERT INTO article_analyses (article_id, sentiment, summary, reason, stocks, became_signal, impact)"
            " VALUES (?,?,?,?,?,?,?)",
            (aid, "neutral", "s", "r", json.dumps([{"code": "5555", "name": "n"}]), 0, 2))
    # テクニカル版の配信記録（窓内・line_ok>0）→ tech_delivered に拾う
    execute("INSERT INTO technical_runs (run_at, target_date, status, pick_count, line_ok, picks)"
            " VALUES (?,?,?,?,?,?)",
            ("2026-06-22 23:00:00", "2026-06-22", "ok", 1, 1,
             json.dumps([{"code": "6600", "name": "tech"}])))
    # テクニカル版だが配信失敗(line_ok=0)→ 捕捉に数えない
    execute("INSERT INTO technical_runs (run_at, target_date, status, pick_count, line_ok, picks)"
            " VALUES (?,?,?,?,?,?)",
            ("2026-06-22 23:05:00", "2026-06-22", "error", 1, 0,
             json.dumps([{"code": "9999", "name": "failed"}])))

    delivered, filtered, analyzed, tech_delivered = matcher.load_window(since, until)
    assert "6533" in delivered
    assert "7777" in filtered
    assert "8888" not in delivered and "8888" not in filtered  # 窓外
    assert "5555" in analyzed
    assert "6600" in tech_delivered           # 配信成功したテク版pick
    assert "9999" not in tech_delivered        # 配信失敗は捕捉に数えない


# ---- 改善提案 ------------------------------------------------------------------

def test_proposal_picks_dominant_class():
    counts = {matcher.STATUS_DELIVERED: 1, matcher.STATUS_SIGNALED: 0,
              matcher.STATUS_NEUTRAL: 1, matcher.STATUS_NOT_COLLECTED: 5}
    msg = proposals.build_proposal(counts, 0.1)
    assert "収集網の外" in msg and "TDnet" in msg  # not_collected が支配的


def test_proposal_no_miss():
    counts = {matcher.STATUS_DELIVERED: 3, matcher.STATUS_SIGNALED: 0,
              matcher.STATUS_NEUTRAL: 0, matcher.STATUS_NOT_COLLECTED: 0}
    assert "取りこぼしなし" in proposals.build_proposal(counts, 1.0)


# ---- エンドツーエンド（ランキング取得をモック） ---------------------------------

def test_agent_run_records_coverage(monkeypatch):
    migrate()
    from feedback import agent
    # ランキングはモック（中小型2件＋ETF1件）。1件は配信済み相当のコードにする。
    fake = [
        ranking.RankItem(1, "6533", "オーケストラHD", "東証PRM", 21.0, False, False),
        ranking.RankItem(2, "1360", "ETF", "東証ETF", 5.0, True, False),
        ranking.RankItem(3, "4444", "未収集", "東証STD", 12.0, False, False),
        ranking.RankItem(4, "7700", "テク配信", "東証STD", 11.0, False, False),
    ]
    monkeypatch.setattr(agent.ranking, "fetch_ranking", lambda *a, **k: fake)
    # 窓内に 6533 のニュース配信済み、7700 をテクニカル版配信済みとして与える
    monkeypatch.setattr(agent.matcher, "load_window",
                        lambda s, u: ({"6533"}, set(), set(), {"7700"}))

    agent.run()

    row = execute("SELECT * FROM coverage_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert row["ranking_type"] == "up"
    assert row["universe"] == 3          # ETF除外で母集団3(6533/4444/7700)
    assert row["captured"] == 1          # 6533(ニュース)
    assert row["captured_tech"] == 1     # 7700(テクニカル)
    assert row["not_collected"] == 1     # 4444
    assert row["capture_rate"] == round(2 / 3, 4)  # 2チャンネル合算
    assert "推奨レバー" in row["proposal"]
    assert "テクニカル版 1件" in row["proposal"]
