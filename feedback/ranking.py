"""Yahoo Finance JP の銘柄ランキング取得・パース。

ランキングページに埋め込まれた `__PRELOADED_STATE__`(JSON) を httpx で取得して読む。
素のHTMLに構造化JSONが入っているためJSレンダリング不要（headless不要）。
HTMLではなくJSONを読むので比較的安定だが、Yahoo側の構造変更で壊れうる。
パースは _parse_state に切り出し（テスト可能）、取得失敗・0件は呼び出し側で障害検知する。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import httpx

from monitor.large_cap import is_large_cap_code

logger = logging.getLogger(__name__)

_RANKING_URL = "https://finance.yahoo.co.jp/stocks/ranking/{kind}?market=all&term=daily"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +feedback)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
_STATE_RE = re.compile(r"__PRELOADED_STATE__\s*=\s*(\{.*\})", re.S)

# marketName にこれを含む銘柄は対象外（ETF・ETN・指数連動）。
_NON_STOCK_MARKETS = ("ETF", "ETN")


@dataclass
class RankItem:
    rank: int
    code: str          # 4桁(新形式英数あり)の証券コード
    name: str
    market: str        # 東証PRM/STD/GRT/ETF 等
    change_pct: Optional[float]
    is_etf: bool
    is_large_cap: bool

    @property
    def in_universe(self) -> bool:
        """捕捉率の母集団（中小型・非ETF）に含まれるか。"""
        return not self.is_etf and not self.is_large_cap


def fetch_ranking(kind: str = "up", top_n: int = 30) -> List[RankItem]:
    """値上がり率(up)/出来高(volume)ランキングの上位 top_n を取得する。

    取得・パース失敗時は空リストを返す（呼び出し側で障害として扱う）。
    """
    url = _RANKING_URL.format(kind=kind)
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            logger.warning("ranking %s: HTTP %s", kind, resp.status_code)
            return []
        results = _parse_state(resp.text)
    except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
        logger.warning("ranking %s: fetch/parse failed (%s)", kind, exc)
        return []
    items = [_to_item(r) for r in results]
    items = [it for it in items if it is not None]
    return items[:top_n]


def _parse_state(html: str) -> list:
    """HTMLから __PRELOADED_STATE__ を抽出し mainRankingList.results を返す。"""
    m = _STATE_RE.search(html)
    if not m:
        raise ValueError("no __PRELOADED_STATE__")
    data = json.loads(m.group(1))
    return (data.get("mainRankingList") or {}).get("results") or []


def _to_item(r: dict) -> Optional[RankItem]:
    code = str(r.get("stockCode") or "").strip()
    if not code:
        return None
    market = str(r.get("marketName") or "").strip()
    rr = r.get("rankingResult") or {}
    # up は changePriceRate、volume は volume 側に changePriceRate が入る
    block = rr.get("changePriceRate") or rr.get("volume") or {}
    pct = _to_float(block.get("changePriceRate"))
    try:
        rank = int(str(r.get("rank") or "0").replace(",", ""))
    except ValueError:
        rank = 0
    return RankItem(
        rank=rank,
        code=code,
        name=str(r.get("stockName") or "").strip(),
        market=market,
        change_pct=pct,
        is_etf=any(tag in market for tag in _NON_STOCK_MARKETS),
        is_large_cap=is_large_cap_code(code),
    )


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return None
