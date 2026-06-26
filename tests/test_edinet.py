"""EDINET 大量保有アラート（edinet パッケージ）のテスト。既存テーブルは触れない。

実行: FORCE_LOCAL_DB=1 DB_PATH=/tmp/t.db .venv/bin/python -m pytest tests/test_edinet.py
"""
import os

from store.db import migrate, execute
from store import settings as cfg
from edinet import agent, codelist


# ---- コードリスト解析 -----------------------------------------------------------

def test_codelist_parse():
    # 1行目メタ・2行目ヘッダ・証券コードは5桁(末尾0)。空はNone。
    lines = [
        "ダウンロード日時など（メタ行）",
        "ＥＤＩＮＥＴコード,提出者種別,証券コード,提出者名",
        "E00004,上場,13760,カネコ種苗株式会社",
        "E31280,その他,,ヘルスケア＆メディカル投資法人",  # 証券コード空→None
    ]
    m = codelist._parse(lines)
    assert m["E00004"] == ("1376", "カネコ種苗株式会社")
    assert m["E31280"] == (None, "ヘルスケア＆メディカル投資法人")


# ---- 候補抽出（_collect） --------------------------------------------------------

_CODE_MAP = {"E00004": ("1376", "カネコ種苗"), "E31280": (None, "ヘルスケアREIT")}


def _doc(docid, dtype="350", desc="大量保有報告書", iec="E00004", filer="××投資事業組合"):
    return {"docID": docid, "docTypeCode": dtype, "docDescription": desc,
            "issuerEdinetCode": iec, "filerName": filer, "submitDateTime": "2026-06-25 09:00"}


def _docs(*ds):
    return {d["docID"]: d for d in ds}


def test_collect_new_only_and_type_filter():
    docs = _docs(
        _doc("D1"),                                       # 350新規 → 採用
        _doc("D2", desc="変更報告書"),                     # 350変更 → new_onlyで除外
        _doc("D3", dtype="360", desc="訂正報告書"),         # 360 → 除外
    )
    picks = agent._collect(docs, _CODE_MAP, new_only=True, denylist=[])
    assert [p["doc_id"] for p in picks] == ["D1"]
    assert picks[0]["sec_code"] == "1376" and picks[0]["is_new"] is True


def test_collect_includes_change_when_not_new_only():
    docs = _docs(_doc("D1"), _doc("D2", desc="変更報告書"))
    picks = agent._collect(docs, _CODE_MAP, new_only=False, denylist=[])
    ids = sorted(p["doc_id"] for p in picks)
    assert ids == ["D1", "D2"]


def test_collect_skips_reit_empty_seccode():
    docs = _docs(_doc("D1", iec="E31280"))               # REIT＝証券コード空
    assert agent._collect(docs, _CODE_MAP, new_only=True, denylist=[]) == []


def test_collect_skips_denylist_and_unknown_issuer():
    docs = _docs(
        _doc("D1", filer="野村アセットマネジメント株式会社"),   # denylist部分一致 → 除外
        _doc("D2", iec="E99999"),                             # コードリスト未収載 → 除外
    )
    assert agent._collect(docs, _CODE_MAP, new_only=True, denylist=["野村アセットマネジメント"]) == []


# ---- run() エンドツーエンド（モック） -------------------------------------------

class _Res:
    ok, error, error_details = 1, 0, []


def _setup(monkeypatch, docs):
    migrate()
    cfg.set_value("edinet_enabled", "true")
    monkeypatch.setenv("EDINET_API_KEY", "dummy")
    monkeypatch.setattr(agent.codelist, "fetch_map", lambda: _CODE_MAP)
    monkeypatch.setattr(agent, "_fetch_documents", lambda date, key: list(docs.values()))
    monkeypatch.setattr(agent, "get_recipients", lambda: ["U" + "0" * 32])
    sent = []
    monkeypatch.setattr(agent, "send_text", lambda text, rec: (sent.append(text) or _Res()))
    return sent


def test_run_sends_new_and_records(monkeypatch):
    sent = _setup(monkeypatch, _docs(_doc("DX1")))
    agent.run()
    assert sent and "カネコ種苗" in sent[0] and "大量保有[新規]" in sent[0]
    row = execute("SELECT * FROM edinet_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert row["status"] == "ok" and row["pick_count"] == 1
    assert execute("SELECT COUNT(*) c FROM large_holdings WHERE doc_id='DX1'").rows[0]["c"] == 1


def test_run_dedup_no_resend(monkeypatch):
    sent = _setup(monkeypatch, _docs(_doc("DX2")))
    agent.run()              # 初回配信
    sent.clear()
    agent.run()              # 2回目: docID既存 → 再送しない
    assert sent == []
    row = execute("SELECT * FROM edinet_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert row["status"] == "no_picks"


def test_run_noop_when_disabled(monkeypatch):
    migrate()
    cfg.set_value("edinet_enabled", "false")
    monkeypatch.setenv("EDINET_API_KEY", "dummy")
    before = execute("SELECT COUNT(*) c FROM edinet_runs").rows[0]["c"]
    agent.run()
    after = execute("SELECT COUNT(*) c FROM edinet_runs").rows[0]["c"]
    assert before == after   # 無効時は記録もしない


def test_run_noop_without_key(monkeypatch):
    migrate()
    cfg.set_value("edinet_enabled", "true")
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    before = execute("SELECT COUNT(*) c FROM edinet_runs").rows[0]["c"]
    agent.run()
    assert execute("SELECT COUNT(*) c FROM edinet_runs").rows[0]["c"] == before
