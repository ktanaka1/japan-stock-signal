"""テクニカル設定の Admin UI 保存（/admin/settings/technical）の検証。

DB直書きを廃した安全な閾値変更経路。型/範囲バリデーションと、不正時のアトミック中止を確認する。

実行（本番D1誤接続を防ぐため FORCE_LOCAL_DB=1 + DB_PATH を必須）:
    FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db ADMIN_TOKEN=t \
        .venv/bin/python -m pytest tests/test_settings_technical.py -v
"""
from __future__ import annotations

import os

import pytest

assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"

_TOKEN = "testtoken"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", _TOKEN)
    db_path = os.environ["DB_PATH"]
    if os.path.exists(db_path):
        os.remove(db_path)
    from fastapi.testclient import TestClient
    from webhook.app import app

    with TestClient(app) as c:
        c.post("/admin/login", data={"token": _TOKEN}, follow_redirects=False)
        yield c


def _post(client, **overrides):
    """既定の妥当な値に overrides を上書きして送信する。"""
    data = {
        "tech_min_change_pct": "5.0",
        "tech_min_volume_surge": "2.0",
        "tech_top_n": "30",
        "tech_min_turnover_oku": "1.0",
        "tech_vol_min_change_pct": "3.0",
        "tech_vol_min_surge": "1.3",
        "tech_scan_volume": "1",
        "tech_dedup_with_news": "1",
    }
    data.update(overrides)
    # None を渡したキーは「送信しない」（チェックボックス未チェック相当）
    data = {k: v for k, v in data.items() if v is not None}
    return client.post("/admin/settings/technical", data=data, follow_redirects=False)


def test_valid_save_persists(client):
    from store import settings as cfg
    r = _post(client, tech_min_turnover_oku="2.5", tech_top_n="40")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert cfg.get("tech_min_turnover_oku") == "2.5"
    assert cfg.get("tech_top_n") == "40"


def test_unchecked_checkbox_becomes_false(client):
    from store import settings as cfg
    # tech_scan_volume を送らない＝未チェック → "false" 固定
    _post(client, tech_scan_volume=None)
    assert cfg.get("tech_scan_volume") == "false"
    assert cfg.get("tech_dedup_with_news") == "true"


def test_out_of_range_rejected_atomically(client):
    from store import settings as cfg
    before = cfg.get("tech_min_turnover_oku")
    # 10000億超は範囲外 → 中止。他項目(top_n)も保存されない（アトミック）
    r = _post(client, tech_min_turnover_oku="99999", tech_top_n="50")
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert cfg.get("tech_min_turnover_oku") == before
    assert cfg.get("tech_top_n") != "50"


def test_non_numeric_rejected(client):
    from store import settings as cfg
    r = _post(client, tech_min_volume_surge="abc")
    assert "err=" in r.headers["location"]
    assert cfg.get("tech_min_volume_surge") == "2.0"


def test_int_field_rejects_float(client):
    from store import settings as cfg
    r = _post(client, tech_top_n="30.5")
    assert "err=" in r.headers["location"]
    assert cfg.get("tech_top_n") == "30"


def test_requires_auth():
    from fastapi.testclient import TestClient
    from webhook.app import app
    with TestClient(app) as c:  # 未ログイン
        r = c.post("/admin/settings/technical", data={}, follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
