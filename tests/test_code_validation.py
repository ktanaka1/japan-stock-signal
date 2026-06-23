"""Yahooによる証券コード実在チェック（捏造コードの除外）のテスト。"""
import httpx

from monitor import quotes
from monitor import agent


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _chart_payload(close=100.0, prev=90.0):
    return {"chart": {"result": [{
        "timestamp": [1, 2],
        "indicators": {"quote": [{"close": [prev, close], "volume": [10, 20]}]},
    }]}}


# ---- quotes._get_quote_and_status のステータス意味 -------------------------------

def test_status_200_returns_quote(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _Resp(200, _chart_payload()))
    q, status = quotes._get_quote_and_status("7203")
    assert status == 200 and q is not None


def test_status_404_no_quote(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _Resp(404))
    q, status = quotes._get_quote_and_status("0000")
    assert status == 404 and q is None


def test_transient_returns_none_status(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectTimeout("timeout")
    monkeypatch.setattr(quotes.httpx, "get", boom)
    q, status = quotes._get_quote_and_status("7203")
    assert q is None and status is None  # transient は実在判定不能


# ---- enrich の _nonexistent マーキング（404のみ） --------------------------------

def test_enrich_marks_nonexistent_only_on_404(monkeypatch):
    def fake(code):
        return (None, 404) if code == "0000" else (quotes.Quote(100.0, 1.0, 10, "2026-06-23"), 200)
    monkeypatch.setattr(quotes, "_get_quote_and_status", lambda c: fake(c))
    stocks = [{"code": "7203", "name": "トヨタ"}, {"code": "0000", "name": "捏造"}]
    quotes.enrich(stocks)
    assert stocks[0].get("_nonexistent") is None
    assert stocks[1].get("_nonexistent") is True
    assert stocks[0]["close"] == 100.0  # 実在側は価格付与


def test_enrich_transient_not_marked(monkeypatch):
    monkeypatch.setattr(quotes, "_get_quote_and_status", lambda c: (None, None))
    stocks = [{"code": "7203", "name": "トヨタ"}]
    quotes.enrich(stocks)
    assert "_nonexistent" not in stocks[0]  # transient では捏造扱いしない


# ---- agent._enrich_and_validate の除外＋権威コード保護 ---------------------------

class _Art:
    def __init__(self, id=1, security_code=None):
        self.id = id
        self.security_code = security_code


def test_drops_nonexistent_keeps_valid(monkeypatch):
    def fake_enrich(stocks):
        for s in stocks:
            if s["code"] == "0000":
                s["_nonexistent"] = True
    monkeypatch.setattr(agent.quotes, "enrich", fake_enrich)
    stocks = [{"code": "7203", "name": "トヨタ"}, {"code": "0000", "name": "捏造"}]
    out = agent._enrich_and_validate(_Art(), stocks)
    codes = [s["code"] for s in out]
    assert "7203" in codes and "0000" not in codes
    assert all("_nonexistent" not in s for s in out)  # 一時フラグは残さない


def test_authoritative_code_kept_even_if_404(monkeypatch):
    """TDnet権威コードは404でも残す（Yahoo未収録の実在銘柄を誤除外しない）。"""
    def fake_enrich(stocks):
        for s in stocks:
            s["_nonexistent"] = True  # 全部404扱い
    monkeypatch.setattr(agent.quotes, "enrich", fake_enrich)
    art = _Art(security_code="65330")  # 権威コード 6533
    stocks = [{"code": "6533", "name": "x"}, {"code": "0000", "name": "捏造"}]
    out = agent._enrich_and_validate(art, stocks)
    codes = [s["code"] for s in out]
    assert codes == ["6533"]  # 権威コードのみ残る
