"""
LINE配信の堅牢化テスト（3点）。pytest非依存の自前ハーネス（プロジェクト慣習に合わせる）。

1. store.recipients.add() のuserId形式検証（不正IDはINSERTしない）
2. notifier._multicast() が不正IDを除外して送信する（全体を落とさない）
3. _multicast の失敗詳細（HTTPステータス＋本文）が digest_runs.error_detail に伝播する

実行（本番D1誤接続厳禁。必ずローカルSQLite強制）:
  FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db .venv/bin/python tests/test_line_robustness.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from unittest import mock

# 鉄壁ガード（本番D1誤接続防止）。環境変数が未指定でも必ずローカルSQLiteへ。
os.environ["FORCE_LOCAL_DB"] = "1"
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "test.db"))

# プロジェクトルートを import パスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

VALID_ID = "U" + "0123456789abcdef0123456789abcdef"          # U + 32桁16進小文字
VALID_ID_2 = "U" + "fedcba9876543210fedcba9876543210"
INVALID_ID = "U_test_dummy"                                  # 本番で混入した不正ID
INVALID_ID_UPPER = "U" + "0123456789ABCDEF0123456789ABCDEF"  # 大文字は不正


def _fresh_db():
    """テストごとに空のSQLiteを使う。db.DB_PATH も上書きする（import時束縛のため）。"""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db_path)  # migrate に作らせる
    os.environ["FORCE_LOCAL_DB"] = "1"
    os.environ["DB_PATH"] = db_path
    import store.db as db
    db.DB_PATH = db_path
    return db_path


# ---------------------------------------------------------------------------
# 1. recipients.add の形式検証
# ---------------------------------------------------------------------------

def test_recipients_add_rejects_invalid():
    _fresh_db()
    from store.db import migrate
    from store import recipients
    migrate()

    recipients.add(VALID_ID)
    recipients.add(INVALID_ID)          # 弾かれる（U_test_dummy）
    recipients.add(INVALID_ID_UPPER)    # 大文字は弾かれる
    recipients.add("")                  # 空も弾かれる

    all_ids = recipients.get_all()
    assert all_ids == [VALID_ID], all_ids
    assert INVALID_ID not in all_ids


def test_recipients_add_does_not_raise_on_invalid():
    """例外を投げないこと（webhookハンドラを巻き込まない）。"""
    _fresh_db()
    from store.db import migrate
    from store import recipients
    migrate()
    recipients.add(INVALID_ID)  # 例外が出ればテスト失敗
    assert recipients.get_all() == []


def test_is_valid_user_id():
    from store import recipients
    assert recipients.is_valid_user_id(VALID_ID) is True
    assert recipients.is_valid_user_id(INVALID_ID) is False
    assert recipients.is_valid_user_id(INVALID_ID_UPPER) is False
    assert recipients.is_valid_user_id("") is False
    assert recipients.is_valid_user_id(VALID_ID + "x") is False  # 桁あふれ


# ---------------------------------------------------------------------------
# 2. _multicast の不正ID除外（全体を落とさない）
# ---------------------------------------------------------------------------

def test_multicast_filters_invalid_and_sends_only_valid():
    from monitor import notifier
    os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"
    os.environ.pop("DRY_RUN", None)

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["to"] = json["to"]
        return mock.Mock(status_code=200, text="{}")

    with mock.patch.object(notifier.httpx, "post", fake_post):
        res = notifier._multicast("hello", [VALID_ID, INVALID_ID, VALID_ID_2])

    # 正規IDだけが to に載る（不正IDは除外）
    assert captured["to"] == [VALID_ID, VALID_ID_2], captured.get("to")
    assert res.ok == 1 and res.error == 0
    assert res.error_details == []


def test_multicast_all_invalid_returns_zero_zero():
    """全員不正なら送信せず (0,0)。呼び出し側で no_recipients 相当に扱える。"""
    from monitor import notifier
    os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"

    def fail_post(*a, **k):
        raise AssertionError("httpx.post must not be called when all recipients invalid")

    with mock.patch.object(notifier.httpx, "post", fail_post):
        res = notifier._multicast("hello", [INVALID_ID, "U_test_dummy2"])
    assert res.ok == 0 and res.error == 0
    assert res.error_details == []


def test_multicast_captures_400_body():
    """400時にHTTPステータスとレスポンス本文を error_details に積む。"""
    from monitor import notifier
    os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"

    body = '{"message":"The request body has 1 error(s)"}'
    with mock.patch.object(notifier.httpx, "post",
                           lambda *a, **k: mock.Mock(status_code=400, text=body)):
        res = notifier._multicast("hello", [VALID_ID])
    assert res.ok == 0 and res.error == 1
    assert res.error_details == [f"LINE 400: {body}"], res.error_details


# ---------------------------------------------------------------------------
# 3. digest.run() の error_detail に実エラー本文が入る
# ---------------------------------------------------------------------------

def test_digest_run_records_line_400_body_in_error_detail():
    _fresh_db()
    from store.db import migrate
    from store import recipients, digest_runs
    migrate()
    recipients.add(VALID_ID)

    from monitor import digest
    os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"
    os.environ.pop("DRY_RUN", None)

    body = '{"message":"The property, \'to[1]\', in the request body is invalid"}'
    marked = {}
    sent_alerts = {}

    one_signal = [{"id": 42, "sentiment": "positive", "summary": "材料",
                   "stocks": [{"code": "9999", "name": "テスト商事"}],
                   "url": "https://example.com/x"}]

    with mock.patch.object(digest.signals, "get_unnotified", lambda: one_signal), \
         mock.patch.object(digest.signals, "mark_notified",
                           lambda ids: marked.setdefault("ids", ids)), \
         mock.patch.object(digest.mailer, "send_digest_alert",
                           lambda error_detail, run_at_jst: sent_alerts.update(
                               error_detail=error_detail, run_at_jst=run_at_jst)), \
         mock.patch("monitor.notifier.httpx.post",
                    lambda *a, **k: mock.Mock(status_code=400, text=body)):
        digest.run()

    # 失敗時はシグナルを通知済みにしない（次回再送）
    assert "ids" not in marked, marked

    latest = digest_runs.get_recent(5)[0]
    assert latest["status"] == "error", latest["status"]
    assert latest["line_error"] >= 1, latest["line_error"]
    assert "LINE 400:" in latest["error_detail"], latest["error_detail"]
    assert body in latest["error_detail"], latest["error_detail"]
    assert "LINE 400:" in sent_alerts["error_detail"], sent_alerts


def test_digest_run_success_marks_notified():
    _fresh_db()
    from store.db import migrate
    from store import recipients, digest_runs
    migrate()
    recipients.add(VALID_ID)

    from monitor import digest
    os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"
    os.environ.pop("DRY_RUN", None)

    marked = {}
    one_signal = [{"id": 7, "sentiment": "positive", "summary": "材料", "impact": 5,
                   "stocks": [{"code": "1", "name": "A"}], "url": "u"}]

    with mock.patch.object(digest.signals, "get_unnotified", lambda: one_signal), \
         mock.patch.object(digest.signals, "mark_notified",
                           lambda ids: marked.setdefault("ids", ids)), \
         mock.patch.object(digest.mailer, "send_digest_alert", lambda **k: None), \
         mock.patch("monitor.notifier.httpx.post",
                    lambda *a, **k: mock.Mock(status_code=200, text="{}")):
        digest.run()

    assert marked["ids"] == [7], marked
    latest = digest_runs.get_recent(1)[0]
    assert latest["status"] == "ok", latest["status"]
    assert latest["error_detail"] is None, latest["error_detail"]


def test_digest_run_all_invalid_recipients_is_no_recipients():
    """受信者が不正IDのみ → 送信先ゼロ → no_recipients 相当でシグナル未マーク。"""
    _fresh_db()
    from store.db import migrate
    from store import digest_runs
    migrate()

    from monitor import digest
    os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "dummy-token"
    os.environ.pop("DRY_RUN", None)

    marked = {}
    one_signal = [{"id": 9, "sentiment": "positive", "summary": "m",
                   "stocks": [{"code": "1", "name": "A"}], "url": "u"}]

    def fail_post(*a, **k):
        raise AssertionError("must not call LINE when all recipients invalid")

    with mock.patch.object(digest, "get_recipients", lambda: [INVALID_ID]), \
         mock.patch.object(digest.signals, "get_unnotified", lambda: one_signal), \
         mock.patch.object(digest.signals, "mark_notified",
                           lambda ids: marked.setdefault("ids", ids)), \
         mock.patch.object(digest.mailer, "send_digest_alert", lambda **k: None), \
         mock.patch("monitor.notifier.httpx.post", fail_post):
        digest.run()

    assert "ids" not in marked, marked  # 送れていないのでマークしない
    latest = digest_runs.get_recent(1)[0]
    assert latest["status"] == "no_recipients", latest["status"]


# ---------------------------------------------------------------------------
# TestClient で lifespan→migrate が発火し、webhook経由のadd検証が効くこと
# ---------------------------------------------------------------------------

def test_webhook_lifespan_migrates_and_filters():
    _fresh_db()
    from webhook.app import app
    with TestClient(app) as c:  # lifespan→migrate 発火
        assert c is not None
        from store import recipients
        recipients.add(VALID_ID)
        recipients.add(INVALID_ID)
        assert recipients.get_all() == [VALID_ID], recipients.get_all()


# ---------------------------------------------------------------------------
# ハーネス
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
