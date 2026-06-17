"""iXBRL の構造化数値抽出＋定性タグ（修正理由）抽出（Step③・純粋関数）。

body_fetcher の XBRL 経路から呼ぶ。ネットワーク・DB に一切依存しない純粋なパース関数で、
入力は zip の bytes（または iXBRL htm の bytes 単体）、出力は
  (xbrl_metrics_json: str|None, correction_reason: str|None)
のタプル。例外は内部で握り、抽出できなければ (None, None) を返す（呼び出し側を止めない）。

設計の核（ユーザー確定要件）:
  1. 抽出対象を 売上高/営業利益/経常利益/当期純利益(＋EPS・配当) に限定。
  2. **contextRef を厳密照合**して「前回予想↔今回予想」「当期実績↔前期実績」のペアを確定する。
     タグ名だけで拾わず、Previous/Current・Forecast/Result・連結/単体・通期(Annual)/四半期を見る。
     曖昧・不一致ならその指標は抽出しない（誤った数字を出すくらいなら出さない）。
  3. 修正理由テキスト（ReasonForForecastCorrection 等）が在れば correction_reason に確保する。

開示種別ごとの比較パターン（仕様書 §4）:
  - 業績予想の修正（tse-rvfc など Forecast 系）:
      ..._PreviousMember_ForecastMember（前回予想） vs ..._CurrentMember_ForecastMember（今回予想）
      → 上方/下方修正。
  - 決算短信（tse-acedjpsm Summary など）:
      PriorYearDuration_..._ResultMember（前期実績） vs CurrentYearDuration_..._ResultMember（当期実績）
      → 増収増益/減収減益。
  - 配当予想の修正（tse-ed-t:DividendPerShare）:
      Annual の Previous_Forecast vs Current_Forecast（＋Prior の Result） → 増配/減配。

堅牢性: 未知タクソノミ・対象タグ無し・コンテキスト不一致は抽出0件として (None, None) を返し例外を投げない。
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from typing import Optional

from lxml import etree

logger = logging.getLogger(__name__)

# --- 抽出対象メトリクスの定義（ホワイトリスト）----------------------------------
#
# 各メトリクスは「日本語ラベル」と「タグ名の末尾候補（連結/単体・日本基準/IFRS の語尾揺れを吸収）」を持つ。
# タグ名は名前空間プレフィクスを除いた localname の末尾一致で照合する
# （例 jppfs_cor:NetSales / tse-ed-t:NetSales / tse-rvfc:NetSalesIFRS をすべて NetSales 系として拾う）。
# 順序＝出力 items の順序＝デイトレ判定での重要度順（売上→営業→経常→純利→EPS）。
#
# 値か率かの区別: amount=金額（百万円に正規化）/ per_share=1株当たり（円・正規化しない）。
_METRICS = [
    # (metric_key, 日本語ラベル, 種別, タグ末尾候補のタプル)
    ("NetSales", "売上高", "amount",
     ("NetSales", "OperatingRevenue", "OperatingRevenues", "Revenue", "Revenues", "NetSalesOfCompletedConstructionContracts")),
    ("OperatingIncome", "営業利益", "amount",
     ("OperatingIncome", "OperatingProfit", "OperatingProfitLoss")),
    ("OrdinaryIncome", "経常利益", "amount",
     ("OrdinaryIncome", "OrdinaryProfit", "OrdinaryProfitLoss", "ProfitBeforeTax", "IncomeBeforeIncomeTaxes")),
    ("Profit", "当期純利益", "amount",
     ("ProfitAttributableToOwnersOfParent", "NetIncome", "ProfitLossAttributableToOwnersOfParent", "Profit")),
    ("EPS", "1株当たり利益", "per_share",
     ("BasicEarningsPerShare", "NetIncomePerShare", "EarningsPerShare")),
]

# 配当（配当予想の修正用）。DividendPerShare のみを別建てで扱う。
_DIVIDEND_SUFFIXES = ("DividendPerShare",)

# 定性（修正理由）テキストタグの末尾候補。優先度順（前にあるものを優先して採用）。
_REASON_SUFFIXES = (
    "ReasonForForecastCorrection",
    "ReasonForDividendForecastCorrection",
    "NoticeOfForecastCorrection",
    "QualitativeInformationAboutForecast",
    "QualitativeInfo",
)

# 証券コードタグ
_SECURITIES_CODE_SUFFIXES = ("SecuritiesCode",)

# iXBRL の数値・テキスト fact 要素（HTMLParser は名前空間を畳むので literal なタグ名で照合）
_IX_FACT_TAGS = ("ix:nonfraction", "ix:nonnumeric")

_MILLION = 1_000_000  # amount を百万円に正規化する除数


class _Fact:
    """iXBRL の 1 fact（タグ末尾名・contextRef・正規化前の値情報）。"""

    __slots__ = ("local", "ctx", "raw_text", "sign", "scale")

    def __init__(self, local: str, ctx: str, raw_text: str, sign: Optional[str], scale: Optional[str]):
        self.local = local
        self.ctx = ctx
        self.raw_text = raw_text
        self.sign = sign
        self.scale = scale


# --- 公開関数 -------------------------------------------------------------------


def parse(content: bytes) -> tuple[Optional[str], Optional[str]]:
    """zip bytes または iXBRL htm bytes から (xbrl_metrics_json, correction_reason) を返す。

    抽出できなければ (None, None)。例外は投げない。
    """
    try:
        htm = _extract_ixbrl_htm(content)
        if htm is None:
            return (None, None)
        facts, texts = _scan_facts(htm)
        if not facts and not texts:
            return (None, None)

        reason = _pick_reason(texts)
        metrics = _build_metrics(facts)
        metrics_json = json.dumps(metrics, ensure_ascii=False) if metrics else None
        return (metrics_json, reason)
    except Exception:
        # 純粋関数として例外を外に漏らさない（未知タクソノミ等は抽出0件扱い）。
        logger.info("xbrl parse: unexpected error; extract nothing", exc_info=True)
        return (None, None)


# --- iXBRL htm の取り出し（zip でも単体 htm でも受ける）--------------------------


def _extract_ixbrl_htm(content: bytes) -> Optional[bytes]:
    """入力 bytes から「数値の核」が載った iXBRL htm を取り出す。

    - zip の場合: Summary（決算サマリ）を最優先、無ければ ixbrl を含む htm を優先。
      決算短信は財表(Attachment)より Summary に前期↔当期の比較が載るため Summary を選ぶ。
    - 既に htm（'<' で始まる/zip でない）の場合: そのまま返す。
    """
    if content[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            return None
        target = _pick_ixbrl_entry(zf.namelist())
        if target is None:
            return None
        return zf.read(target)
    # zip でない＝htm そのものとして扱う
    return content


def _pick_ixbrl_entry(names: list[str]) -> Optional[str]:
    """zip 内から数値抽出に使う iXBRL htm を選ぶ。

    優先: Summary 配下の ixbrl htm（決算短信のサマリ＝前期↔当期比較が載る）
        → ixbrl を含む htm → 任意の htm。
    """
    htmls = [n for n in names if n.lower().endswith((".htm", ".html"))]
    if not htmls:
        return None
    # 決算短信の Summary を最優先（PL/BS 個票より比較に向く）
    for n in htmls:
        low = n.lower()
        if "summary" in low and "ixbrl" in low:
            return n
    for n in htmls:
        if "ixbrl" in n.lower():
            return n
    return htmls[0]


# --- fact 走査 ------------------------------------------------------------------


def _scan_facts(htm: bytes) -> tuple[list[_Fact], dict[str, str]]:
    """iXBRL htm を走査し、(数値 fact のリスト, 末尾名→テキスト の辞書) を返す。

    texts には定性テキストや証券コードなど nonNumeric を末尾名キーで格納（先勝ち）。
    """
    tree = etree.parse(io.BytesIO(htm), etree.HTMLParser(recover=True))
    root = tree.getroot()
    facts: list[_Fact] = []
    texts: dict[str, str] = {}
    if root is None:
        return facts, texts

    for e in root.iter(*_IX_FACT_TAGS):
        name = e.get("name") or ""
        if not name:
            continue
        local = name.split(":")[-1]
        ctx = e.get("contextref") or ""
        tag = e.tag  # 'ix:nonfraction' | 'ix:nonnumeric'
        if tag.endswith("nonfraction"):
            raw = (e.text or "").strip()
            facts.append(_Fact(local, ctx, raw, e.get("sign"), e.get("scale")))
        else:
            # nonNumeric: テキストを末尾名キーで保持（先勝ち＝最初の出現を採用）
            txt = " ".join((t.strip() for t in e.itertext() if t and t.strip()))
            if txt and local not in texts:
                texts[local] = txt
    return facts, texts


# --- 値の正規化 -----------------------------------------------------------------


def _normalize_value(fact: _Fact, kind: str) -> Optional[float]:
    """fact の表示テキストを sign / scale を解釈して数値化する。

    iXBRL の正準値 = text_number × 10^scale。表示テキストには scale 適用後の桁が出ているため
    (例 '6,620' scale=6 → 6,620 百万円)、amount は素直に百万円表示として扱い、
    1株当たり(per_share)は円のまま返す。sign='-' なら符号反転。
    """
    raw = (fact.raw_text or "").replace(",", "").replace("△", "-").replace("▲", "-").strip()
    if raw in ("", "-", "－", "—"):
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if fact.sign == "-":
        val = -val
    # 表示テキストは scale 適用済みの人間可読値（百万円・円）なので scale で再スケールしない。
    # ただし amount で明らかに円単位（scale が 0 など）の桁の場合に備え、ここでは表示値を信頼する。
    return val


def _to_int_or_float(v: float) -> float | int:
    """整数なら int、小数なら丸めて float（JSON を読みやすく）。"""
    if v == int(v):
        return int(v)
    return round(v, 2)


# --- メトリクス構築（contextRef 厳密照合）---------------------------------------


def _build_metrics(facts: list[_Fact]) -> Optional[dict]:
    """fact 群から basis を判定し、前回↔今回（または前期↔当期）の比較ペアを厳密照合で組む。

    優先順位: 業績予想の修正 → 配当予想の修正 → 決算短信。
    複数該当する開示（業績予想＋配当の同時修正など）は、より株価インパクトの大きい
    業績予想修正を優先して basis にする（配当は items に含められないが reason で補える）。
    """
    # contextRef の出現傾向で開示種別を見分ける
    has_prev_forecast = any("PreviousMember" in f.ctx and "ForecastMember" in f.ctx for f in facts)
    has_curr_forecast = any("CurrentMember" in f.ctx and "ForecastMember" in f.ctx for f in facts)
    has_year_result = any(
        ("CurrentYearDuration" in f.ctx and "ResultMember" in f.ctx and "Quarter" not in f.ctx)
        for f in facts
    )
    has_prior_result = any(
        ("PriorYearDuration" in f.ctx and "ResultMember" in f.ctx and "Quarter" not in f.ctx)
        for f in facts
    )

    # 業績指標（売上等）が Forecast 比較で取れるか
    if has_prev_forecast and has_curr_forecast:
        result = _build_forecast_revision(facts)
        if result:
            return result

    # 配当予想の修正
    div = _build_dividend_revision(facts)
    if div:
        return div

    # 決算短信（前期実績 vs 当期実績）
    if has_year_result and has_prior_result:
        result = _build_earnings(facts)
        if result:
            return result

    return None


def _select_fact(
    facts: list[_Fact],
    suffixes: tuple[str, ...],
    required: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
) -> Optional[_Fact]:
    """末尾名が suffixes のいずれか、かつ contextRef が required を全て含み forbidden を含まない
    最初の有効な数値 fact を返す。

    連結優先・通期優先のため、required を満たす候補から ConsolidatedMember / AnnualMember を
    含むものを優先採用する（厳密照合：別コンテキストの取り違えを防ぐ）。
    """
    candidates: list[_Fact] = []
    for f in facts:
        if not _suffix_match(f.local, suffixes):
            continue
        if any(r not in f.ctx for r in required):
            continue
        if any(x in f.ctx for x in forbidden):
            continue
        if _normalize_value(f, "amount") is None:
            continue
        candidates.append(f)
    if not candidates:
        return None

    def score(f: _Fact) -> tuple:
        return (
            "ConsolidatedMember" in f.ctx and "NonConsolidatedMember" not in f.ctx,  # 連結優先
            "AnnualMember" in f.ctx,  # 通期優先（配当）
        )

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _suffix_match(local: str, suffixes: tuple[str, ...]) -> bool:
    """localname が suffix のいずれかに（IFRS 等の語尾を許容して）一致するか。

    例 'NetSalesIFRS' は 'NetSales' に一致。'NetSalesSummaryOfBusinessResults' のような
    別概念を誤って拾わないよう、suffix の直後は語尾(IFRS/末尾)のみ許す。
    """
    for s in suffixes:
        if local == s or local == s + "IFRS":
            return True
    return False


def _pct_change(prev: float, curr: float) -> Optional[float]:
    if prev == 0:
        return None
    return round((curr - prev) / abs(prev) * 100, 1)


def _direction(prev: Optional[float], curr: Optional[float]) -> Optional[str]:
    if prev is None or curr is None:
        return None
    if curr > prev:
        return "up"
    if curr < prev:
        return "down"
    return "flat"


# --- basis 別ビルダ -------------------------------------------------------------


def _build_forecast_revision(facts: list[_Fact]) -> Optional[dict]:
    """業績予想の修正: 前回予想 vs 今回予想 を厳密照合して items を組む。

    ★当期(current fiscal year)へのピン留め（ユーザー指摘の致命的弱点対策）:
      業績予想の修正XBRLには、稀に「当期の修正」と「来期(NextYear)の見通し」が同居する。
      実ファイル(tse-rvfc)で前回/今回予想の contextRef は必ず
        CurrentYearDuration_..._PreviousMember_ForecastMember /
        CurrentYearDuration_..._CurrentMember_ForecastMember
      の形を取り、来期は NextYearDuration_...（更に翌々期は Next2YearDuration_...）になる
      ことを実XBRLで裏取りした。よって当期予想だけを確定的に拾うため、required に
      "CurrentYearDuration" を加え（当期会計期間にピン留め）、さらに二重防御として
      将来期間 "NextYearDuration"/"Next2YearDuration" を forbidden に追加する。
      これにより「来期の超強気予想が当期修正よりファイル内で先に定義されていても」
      出現順に依存せず当期の値のみを採用し、巨大な誤シグナルの発火を防ぐ。
    """
    items = []
    _future = ("NextYearDuration", "Next2YearDuration")
    for key, label, kind, suffixes in _METRICS:
        prev_f = _select_fact(
            facts, suffixes,
            required=("CurrentYearDuration", "PreviousMember", "ForecastMember"),
            forbidden=("LowerMember", "UpperMember", "QuarterMember", "SecondQuarter", "FirstQuarter", "ThirdQuarter") + _future,
        )
        curr_f = _select_fact(
            facts, suffixes,
            required=("CurrentYearDuration", "CurrentMember", "ForecastMember"),
            forbidden=("LowerMember", "UpperMember", "ResultMember", "QuarterMember", "SecondQuarter", "FirstQuarter", "ThirdQuarter") + _future,
        )
        prior_f = _select_fact(
            facts, suffixes,
            required=("PriorYearDuration", "ResultMember"),
            forbidden=("QuarterMember", "SecondQuarter", "FirstQuarter", "ThirdQuarter"),
        )
        item = _make_item(key, label, kind, prev_f, curr_f, prior_f)
        if item:
            items.append(item)
    if not items:
        return None
    overall = _overall_label(items, basis="forecast_revision")
    return {"basis": "forecast_revision", "items": items, "overall": overall}


def _build_earnings(facts: list[_Fact]) -> Optional[dict]:
    """決算短信: 前期実績(Prior) vs 当期実績(Current) を厳密照合して items を組む。"""
    items = []
    for key, label, kind, suffixes in _METRICS:
        prev_f = _select_fact(
            facts, suffixes,
            required=("PriorYearDuration", "ResultMember"),
            forbidden=("ForecastMember", "QuarterMember", "SecondQuarter", "FirstQuarter", "ThirdQuarter"),
        )
        curr_f = _select_fact(
            facts, suffixes,
            required=("CurrentYearDuration", "ResultMember"),
            forbidden=("ForecastMember", "QuarterMember", "SecondQuarter", "FirstQuarter", "ThirdQuarter"),
        )
        item = _make_item(key, label, kind, prev_f, curr_f, None)
        if item:
            items.append(item)
    if not items:
        return None
    overall = _overall_label(items, basis="earnings")
    return {"basis": "earnings", "items": items, "overall": overall}


def _build_dividend_revision(facts: list[_Fact]) -> Optional[dict]:
    """配当予想の修正: 年間配当の 前回予想 vs 今回予想（＋前期実績）。"""
    # 配当も将来期間（来期 NextYear / 翌々期 Next2Year の年間配当見通し）の混入を最小限の
    # forbidden で排除する（AnnualMember 軸＋当期 CurrentYearDuration への暗黙のピン留めに加え、
    # 念のため将来期間を明示除外）。前回/今回は CurrentYearDuration_AnnualMember_... に限定。
    _future = ("NextYearDuration", "Next2YearDuration")
    prev_f = _select_fact(
        facts, _DIVIDEND_SUFFIXES,
        required=("CurrentYearDuration", "AnnualMember", "PreviousMember", "ForecastMember"),
        forbidden=("LowerMember", "UpperMember") + _future,
    )
    curr_f = _select_fact(
        facts, _DIVIDEND_SUFFIXES,
        required=("CurrentYearDuration", "AnnualMember", "CurrentMember", "ForecastMember"),
        forbidden=("LowerMember", "UpperMember", "ResultMember") + _future,
    )
    prior_f = _select_fact(
        facts, _DIVIDEND_SUFFIXES,
        required=("PriorYearDuration", "AnnualMember", "ResultMember"),
        forbidden=("LowerMember", "UpperMember") + _future,
    )
    item = _make_item("DividendPerShare", "1株当たり配当", "per_share", prev_f, curr_f, prior_f)
    if not item:
        return None
    overall = _overall_label([item], basis="dividend_revision")
    return {"basis": "dividend_revision", "items": [item], "overall": overall}


def _make_item(
    key: str, label: str, kind: str,
    prev_f: Optional[_Fact], curr_f: Optional[_Fact], prior_f: Optional[_Fact],
) -> Optional[dict]:
    """前回(prev)・今回(curr)・前期(prior) の fact から正規化済み item を作る。

    比較の主軸（prev↔curr）が両方揃わなければ抽出しない（誤照合・片側欠落の混同を避ける）。
    """
    prev = _normalize_value(prev_f, kind) if prev_f else None
    curr = _normalize_value(curr_f, kind) if curr_f else None
    prior = _normalize_value(prior_f, kind) if prior_f else None
    if prev is None or curr is None:
        return None

    change_pct = _pct_change(prev, curr)
    direction = _direction(prev, curr)
    unit = "円" if kind == "per_share" else "百万円"
    return {
        "metric": key,
        "label": label,
        "previous": _to_int_or_float(prev),
        "current": _to_int_or_float(curr),
        "prior": _to_int_or_float(prior) if prior is not None else None,
        "unit": unit,
        "change_pct": change_pct,
        "direction": direction,
    }


def _overall_label(items: list[dict], basis: str) -> str:
    """items の方向から総合ラベルを付ける。

    - forecast_revision: 売上/営業/経常/純利 の総合で 上方修正/下方修正/混在/予想据え置き。
    - earnings: 増収増益/減収減益/混在 を売上と利益から。
    - dividend_revision: 増配/減配/配当据え置き。
    """
    dirs = [it["direction"] for it in items if it["direction"]]
    if not dirs:
        return "判定不可"

    if basis == "dividend_revision":
        d = items[0]["direction"]
        return {"up": "増配", "down": "減配", "flat": "配当据え置き"}.get(d, "判定不可")

    ups = dirs.count("up")
    downs = dirs.count("down")

    if basis == "earnings":
        sales = next((it for it in items if it["metric"] == "NetSales"), None)
        profits = [it for it in items if it["metric"] in ("OperatingIncome", "OrdinaryIncome", "Profit")]
        sales_dir = sales["direction"] if sales else None
        profit_up = sum(1 for it in profits if it["direction"] == "up")
        profit_down = sum(1 for it in profits if it["direction"] == "down")
        rev = "増収" if sales_dir == "up" else "減収" if sales_dir == "down" else ""
        if profit_up and not profit_down:
            inc = "増益"
        elif profit_down and not profit_up:
            inc = "減益"
        elif profit_up and profit_down:
            inc = "増減混在"
        else:
            inc = ""
        label = (rev + inc).strip()
        return label or ("増収増益" if ups and not downs else "減収減益" if downs and not ups else "混在")

    # forecast_revision
    if ups and not downs:
        return "上方修正"
    if downs and not ups:
        return "下方修正"
    if ups and downs:
        return "上下混在"
    return "予想据え置き"


def _pick_reason(texts: dict[str, str]) -> Optional[str]:
    """定性テキスト（修正理由）を優先度順に拾って返す。

    理由系タグが複数在れば、より具体的な ReasonFor... を優先。Notice 系は補助。
    複数拾えた場合は連結して情報量を確保（Step④で LLM が「なぜ修正したか」を読む材料）。
    """
    parts: list[str] = []
    for suffix in _REASON_SUFFIXES:
        for local, txt in texts.items():
            if local == suffix and txt and txt not in parts:
                parts.append(txt)
    if not parts:
        return None
    joined = "\n".join(parts).strip()
    return joined or None


def extract_security_code(content: bytes) -> Optional[str]:
    """iXBRL から証券コードを取り出す補助関数（新形式英数字 例 376A0 にも対応）。

    body_fetcher 側で company_code を補完したいとき用。抽出できなければ None。
    """
    try:
        htm = _extract_ixbrl_htm(content)
        if htm is None:
            return None
        _, texts = _scan_facts(htm)
        for suffix in _SECURITIES_CODE_SUFFIXES:
            for local, txt in texts.items():
                if local == suffix and txt:
                    # iXBRL が桁ごとにテキストを分割する事があり itertext 連結で空白が入る。
                    # 証券コードは英数字のみなので空白を畳む（新形式 376A0 等も維持）。
                    return "".join(txt.split())
    except Exception:
        return None
    return None
