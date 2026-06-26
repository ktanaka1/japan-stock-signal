"""テクニカル精度計測の判定ヘルパー（旬ヒット/空振り/売買代金バケット）の検証。

実行: FORCE_LOCAL_DB=1 DB_PATH=/tmp/t.db .venv/bin/python -m pytest tests/test_verify_technical.py
（_verdict は coverage_runs を読むため空のローカルDBで動かす）
"""
from scripts.verify_technical import (
    _is_hit, _is_noise, _turnover_bucket, _verdict, SC_MIN_N,
)


def _ev(rng, spike, source="up"):
    return {"m": {"range": rng, "spike_prev": spike, "split": False}, "source": source}


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


def test_verdict_all_pass_returns_empty():
    # 値幅10%（≥5%）・出来高1.0 → 値幅ヒット100%・空振り0%。捕捉率は空DBで判定保留。
    evs = [_ev(10.0, 1.0) for _ in range(SC_MIN_N)]
    assert _verdict(evs) == []


def test_verdict_flags_low_range_and_noise():
    # 値幅2%・出来高1.0 → 値幅ヒット0%(主指標FAIL) かつ 空振り100%(ノイズFAIL)
    evs = [_ev(2.0, 1.0) for _ in range(SC_MIN_N)]
    fails = _verdict(evs)
    assert len(fails) == 2
    assert any("主指標" in f for f in fails) and any("ノイズ" in f for f in fails)


def test_verdict_small_sample_holds():
    # SC_MIN_N 未満なら基準割れでも判定保留（誤アラート抑止）→ 空リスト
    evs = [_ev(2.0, 1.0) for _ in range(SC_MIN_N - 1)]
    assert _verdict(evs) == []


def test_verdict_vol_source_gap_flagged():
    # up源は良好(値幅10%×10)、vol源は劣化(値幅0%×10) → up比-100ptで出来高源FAIL
    evs = [_ev(10.0, 1.0, "up") for _ in range(SC_MIN_N)] + \
          [_ev(2.0, 1.0, "vol") for _ in range(SC_MIN_N)]
    fails = _verdict(evs)
    assert any("出来高源" in f for f in fails)
