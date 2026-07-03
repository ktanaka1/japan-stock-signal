"""【使い捨てプローブ】未捕捉急騰銘柄に「拾い逃した材料」が実在したかを測る。

背景: 捕捉率FBで `not_collected`（収集網の外）が支配要因。逆引きディスカバリ
（ランキング→材料を能動取得）を本実装する前に、"そもそも掘るべき鉱脈があるか"
を低コスト検証する。Yahoo個別銘柄ニュース(newsTopics.articles)を read-only で引き、
「銘柄固有の材料」と「市況の一覧に名前が出ただけのノイズ」を区別して実在率を出す。

read-only。DBは coverage_runs を読むだけ・書き込み一切なし。外部はYahooに礼儀正しく
間欠アクセス。本番/ローカルどちらのDBでも動く（対象銘柄の取得だけがDB依存）。

使い方:
    .venv/bin/python -m scripts.probe_hidden_materials              # 既定: 6/29の未捕捉
    .venv/bin/python -m scripts.probe_hidden_materials 2026-06-29 2026-06-26
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

import store.coverage_runs as coverage_runs

_NEWS_URL = "https://finance.yahoo.co.jp/quote/{code}.T/news"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +probe)"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
_SLEEP = 0.8  # Yahooへの礼儀（秒）

# newsTopics.articles の各要素（HTML中はクオートが \" にエスケープされている）。
_ARTICLE_RE = re.compile(
    r'\\"headline\\":\\"(?P<h>.*?)\\",\\"link\\":\\"(?P<l>.*?)\\"'
    r'(?:,\\"mediaName\\":\\"(?P<m>.*?)\\")?.*?\\"createTime\\":\\"(?P<t>.*?)\\"',
)

# 銘柄固有の材料を示すキーワード（カタリスト）。
_CATALYST_KW = [
    "上方修正", "下方修正", "業績予想", "決算", "増配", "復配", "記念配",
    "自己株", "自社株買", "立会外", "公開買付", "TOB", "買収", "子会社化",
    "資本業務提携", "業務提携", "提携", "受注", "契約", "採用", "供給",
    "新製品", "新薬", "承認", "治験", "特許", "譲渡益", "売却益", "特別利益",
    "増資", "第三者割当", "MSワラント", "新株予約権", "株式分割", "優待",
    "黒字", "最高益", "上場", "テーマ", "材料", "思惑", "観測", "報道",
]
# 市況の一覧記事（名前が載るだけ＝材料ではない）。これだけならノイズ扱い。
_MARKET_NOISE_RE = re.compile(
    r"(ランキング|動いた株|出来た株|値上がり率|値下がり率|"
    r"ストップ高／ストップ安|特別気配|株価注意報|寄り付き|前場|後場の|本日の|"
    r"日経平均|相場の|マーケット)"
)
# 個別銘柄に材料があったことを強く示す動意株/個別材料系の見出し型。
_SPECIFIC_RE = re.compile(r"(動意株|急伸|急騰|ストップ高).{0,30}(" + "|".join(_CATALYST_KW) + ")")


@dataclass
class Article:
    headline: str
    media: str
    time: str


@dataclass
class Probe:
    code: str
    name: str
    pct: Optional[float]
    n_news: int = 0
    articles: List[Article] = field(default_factory=list)
    material_headlines: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def has_material(self) -> bool:
        return bool(self.material_headlines)


def _unescape(s: str) -> str:
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s.replace("\\u3000", "　").replace('\\"', '"')


def _name_token(name: str) -> str:
    """『東京ボード工業(株)』→『東京ボード』など見出し照合用の短い社名。"""
    n = re.sub(r"\(株\)|（株）|株式会社|ホールディングス|HD|グループ", "", name)
    n = n.strip()
    return n[:4] if len(n) > 4 else n


def _is_material(headline: str, name_token: str) -> bool:
    """見出しが『この銘柄固有の材料』を示すか。市況一覧のノイズは弾く。"""
    h = headline
    # 動意株+カタリスト型は社名照合なしでも材料とみなす（個別解説記事）。
    if _SPECIFIC_RE.search(h):
        return True
    has_kw = any(k in h for k in _CATALYST_KW)
    if not has_kw:
        return False
    # カタリスト語があっても、市況一覧で社名に触れていないものはノイズ。
    if _MARKET_NOISE_RE.search(h) and (not name_token or name_token not in h):
        return False
    return True


def _fetch_news(code: str) -> tuple[List[Article], str]:
    url = _NEWS_URL.format(code=code)
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return [], f"HTTP {resp.status_code}"
    except httpx.RequestError as exc:
        return [], f"request error: {exc}"
    arts: List[Article] = []
    for m in _ARTICLE_RE.finditer(resp.text):
        arts.append(
            Article(
                headline=_unescape(m.group("h")),
                media=_unescape(m.group("m") or ""),
                time=m.group("t") or "",
            )
        )
    return arts, ""


def _targets(dates: List[str]) -> List[tuple]:
    """指定営業日の確定行から status=not_collected の (code, name, pct) を集める。"""
    rows = coverage_runs.get_recent(60)
    seen = set()
    out = []
    for d in dates:
        cand = [r for r in rows if r["ranking_date"] == d and r["finalized"] == 1]
        if not cand:
            cand = [r for r in rows if r["ranking_date"] == d]
        if not cand:
            print(f"  (warn) {d}: coverage_runs に行が無い")
            continue
        row = cand[0]
        for item in row["detail"]:
            if item.get("status") != "not_collected":
                continue
            key = (d, str(item.get("code")))
            if key in seen:
                continue
            seen.add(key)
            out.append((str(item.get("code")), item.get("name") or "", item.get("pct")))
    return out


def main() -> None:
    dates = sys.argv[1:] or ["2026-06-29"]
    print(f"=== 隠れ材料プローブ 対象日: {', '.join(dates)} ===")
    targets = _targets(dates)
    print(f"未捕捉(not_collected)銘柄: {len(targets)}件\n")

    probes: List[Probe] = []
    for i, (code, name, pct) in enumerate(targets, 1):
        arts, err = _fetch_news(code)
        p = Probe(code=code, name=name, pct=pct, n_news=len(arts), articles=arts, error=err)
        token = _name_token(name)
        for a in arts:
            if _is_material(a.headline, token):
                p.material_headlines.append(f"{a.time} {a.headline}  [{a.media}]")
        probes.append(p)
        flag = "★材料あり" if p.has_material else ("…なし" if not err else f"ERR {err}")
        pcts = f"{pct:+.1f}%" if isinstance(pct, (int, float)) else "?"
        print(f"[{i:2}/{len(targets)}] {code} {name[:14]:<14} {pcts:>7} news={p.n_news:<2} {flag}")
        time.sleep(_SLEEP)

    ok = [p for p in probes if p.has_material]
    fetched = [p for p in probes if not p.error]
    anynews = [p for p in fetched if p.n_news > 0]
    print("\n" + "=" * 60)
    print("【結果サマリ】")
    print(f"  対象          : {len(probes)}銘柄")
    print(f"  取得成功      : {len(fetched)}銘柄")
    print(f"  ニュース有り  : {len(anynews)}銘柄")
    if fetched:
        rate = len(ok) / len(fetched) * 100
        print(f"  ★材料実在率  : {len(ok)}/{len(fetched)} = {rate:.0f}%  (取得成功ベース)")
    print("\n【材料が実在した銘柄（＝逆引きで拾えたはずの取りこぼし）】")
    for p in ok:
        print(f"\n  ● {p.code} {p.name} ({p.pct:+.1f}%)" if isinstance(p.pct, (int, float))
              else f"\n  ● {p.code} {p.name}")
        for h in p.material_headlines[:4]:
            print(f"      - {h}")

    # レポートを一時ディレクトリに保存（証跡）
    out_path = os.path.join(tempfile.gettempdir(), "probe_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "dates": dates,
                "total": len(probes),
                "fetched": len(fetched),
                "with_material": len(ok),
                "rate_pct": round(len(ok) / len(fetched) * 100, 1) if fetched else None,
                "details": [
                    {
                        "code": p.code, "name": p.name, "pct": p.pct,
                        "n_news": p.n_news, "error": p.error,
                        "material": p.material_headlines,
                        "all_headlines": [f"{a.time} {a.headline}" for a in p.articles[:12]],
                    }
                    for p in probes
                ],
            },
            f, ensure_ascii=False, indent=2,
        )
    print(f"\n(詳細JSON: {out_path})")


if __name__ == "__main__":
    main()
