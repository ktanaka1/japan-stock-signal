"""
精査役エージェント - LLM判定モジュール
ニュース記事の株価センチメント分析と関連銘柄の特定を行う。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

# DRY_RUN・フォールバック用のコード直書きプロンプト。
# 本番では settings の gemini_system_prompt（DEFAULTは store/settings.py で同内容に厳格化）が優先される。
# settings 取得不能時やテスト時にこの定数が効く。
_SYSTEM_PROMPT = (
    "あなたは日本株デイトレーダーを支援するアナリストです。\n"
    "ニュース記事を読み、以下のフィールドを必ず埋めてください：\n"
    "sentiment: positive（株価上昇要因）/ negative（株価下落要因）/ neutral（株価影響なし）\n"
    "summary: 日本語で1行、見出しのように簡潔な要約\n"
    "reason: sentiment をそう判断した根拠を日本語で2〜3文で説明する。"
    "中立と判断した場合はその理由も記載する。\n"
    "stocks: 関連する日本株銘柄のリスト（証券コード4桁と銘柄名）。"
    "銘柄が特定できない場合は空リストにする。\n"
    "\n"
    "【数値が与えられた開示の判定ルール（厳守）】\n"
    "業績予想の修正・決算・配当の開示で、数値（XBRL確定値 or 本文）が与えられている場合、"
    "安易に neutral にしてはならない。\n"
    "今回値 > 前回予想（上方修正・増配・増益）= positive、"
    "今回値 < 前回予想（下方修正・減配・減益）= negative。\n"
    "数値や機械方向ラベルがある場合はそれを根拠に方向を確定し、"
    "reason に前回予想比/前期比を必ず明記せよ。\n"
    "neutral は『株価に影響しない』と確信できる時のみ。"
    "数値があるのに情報不足を理由に neutral にしてはならない。\n"
    "\n"
    "【最重要・絶対厳守】\n"
    "渡されたデータが『XBRL抽出数値（確定値）』である場合、それは100%正確な事実である。"
    "その数値の比較結果（上昇/下落/据え置き）のみを絶対の根拠として方向を判定せよ。"
    "XBRL数値が与えられているときに、テキスト（修正理由など）から別の要因を推測して"
    "判定を覆してはならない。\n"
    "本文テキスト（PDF抽出）のみが与えられた場合は、本文中の前回予想と今回予想"
    "（または前期実績）を見つけ、数値を比較して方向を判定せよ。"
)

_BATCH_SYSTEM_PROMPT = (
    _SYSTEM_PROMPT
    + "\n複数の記事が番号付きで与えられます。"
    "記事ごとに1つの結果を返し、index フィールドに記事番号をそのまま入れてください。"
)

# 本文（PDF/XBRLテキスト）をLLMに渡す際の文字数上限の既定値（settingsで上書き可能）。
_BODY_MAX_CHARS = 4000

# 本文付き記事はトークン消費が大きいため、本文付きを含むバッチは小さくする（仕様書§8）。
# 本文なし（title_only/error）バッチの既定サイズ。
_BATCH_SIZE_DEFAULT = 10
# 本文付き（xbrl_metrics / full_body あり）バッチの縮小サイズ。
_BATCH_SIZE_WITH_BODY = 3

# 使用するGeminiモデルの既定値（実際の値はsettingsで上書き可能 / 管理画面から変更）
# 無料枠が広く実績のある 2.5-flash を既定にする。上位へは管理画面で切替可能。
_MODEL = "gemini-2.5-flash"

# Geminiのレート制限対策にバッチ間で待機する（実際の値はsettingsで上書き可能）
_BATCH_INTERVAL_SECONDS = 7

# 連続でバッチが失敗したら打ち切る（日次クォータ枯渇時にリトライ待機で
# ジョブのタイムアウトまで時間を浪費しないため。残りは未読のまま次回に回る）
_MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class AnalysisResult:
    sentiment: str  # "positive" | "negative" | "neutral"
    summary: str
    reason: str
    stocks: List[dict]  # [{"name": "日本製鉄", "code": "5401"}]


# basis（xbrl_metrics）→ 人間可読の開示種別ラベル
_BASIS_LABELS = {
    "forecast_revision": "業績予想の修正",
    "earnings": "決算",
    "dividend_revision": "配当予想の修正",
}

# direction → 日本語
_DIRECTION_LABELS = {"up": "up（増加）", "down": "down（減少）", "flat": "flat（据え置き）"}


def _has_body(article: object) -> bool:
    """LLMに渡せる本文（XBRL数値 or PDF/XBRLテキスト）を持つ記事か。

    body_status が 'xbrl'/'pdf_text' で、実際に xbrl_metrics か full_body がある記事を
    「本文付き」として扱い、バッチを縮小する判断に使う。
    """
    status = getattr(article, "body_status", "title_only")
    if status == "xbrl":
        return bool(getattr(article, "xbrl_metrics", None) or getattr(article, "full_body", None))
    if status == "pdf_text":
        return bool(getattr(article, "full_body", None))
    return False


def _format_xbrl_metrics(metrics: dict) -> str:
    """xbrl_metrics(dict) を人間可読の数値表テキストに整形する。"""
    basis = metrics.get("basis", "")
    type_label = _BASIS_LABELS.get(basis, basis or "不明")
    lines = [f"種別：{type_label}", "XBRL抽出数値（確定値・100%正確）："]
    for it in metrics.get("items", []):
        label = it.get("label", it.get("metric", ""))
        prev = it.get("previous")
        curr = it.get("current")
        unit = it.get("unit", "")
        pct = it.get("change_pct")
        direction = it.get("direction")
        dir_label = _DIRECTION_LABELS.get(direction, direction or "")
        prev_s = f"{prev:,}" if isinstance(prev, (int, float)) else str(prev)
        curr_s = f"{curr:,}" if isinstance(curr, (int, float)) else str(curr)
        pct_s = f"{pct:+}%" if isinstance(pct, (int, float)) else "－"
        line = f"  {label} 前回{prev_s}→今回{curr_s} {unit}（{pct_s}・{dir_label}）"
        prior = it.get("prior")
        if prior is not None:
            prior_s = f"{prior:,}" if isinstance(prior, (int, float)) else str(prior)
            line += f"（前期実績 {prior_s} {unit}）"
        lines.append(line)
    overall = metrics.get("overall")
    if overall:
        lines.append(f"機械方向判定：{overall}")
    return "\n".join(lines)


def _build_article_contents(index: int, article: object, body_max_chars: int) -> str:
    """記事1件を body_status で経路分岐し、LLM入力テキストを組み立てる。

    - xbrl: xbrl_metrics を数値表に整形（理由も付す）。metrics が無ければ full_body にフォールバック。
    - pdf_text: タイトル＋証券コード＋PDF抽出テキスト先頭N字。
    - それ以外（title_only/error）: 従来どおり タイトル＋body[:500]。
    """
    title = getattr(article, "title", "") or ""
    status = getattr(article, "body_status", "title_only")
    code = getattr(article, "security_code", None)
    header = f"【記事{index}】\nタイトル：{title}"
    if code:
        header += f"\n証券コード：{code}"

    if status == "xbrl":
        raw = getattr(article, "xbrl_metrics", None)
        metrics = None
        if raw:
            try:
                metrics = json.loads(raw)
            except (ValueError, TypeError):
                metrics = None
        if metrics:
            parts = [header, _format_xbrl_metrics(metrics)]
            reason = getattr(article, "correction_reason", None)
            if reason:
                parts.append(f"修正理由：{reason}")
            return "\n".join(parts)
        # 対象タグ非ヒット等で metrics が無い場合は PDF経路と同様に本文テキストを渡す
        full_body = getattr(article, "full_body", None)
        if full_body:
            return (
                f"{header}\n"
                "本文（XBRLテキスト抽出。前回予想と今回予想／前期実績を見つけ数値比較で方向判定せよ）：\n"
                f"{full_body[:body_max_chars]}"
            )
        body = getattr(article, "body", "") or ""
        return f"{header}\n本文：{body[:500]}"

    if status == "pdf_text":
        full_body = getattr(article, "full_body", None) or ""
        return (
            f"{header}\n"
            "本文（PDF抽出。本文中の前回予想と今回予想／前期実績を見つけ数値比較で方向判定せよ）：\n"
            f"{full_body[:body_max_chars]}"
        )

    # title_only / error / その他: 従来形を維持
    body = getattr(article, "body", "") or ""
    return f"{header}\n本文：{body[:500]}"


def analyze(title: str, body: str) -> AnalysisResult:
    """記事を分析する。DRY_RUN=true のときはモック結果を返す。"""
    if os.getenv("DRY_RUN", "").lower() == "true":
        return _mock_analyze(title)
    return _llm_analyze(title, body)


def analyze_batch(
    articles: list,
    batch_size: int = _BATCH_SIZE_DEFAULT,
    system_prompt: str | None = None,
    batch_interval: int | None = None,
    max_failures: int | None = None,
    model: str | None = None,
    body_max_chars: int | None = None,
    body_batch_size: int | None = None,
) -> List[Tuple[object, Optional[AnalysisResult]]]:
    """記事リストをまとめて分析し、(article, result) のリストを返す。

    1回のLLM呼び出しで複数記事を処理してリクエスト数を抑える。
    本文付き（XBRL数値 or PDFテキスト）記事はトークン消費が大きいため、本文なし記事より
    小さいバッチサイズ（body_batch_size）で別チャンクとして処理する（仕様書§8）。
    結果が返らなかった記事は result=None になる。
    """
    interval = batch_interval if batch_interval is not None else _BATCH_INTERVAL_SECONDS
    failures_limit = max_failures if max_failures is not None else _MAX_CONSECUTIVE_FAILURES
    used_model = model if model else _MODEL
    max_chars = body_max_chars if body_max_chars is not None else _BODY_MAX_CHARS
    body_size = body_batch_size if body_batch_size is not None else _BATCH_SIZE_WITH_BODY
    prompt = system_prompt if system_prompt else _SYSTEM_PROMPT
    effective_batch_prompt = (
        prompt
        + "\n複数の記事が番号付きで与えられます。"
        "記事ごとに1つの結果を返し、index フィールドに記事番号をそのまま入れてください。"
    )

    # 本文付き／本文なしで使うバッチサイズが違うため、チャンク列を先に組む。
    # 本文なしを通常サイズ、本文付きを縮小サイズに分けてもリクエスト総数は大きく増えない。
    plain = [a for a in articles if not _has_body(a)]
    with_body = [a for a in articles if _has_body(a)]
    chunks: list[list] = []
    for i in range(0, len(plain), batch_size):
        chunks.append(plain[i : i + batch_size])
    for i in range(0, len(with_body), body_size):
        chunks.append(with_body[i : i + body_size])

    dry_run = os.getenv("DRY_RUN", "").lower() == "true"
    results: List[Tuple[object, Optional[AnalysisResult]]] = []
    consecutive_failures = 0
    for idx, chunk in enumerate(chunks):
        if dry_run:
            results.extend((a, _mock_analyze(a.title)) for a in chunk)
            continue
        if idx > 0:
            time.sleep(interval)
        try:
            results.extend(
                _llm_analyze_batch(
                    chunk,
                    system_prompt=effective_batch_prompt,
                    model=used_model,
                    body_max_chars=max_chars,
                )
            )
            consecutive_failures = 0
        except Exception:
            logger.exception("Batch analysis failed for articles %s", [a.id for a in chunk])
            results.extend((a, None) for a in chunk)
            consecutive_failures += 1
            if consecutive_failures >= failures_limit:
                remaining = sum(len(c) for c in chunks[idx + 1 :])
                logger.error(
                    "Aborting after %d consecutive batch failures; %d articles left for next run",
                    consecutive_failures,
                    remaining,
                )
                break
    return results


def _mock_analyze(title: str) -> AnalysisResult:
    return AnalysisResult(
        sentiment="positive",
        summary=f"[DRY RUN] {title[:60]}",
        reason="[DRY RUN] モック判定のため根拠なし。",
        stocks=[{"name": "サンプル株式会社", "code": "9999"}],
    )


def _llm_analyze(title: str, body: str, _retry: int = 3) -> AnalysisResult:
    import time
    from pydantic import BaseModel
    from google import genai
    from google.genai import types

    class Stock(BaseModel):
        name: str
        code: str

    class Analysis(BaseModel):
        sentiment: Literal["positive", "negative", "neutral"]
        summary: str
        reason: str
        stocks: List[Stock]

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(_retry):
        try:
            response = client.models.generate_content(
                model=_MODEL,
                contents=f"タイトル：{title}\n\n本文：{body[:800]}",
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=Analysis,
                ),
            )
            result: Analysis = response.parsed
            return AnalysisResult(
                sentiment=result.sentiment,
                summary=result.summary,
                reason=result.reason,
                stocks=[{"name": s.name, "code": s.code} for s in result.stocks],
            )
        except Exception as e:
            if attempt < _retry - 1 and ("503" in str(e) or "429" in str(e)):
                wait = 10 * (attempt + 1) if "503" in str(e) else 30 * (attempt + 1)
                logger.warning("Retryable error, retrying in %ds: %s", wait, e)
                time.sleep(wait)
            else:
                raise


def _llm_analyze_batch(
    articles: list,
    system_prompt: str = _BATCH_SYSTEM_PROMPT,
    model: str = _MODEL,
    body_max_chars: int = _BODY_MAX_CHARS,
    _retry: int = 3,
) -> List[Tuple[object, Optional[AnalysisResult]]]:
    from pydantic import BaseModel
    from google import genai
    from google.genai import types

    class Stock(BaseModel):
        name: str
        code: str

    class Item(BaseModel):
        index: int
        sentiment: Literal["positive", "negative", "neutral"]
        summary: str
        reason: str
        stocks: List[Stock]

    class BatchAnalysis(BaseModel):
        items: List[Item]

    contents = "\n\n".join(
        _build_article_contents(i, a, body_max_chars) for i, a in enumerate(articles)
    )

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(_retry):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=BatchAnalysis,
                ),
            )
            parsed: BatchAnalysis = response.parsed
            by_index = {item.index: item for item in parsed.items}
            results: List[Tuple[object, Optional[AnalysisResult]]] = []
            for i, article in enumerate(articles):
                item = by_index.get(i)
                if item is None:
                    logger.warning("No result for article %s in batch response", article.id)
                    results.append((article, None))
                    continue
                results.append(
                    (
                        article,
                        AnalysisResult(
                            sentiment=item.sentiment,
                            summary=item.summary,
                            reason=item.reason,
                            stocks=[{"name": s.name, "code": s.code} for s in item.stocks],
                        ),
                    )
                )
            return results
        except Exception as e:
            if attempt < _retry - 1 and ("503" in str(e) or "429" in str(e)):
                wait = 10 * (attempt + 1) if "503" in str(e) else 30 * (attempt + 1)
                logger.warning("Retryable error, retrying in %ds: %s", wait, e)
                time.sleep(wait)
            else:
                raise
