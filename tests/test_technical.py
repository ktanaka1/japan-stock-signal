"""テクニカル版 朝のシグナル（technical パッケージ）のテスト。既存テーブルは読み取りのみ。"""
import json
import os

# --- 環境ガード: 本番D1へ繋がない（CLAUDE.md） ---
assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"

from store.db import migrate, execute
from monitor import quotes
from feedback import ranking
from technical import agent


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _chart(volumes, closes):
    return {"chart": {"result": [{
        "timestamp": list(range(1, len(closes) + 1)),
        "indicators": {"quote": [{"close": closes, "volume": volumes}]},
    }]}}


# ---- 出来高急増の算出 -----------------------------------------------------------

def test_volume_surge_computed(monkeypatch):
    # 直近4日平均=100、最新=500 → surge 5.0。change_pct = (120-100)/100=+20%
    payload = _chart(volumes=[100, 100, 100, 100, 500], closes=[100, 100, 100, 100, 120])
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _Resp(200, payload))
    # 場中未確定除外は当日バー判定。tsは過去扱い(小さい値)なので残る前提
    m = quotes.get_daily_metrics("9444")
    assert m["surge"] == 5.0
    assert m["change_pct"] == 20.0
    assert m["volume"] == 500


def test_metrics_none_on_404(monkeypatch):
    monkeypatch.setattr(quotes.httpx, "get", lambda *a, **k: _Resp(404))
    assert quotes.get_daily_metrics("0000") is None


# ---- スキャン（フィルタ＋dedup） -------------------------------------------------

def _item(code, name, pct, etf=False, large=False):
    return ranking.RankItem(rank=1, code=code, name=name, market="東証STD",
                            change_pct=pct, is_etf=etf, is_large_cap=large)


def _metrics(surge, close=1000.0, volume=200000):
    # turnover = close*volume。既定 1000*200000=2億 > フロア1億。
    return {"surge": surge, "close": close, "volume": volume,
            "change_pct": 12.0, "asof": "2026-06-23"}


def test_scan_filters(monkeypatch):
    items = [
        _item("1111", "急増大幅高", 12.0),                    # change OK, surge OK → 採用
        _item("2222", "値幅不足", 3.0),                       # change<5 → 除外
        _item("3333", "出来高不足", 10.0),                    # surge<2 → 除外
        _item("4444", "ETF", 20.0, etf=True),                 # 母集団外 → 除外
        _item("5555", "大型", 20.0, large=True),              # 母集団外 → 除外
        _item("6666", "本体配信済", 15.0),                    # dedup → 除外
        _item("7777", "薄商い", 15.0),                        # 売買代金フロア未満 → 除外
    ]
    monkeypatch.setattr(agent.ranking, "fetch_ranking", lambda *a, **k: items)
    metrics = {
        "1111": _metrics(4.0),
        "3333": _metrics(1.2),
        "6666": _metrics(9.0),
        "7777": _metrics(9.0, close=50.0, volume=1000),       # 5万円 ≪ 1億 → 弾く
    }
    monkeypatch.setattr(agent.quotes, "get_daily_metrics",
                        lambda code: metrics.get(code, _metrics(3.0)))
    picks = agent._scan(min_change=5.0, min_surge=2.0, top_n=30, exclude={"6666"},
                        min_turnover_yen=1e8, scan_volume=False, vol_min_change=3.0,
                        vol_min_surge=2.0)
    codes = [p["code"] for p in picks]
    assert codes == ["1111"]
    assert picks[0]["source"] == "up"
    assert picks[0]["turnover"] == 2e8


def test_scan_adds_volume_source(monkeypatch):
    # up は対象なし、出来高ランキングから上昇方向＋surge＋代金を満たす銘柄を拾う。
    up_items = [_item("1111", "値幅不足", 2.0)]                # change<5 → up採用なし
    vol_items = [
        _item("8888", "出来高先行", 4.0),                     # change≥3 surge OK 代金OK → vol採用
        _item("9999", "下落", -1.0),                          # 上昇方向でない → 除外
    ]
    def _fetch(kind="up", **k):
        return vol_items if kind == "volume" else up_items
    monkeypatch.setattr(agent.ranking, "fetch_ranking", _fetch)
    metrics = {"8888": _metrics(3.0), "9999": _metrics(5.0)}
    monkeypatch.setattr(agent.quotes, "get_daily_metrics",
                        lambda code: metrics.get(code, _metrics(3.0)))
    picks = agent._scan(min_change=5.0, min_surge=2.0, top_n=30, exclude=set(),
                        min_turnover_yen=1e8, scan_volume=True, vol_min_change=3.0,
                        vol_min_surge=2.0)
    assert [(p["code"], p["source"]) for p in picks] == [("8888", "vol")]


def test_scan_vol_surge_separated(monkeypatch):
    # vol源はup源より緩いsurge下限で通る（出来高上位は自平均比の急増が出にくいため分離）。
    # surge1.5: up源(下限2.0)では落ち、vol源(下限1.3)では採用される。
    up_items = [_item("1111", "急増不足", 12.0)]               # change OK だが surge1.5<2.0 → up不採用
    vol_items = [_item("8888", "商い恒常多", 4.0)]             # surge1.5≥1.3 → vol採用
    def _fetch(kind="up", **k):
        return vol_items if kind == "volume" else up_items
    monkeypatch.setattr(agent.ranking, "fetch_ranking", _fetch)
    monkeypatch.setattr(agent.quotes, "get_daily_metrics", lambda code: _metrics(1.5))
    picks = agent._scan(min_change=5.0, min_surge=2.0, top_n=30, exclude=set(),
                        min_turnover_yen=1e8, scan_volume=True, vol_min_change=3.0,
                        vol_min_surge=1.3)
    assert [(p["code"], p["source"]) for p in picks] == [("8888", "vol")]


def test_scan_dedup_up_priority(monkeypatch):
    # 同一コードが up/volume 両方に出ても1件・up優先。
    same = [_item("5050", "両ランクイン", 12.0)]
    monkeypatch.setattr(agent.ranking, "fetch_ranking", lambda *a, **k: same)
    monkeypatch.setattr(agent.quotes, "get_daily_metrics", lambda code: _metrics(4.0))
    picks = agent._scan(min_change=5.0, min_surge=2.0, top_n=30, exclude=set(),
                        min_turnover_yen=1e8, scan_volume=True, vol_min_change=3.0,
                        vol_min_surge=2.0)
    assert len(picks) == 1 and picks[0]["source"] == "up"


def test_delivered_today_codes(monkeypatch):
    migrate()
    execute("INSERT INTO signals (article_id, sentiment, summary, stocks, url, impact, created_at, notified_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (5001, "positive", "s", json.dumps([{"code": "6533", "name": "x"}]), "u", 4,
             "2026-06-23 00:00:00", "2026-06-23 22:40:00"))  # JST 6/24 07:40 配信
    codes = agent._delivered_today_codes("2026-06-24")
    assert "6533" in codes


# ---- 配信フォーマット -----------------------------------------------------------

def test_format_pick():
    p = {"code": "9444", "name": "トーシンHD", "change_pct": 34.2, "surge": 5.8, "close": 271.0}
    s = agent._format_pick(p)
    assert "トーシンHD(9444)" in s and "前日 +34.2%" in s and "出来高 5.8倍" in s
    assert "📈値上り" in s


def test_format_pick_volume_source():
    p = {"code": "8888", "name": "出来高株", "change_pct": 4.0, "surge": 3.0,
         "close": 1500.0, "turnover": 3.5e8, "source": "vol"}
    s = agent._format_pick(p)
    assert "🔊出来高" in s and "代金 3.5億" in s


# ---- run() エンドツーエンド（モック） -------------------------------------------

def test_run_records_and_sends(monkeypatch):
    migrate()
    monkeypatch.setattr(agent, "get_recipients", lambda: ["U" + "0" * 32])
    monkeypatch.setattr(agent.ranking, "fetch_ranking",
                        lambda *a, **k: [_item("1111", "急増", 12.0)])
    monkeypatch.setattr(agent.quotes, "get_daily_metrics",
                        lambda code: {"surge": 4.0, "close": 1000.0, "volume": 200000,
                                      "change_pct": 12.0, "asof": "2026-06-23"})
    sent = []

    class _Res:
        ok, error, error_details = 1, 0, []
    monkeypatch.setattr(agent, "send_text", lambda text, rec: (sent.append(text) or _Res()))

    agent.run()

    row = execute("SELECT * FROM technical_runs ORDER BY id DESC LIMIT 1").rows[0]
    assert row["status"] == "ok" and row["pick_count"] == 1
    assert "1111" in sent[0]
    assert json.loads(row["picks"])[0]["code"] == "1111"
