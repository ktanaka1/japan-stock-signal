"""大型株（TOPIX Core30 相当）の判定（柱1: 大型株ノイズの除外）。

超大型株は材料が良くてもデイトレサイズでは動かず、インパクトスコアでも低評価に
なりやすいが、保険として配信から除外できるようにする。marketCap API が使えないため
手動リストで近似する（floor 運用・完全網羅ではない）。検証スクリプトと共用する。
"""
from __future__ import annotations

# TOPIX Core30 相当の超大型株コード。
LARGE_CAP_CODES = frozenset({
    "2914", "3382", "4063", "4502", "4503", "4519", "4543", "4568", "4661", "6098",
    "6146", "6273", "6367", "6501", "6503", "6594", "6702", "6752", "6758", "6861",
    "6902", "6954", "6981", "7011", "7203", "7267", "7741", "7751", "7974", "8001",
    "8031", "8035", "8053", "8058", "8306", "8316", "8411", "8591", "8604", "8725",
    "8766", "8801", "9020", "9022", "9432", "9433", "9434", "9983", "9984",
})


def is_large_cap_code(code: str) -> bool:
    return str(code or "").strip() in LARGE_CAP_CODES


def all_large_cap(stocks: list) -> bool:
    """シグナルの全銘柄が大型株か（1つでも中小型を含めば False）。空は False。"""
    codes = [str(s.get("code", "")).strip() for s in stocks if s.get("code")]
    return bool(codes) and all(c in LARGE_CAP_CODES for c in codes)
