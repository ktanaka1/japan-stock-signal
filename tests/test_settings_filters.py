"""配信フィルタ・収集ソースの Admin UI 保存（/admin/settings/filters）の検証。

隠し設定だった収集ソース(PR TIMES/EDINET)と防御フィルタ(大型株除外/最小インパクト)を
画面から変更できるようにした経路。テクニカル設定と同じアトミック検証の作法を確認する。

実行（本番D1誤接続を防ぐため FORCE_LOCAL_DB=1 + DB_PATH を必須）:
    FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db ADMIN_TOKEN=t \
        .venv/bin/python -m pytest tests/test_settings_filters.py -v
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
        "prtimes_enabled": "1",
        "edinet_enabled": "1",
        "exclude_large_cap": "1",
        "rescue_enabled": "1",
        "min_impact_for_notify": "4",
        "rescue_min_impact": "3",
    }
    data.update(overrides)
    # None を渡したキーは「送信しない」（チェックボックス未チェック相当）
    data = {k: v for k, v in data.items() if v is not None}
    return client.post("/admin/settings/filters", data=data, follow_redirects=False)


def test_settings_page_renders_four_items(client):
    r = client.get("/admin/settings")
    assert r.status_code == 200
    html = r.text
    # 4項目の input が現在値を反映して描画される
    assert 'name="prtimes_enabled"' in html
    assert 'name="edinet_enabled"' in html
    assert 'name="exclude_large_cap"' in html
    assert 'name="min_impact_for_notify"' in html
    assert 'name="rescue_enabled"' in html
    assert 'name="rescue_min_impact"' in html
    # 既定では exclude_large_cap=true なので checked、prtimes=false なので未checked付近
    assert 'value="4"' in html  # min_impact_for_notify の既定
    # edinet_enabled は統合カードに一本化。EDINETカードからは削除され、HTML中で1つだけ
    assert html.count('name="edinet_enabled"') == 1


def test_edinet_form_does_not_touch_edinet_enabled(client):
    """EDINETカードのフォーム(/settings/edinet)は edinet_enabled を上書きしないこと。

    旧仕様では checkbox 不在=false で毎回OFFに戻すフットガンがあった。有効/無効は
    統合フィルタカードに一本化し、EDINETフォームは denylist 等のEDINET固有設定だけ扱う。
    """
    from store import settings as cfg
    # 統合カードで edinet を有効化
    _post(client, edinet_enabled="1")
    assert cfg.get("edinet_enabled") == "true"

    # EDINETフォームを送信（edinet_enabled は送らない＝旧仕様ならOFFに戻っていた）
    r = client.post(
        "/admin/settings/edinet",
        data={"edinet_new_only": "1", "edinet_filer_denylist": "テスト除外"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    # edinet_enabled は true のまま、denylist だけ更新される
    assert cfg.get("edinet_enabled") == "true"
    assert cfg.get("edinet_filer_denylist") == "テスト除外"
    assert cfg.get("edinet_new_only") == "true"


def test_valid_save_persists(client):
    from store import settings as cfg
    # prtimes を on、min_impact を 3 に
    r = _post(client, prtimes_enabled="1", min_impact_for_notify="3")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert cfg.get("prtimes_enabled") == "true"
    assert cfg.get("min_impact_for_notify") == "3"


def test_unchecked_checkbox_becomes_false(client):
    from store import settings as cfg
    # exclude_large_cap を送らない＝未チェック → "false" 固定
    _post(client, exclude_large_cap=None)
    assert cfg.get("exclude_large_cap") == "false"
    # 他の bool は送っているので true
    assert cfg.get("prtimes_enabled") == "true"
    assert cfg.get("edinet_enabled") == "true"


def test_min_impact_zero_means_deliver_all(client):
    """0（=全件配信。monitor/digest.py の min_impact=0 と同義）は有効値として保存できる。"""
    from store import settings as cfg
    r = _post(client, min_impact_for_notify="0")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert cfg.get("min_impact_for_notify") == "0"


@pytest.mark.parametrize("bad", ["-1", "6", "abc"])
def test_min_impact_out_of_range_or_nonnumeric_rejected_atomically(client, bad):
    from store import settings as cfg
    before_impact = cfg.get("min_impact_for_notify")
    before_prtimes = cfg.get("prtimes_enabled")
    # 不正な min_impact → 中止。同時送信した prtimes も反映されない（アトミック）
    r = _post(client, min_impact_for_notify=bad, prtimes_enabled="1")
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert cfg.get("min_impact_for_notify") == before_impact
    assert cfg.get("prtimes_enabled") == before_prtimes


def test_requires_auth():
    from fastapi.testclient import TestClient
    from webhook.app import app
    with TestClient(app) as c:  # 未ログイン
        r = c.post("/admin/settings/filters", data={}, follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
