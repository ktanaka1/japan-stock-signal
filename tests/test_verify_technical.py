"""テクニカル精度計測の判定ヘルパー（旬ヒット/空振り/売買代金バケット）の検証。"""
from scripts.verify_technical import _is_hit, _is_noise, _turnover_bucket


def test_hit_requires_range_and_spike():
    assert _is_hit({"range": 6.0, "spike_prev": 2.0}) is True
    assert _is_hit({"range": 6.0, "spike_prev": 1.0}) is False   # 出来高不足
    assert _is_hit({"range": 4.0, "spike_prev": 3.0}) is False   # 値幅不足


def test_noise_requires_both_low():
    assert _is_noise({"range": 2.0, "spike_prev": 1.0}) is True
    assert _is_noise({"range": 8.0, "spike_prev": 1.0}) is False  # 値幅は出た
    assert _is_noise({"range": 2.0, "spike_prev": 3.0}) is False  # 出来高は跳ねた


def test_noise_handles_missing_metrics():
    # range/spike が None でも 0 扱いで空振り判定（落ちない）
    assert _is_noise({"range": None, "spike_prev": None}) is True


def test_turnover_bucket():
    assert _turnover_bucket(None) == "不明"
    assert _turnover_bucket(2.5e8) == "<3億"
    assert _turnover_bucket(5e8) == "3〜10億"
    assert _turnover_bucket(20e8) == "≥10億"
