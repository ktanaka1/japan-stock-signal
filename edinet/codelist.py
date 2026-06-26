"""EDINETコードリスト（edinetCode → 証券コード4桁/銘柄名）の取得・解決。

公式の Edinetcode.zip（キー不要・cp932・1行目メタ/2行目ヘッダ）をDLして対応表を作る。
証券コードは5桁(末尾0)で格納されているため [:4] で4桁化する。REIT/投資法人など証券コードが
空のものは4桁解決を None にする（呼び出し側でスキップ＝中小型株デイトレ対象外なので実害なし）。
名寄せ不要・authoritative。XBRLパースは一切しない。
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +edinet)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


def fetch_map() -> Dict[str, Tuple[Optional[str], str]]:
    """edinetCode -> (証券コード4桁 or None, 銘柄名) の辞書を返す。取得失敗時は空dict。"""
    try:
        r = httpx.get(_URL, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200 or r.content[:2] != b"PK":
            logger.warning("EDINET codelist: HTTP %s / not a zip", r.status_code)
            return {}
        z = zipfile.ZipFile(io.BytesIO(r.content))
        name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if not name:
            logger.warning("EDINET codelist: csv not found in zip")
            return {}
        lines = z.read(name).decode("cp932", errors="replace").splitlines()
    except (httpx.RequestError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        logger.warning("EDINET codelist fetch failed (%s)", exc)
        return {}
    return _parse(lines)


def _parse(lines: list) -> Dict[str, Tuple[Optional[str], str]]:
    """CSV行（1行目メタ・2行目ヘッダ）を edinetCode -> (sec4, name) に変換する。テスト可能に分離。"""
    if len(lines) < 2:
        return {}
    reader = csv.reader(lines[1:])  # 1行目はメタ情報なので捨てる
    try:
        header = next(reader)
    except StopIteration:
        return {}
    ei = next((i for i, h in enumerate(header) if "ＥＤＩＮＥＴコード" in h), None)
    sc = next((i for i, h in enumerate(header) if "証券コード" in h), None)
    nm = next((i for i, h in enumerate(header) if h == "提出者名"), None)
    if ei is None or sc is None:
        logger.warning("EDINET codelist: unexpected header %s", header)
        return {}
    out: Dict[str, Tuple[Optional[str], str]] = {}
    for row in reader:
        if len(row) <= max(ei, sc, nm or 0):
            continue
        code = row[ei].strip()
        if not code:
            continue
        raw = row[sc].strip()
        sec4 = raw[:4] if raw else None
        out[code] = (sec4, row[nm].strip() if nm is not None else "")
    return out
