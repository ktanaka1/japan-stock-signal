"""取りこぼしの支配クラスから、現行ピックアップロジックの改善レバーを提案する。

本機能の第一義は「ピックアップロジックのブラッシュアップ」。捕捉率は手段であり、
出口は『次にどのレバーを動かすべきか』の提示。v1は決定的ルール（支配クラス→レバー）。
"""
from __future__ import annotations

from feedback.matcher import (
    STATUS_DELIVERED_TECH,
    STATUS_SIGNALED,
    STATUS_NEUTRAL,
    STATUS_NOT_COLLECTED,
)

# 取りこぼしクラス → 示唆・推奨レバー
_LEVERS = {
    STATUS_NOT_COLLECTED: (
        "収集網の外（ニュース無し）",
        "TDnet収集窓(limit)の拡張 / ランキング逆引きディスカバリ(未捕捉上位を翌朝候補化) / 情報源追加",
    ),
    STATUS_NEUTRAL: (
        "分析したが中立で落とした",
        "LLMの中立判定・プロンプト見直し（数値開示の方向確定を強化）",
    ),
    STATUS_SIGNALED: (
        "シグナル化したが未配信（impact閾値/大型株フィルタで除外）",
        "min_impact_for_notify / exclude_large_cap の実証チューニング",
    ),
}


def build_proposal(counts: dict, capture_rate) -> str:
    """status別件数(母集団)から、支配的な取りこぼしクラスと推奨レバーのサマリを作る。

    捕捉率はニュース版・テクニカル版の2チャンネル合算。内訳でテクニカル版の寄与を明示する。
    """
    misses = {k: counts.get(k, 0) for k in _LEVERS}
    total_miss = sum(misses.values())
    rate_s = f"{capture_rate * 100:.0f}%" if capture_rate is not None else "—"
    tech = counts.get(STATUS_DELIVERED_TECH, 0)
    tech_s = f"（うちテクニカル版 {tech}件）" if tech else ""
    if total_miss == 0:
        return f"捕捉率 {rate_s}{tech_s}。母集団の取りこぼしなし。"

    dominant = max(misses, key=lambda k: misses[k])
    label, lever = _LEVERS[dominant]
    breakdown = " / ".join(
        f"{_LEVERS[k][0]}:{misses[k]}" for k in _LEVERS if misses[k]
    )
    return (
        f"捕捉率 {rate_s}{tech_s}。取りこぼし内訳[{breakdown}]。"
        f"支配要因＝『{label}』({misses[dominant]}件) → 推奨レバー: {lever}"
    )
