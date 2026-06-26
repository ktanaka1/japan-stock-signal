"""テクニカル成功基準ライン割れメール（mailer.send_technical_alert）の検証。

REPORT_EMAIL 未設定ならスキップ、設定済みなら件名・本文に割れ内容を含めて送る。
SMTP は実送しないよう _send をモックする。
"""
from monitor import mailer


def test_skips_without_report_email(monkeypatch):
    monkeypatch.delenv("REPORT_EMAIL", raising=False)
    called = []
    monkeypatch.setattr(mailer, "_send", lambda *a, **k: called.append(a))
    mailer.send_technical_alert(["主指標 値幅≥5%率 50% < 70%"], "2026-06-29 21:00")
    assert called == []  # 宛先未設定なら送らない


def test_sends_with_report_email(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL", "ops@example.com")
    captured = {}
    monkeypatch.setattr(mailer, "_send",
                        lambda to, subject, body: captured.update(to=to, subject=subject, body=body))
    fails = ["主指標 値幅≥5%率 50% < 70%", "ノイズ 空振り率 30% > 15%"]
    mailer.send_technical_alert(fails, "2026-06-29 21:00")
    assert captured["to"] == "ops@example.com"
    assert "成功基準ライン割れ（2件）" in captured["subject"]
    assert "主指標" in captured["body"] and "ノイズ" in captured["body"]
    assert "2026-06-29 21:00" in captured["body"]
