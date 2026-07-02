"""TDnet取得の連続失敗を検知してメール障害アラートを出すロジックのテスト。"""
import os

# --- 環境ガード: 本番D1へ繋がない（CLAUDE.md） ---
assert os.getenv("FORCE_LOCAL_DB") == "1", "FORCE_LOCAL_DB=1 を必須にする（本番D1誤接続防止）"
assert os.getenv("DB_PATH"), "DB_PATH を指定すること"

from store.db import migrate
from store import settings as cfg


def _fresh():
    migrate()
    cfg.set_value("tdnet_fail_streak", "0")
    cfg.set_value("tdnet_alerted", "0")
    cfg.set_value("tdnet_fail_alert_threshold", "3")


def _patch_mailer(monkeypatch):
    """mailer の2通を記録するスタブに差し替え、(alerts, recoveries) を返す。"""
    from collector import agent
    alerts, recoveries = [], []
    monkeypatch.setattr(agent.mailer, "send_collector_alert",
                        lambda **k: alerts.append(k))
    monkeypatch.setattr(agent.mailer, "send_collector_recovery",
                        lambda **k: recoveries.append(k))
    return alerts, recoveries


def test_no_alert_below_threshold(monkeypatch):
    _fresh()
    from collector import agent
    alerts, _ = _patch_mailer(monkeypatch)
    agent._check_tdnet_health(False, "t1")
    agent._check_tdnet_health(False, "t2")
    assert cfg.get("tdnet_fail_streak") == "2"
    assert alerts == []  # 閾値3未満は送らない


def test_alert_once_at_threshold(monkeypatch):
    _fresh()
    from collector import agent
    alerts, _ = _patch_mailer(monkeypatch)
    agent._check_tdnet_health(False, "t1")
    agent._check_tdnet_health(False, "t2")
    agent._check_tdnet_health(False, "t3")  # 3回目で閾値到達→送信
    agent._check_tdnet_health(False, "t4")  # 継続失敗でも再送しない
    assert len(alerts) == 1
    assert alerts[0]["fail_streak"] == 3
    assert cfg.get("tdnet_alerted") == "1"
    assert cfg.get("tdnet_fail_streak") == "4"


def test_recovery_sends_once_and_resets(monkeypatch):
    _fresh()
    from collector import agent
    alerts, recoveries = _patch_mailer(monkeypatch)
    for t in ("t1", "t2", "t3"):
        agent._check_tdnet_health(False, t)  # アラート送信状態にする
    assert len(alerts) == 1

    agent._check_tdnet_health(True, "t5")    # 復旧→復旧通知1通＋状態リセット
    assert len(recoveries) == 1
    assert recoveries[0]["fail_streak"] == 3
    assert cfg.get("tdnet_fail_streak") == "0"
    assert cfg.get("tdnet_alerted") == "0"

    agent._check_tdnet_health(True, "t6")    # 正常継続では何も送らない
    assert len(recoveries) == 1


def test_ok_without_prior_failure_is_silent(monkeypatch):
    _fresh()
    from collector import agent
    alerts, recoveries = _patch_mailer(monkeypatch)
    agent._check_tdnet_health(True, "t1")
    assert alerts == [] and recoveries == []


def test_recovery_before_alert_no_recovery_mail(monkeypatch):
    """閾値未満の失敗からの復旧では、アラート未送信なので復旧通知も送らない。"""
    _fresh()
    from collector import agent
    alerts, recoveries = _patch_mailer(monkeypatch)
    agent._check_tdnet_health(False, "t1")   # streak=1（アラート未送信）
    agent._check_tdnet_health(True, "t2")    # 復旧するが通知不要
    assert alerts == [] and recoveries == []
    assert cfg.get("tdnet_fail_streak") == "0"
