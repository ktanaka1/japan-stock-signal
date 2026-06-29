"""上場企業名→証券コードの辞書（PR TIMES など証券コードを持たない情報源の名寄せ用）。

設計方針（WIP: prtimes-collector）:
  - 辞書データは `data/listed_companies.tsv`（JPX data_j.xls をオフラインで変換したもの）を
    リポジトリ同梱し、起動時に **標準ライブラリ csv だけ** でロードする。
    Excel パース用の重い依存（xlrd/openpyxl）はここには持ち込まない。
    （TSV の再生成は scripts/refresh_listed_companies.py を手元で実行する）
  - 突合は **保守的**（高信頼一致のみ採用）。表記ゆれの取りこぼし（recall低下）は許容し、
    誤コード付与（precision低下）を避ける。正規化キーが複数の上場会社に衝突する場合は
    その曖昧キーを辞書から除外する（誤同定の方が害が大きいため）。

TSV 形式（ヘッダ付き・タブ区切り）:
    code<TAB>name
    7419<TAB>ノジマ
"""
from __future__ import annotations

import csv
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TSV_PATH = Path(__file__).parent.parent / "data" / "listed_companies.tsv"

# 社名から除去する法人格・接尾辞。NFKC 正規化後（全角→半角済み）の文字列に対して適用する。
# 「ホールディングス」「ＨＤ(→HD)」等は別会社の識別に効かないノイズなので落として突合する。
_CORP_TOKENS = (
    "株式会社", "有限会社", "合同会社", "合資会社", "合名会社",
    "(株)", "（株）", "㈱", "(有)", "（有）", "㈲",
)
_SUFFIX_TOKENS = (
    "ホールディングス", "ホールディング",
    "holdings", "holding", "hd",
    "グループ", "group",
)
_SPACE_RE = re.compile(r"\s+")
# 社名の区切り記号。中黒(・)は JPX略称と PR TIMES 正式名で揺れる純粋なノイズなので除去する。
# 長音符(ー)やダッシュは語の一部になり得る（誤同定の元）ので**除去しない**（保守的突合）。
# "・" は NFKC で変化しないため明示的に対象にする。
_PUNCT_RE = re.compile(r"[・･]")

# モジュールキャッシュ（プロセス内で1回だけロード）
_dict_cache: Optional[dict[str, tuple[str, str]]] = None


def normalize_name(name: str) -> str:
    """社名を突合用キーに正規化する。

    手順: NFKC（全角→半角・全角空白→半角空白）→ 法人格/接尾辞の除去 → 空白除去 → 小文字化。
    返り値が空文字になる場合は突合不能（呼び出し側で破棄する）。
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).strip()
    # 法人格は位置に依らず除去（前置/後置の両方ありうる）
    for token in _CORP_TOKENS:
        s = s.replace(token, "")
    s = _PUNCT_RE.sub("", s)
    s = _SPACE_RE.sub("", s).lower()
    # 接尾辞（HD/ホールディングス等）は末尾のみ除去（社名中の一致を巻き込まない）
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIX_TOKENS:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                changed = True
    return s


def _load_dict(path: Path = _TSV_PATH) -> dict[str, tuple[str, str]]:
    """TSV を読み込み {正規化キー: (4桁コード, 正式社名)} を構築する。

    - 同一の正規化キーに複数の異なるコードが衝突した場合、その曖昧キーは除外する
      （誤同定を避けるため保守的に捨てる）。同一コードの重複は無害なので許容。
    """
    result: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    try:
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                code = (row.get("code") or "").strip()
                name = (row.get("name") or "").strip()
                if not code or not name:
                    continue
                key = normalize_name(name)
                if not key:
                    continue
                if key in result and result[key][0] != code:
                    ambiguous.add(key)
                    continue
                result.setdefault(key, (code, name))
    except FileNotFoundError:
        logger.warning("listed_companies.tsv not found at %s; lookups disabled", path)
        return {}
    except Exception:
        logger.warning("failed to load listed_companies.tsv; lookups disabled", exc_info=True)
        return {}

    for key in ambiguous:
        result.pop(key, None)
    logger.info("Loaded %d listed companies (%d ambiguous keys dropped)",
                len(result), len(ambiguous))
    return result


def _get_dict() -> dict[str, tuple[str, str]]:
    global _dict_cache
    if _dict_cache is None:
        _dict_cache = _load_dict()
    return _dict_cache


def lookup(company_name: str) -> Optional[tuple[str, str]]:
    """発表企業名を正規化して上場辞書に完全一致したら (4桁コード, 正式社名) を返す。

    一致しなければ None（＝非上場 or 表記ゆれ。呼び出し側は破棄する）。
    """
    key = normalize_name(company_name)
    if not key:
        return None
    return _get_dict().get(key)


def reset_cache() -> None:
    """テスト用: モジュールキャッシュをクリアする。"""
    global _dict_cache
    _dict_cache = None


def load_from(path: Path) -> None:
    """テスト用: 任意の TSV パスから辞書を読み込みキャッシュに載せる。"""
    global _dict_cache
    _dict_cache = _load_dict(path)
