"""
精査役エージェント - LLM判定モジュール
ニュース記事の株価センチメント分析と関連銘柄の特定を行う。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "あなたは日本株デイトレーダーを支援するアナリストです。\n"
    "ニュース記事を読み、以下のフィールドを必ず埋めてください：\n"
    "sentiment: positive（株価上昇要因）/ negative（株価下落要因）/ neutral（株価影響なし）\n"
    "summary: 日本語で1行、見出しのように簡潔な要約\n"
    "reason: sentiment をそう判断した根拠を日本語で2〜3文で説明する。"
    "中立と判断した場合はその理由も記載する。\n"
    "stocks: 関連する日本株銘柄のリスト（証券コード4桁と銘柄名）。"
    "銘柄が特定できない場合は空リストにする。"
)

_BATCH_SYSTEM_PROMPT = (
    _SYSTEM_PROMPT
    + "\n複数の記事が番号付きで与えられます。"
    "記事ごとに1つの結果を返し、index フィールドに記事番号をそのまま入れてください。"
)

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


def analyze(title: str, body: str) -> AnalysisResult:
    """記事を分析する。DRY_RUN=true のときはモック結果を返す。"""
    if os.getenv("DRY_RUN", "").lower() == "true":
        return _mock_analyze(title)
    return _llm_analyze(title, body)


def analyze_batch(
    articles: list,
    batch_size: int = 10,
    system_prompt: str | None = None,
    batch_interval: int | None = None,
    max_failures: int | None = None,
    model: str | None = None,
) -> List[Tuple[object, Optional[AnalysisResult]]]:
    """記事リストをbatch_size件ずつまとめて分析し、(article, result) のリストを返す。

    1回のLLM呼び出しで複数記事を処理してリクエスト数を抑える。
    結果が返らなかった記事は result=None になる。
    """
    interval = batch_interval if batch_interval is not None else _BATCH_INTERVAL_SECONDS
    failures_limit = max_failures if max_failures is not None else _MAX_CONSECUTIVE_FAILURES
    used_model = model if model else _MODEL
    prompt = system_prompt if system_prompt else _SYSTEM_PROMPT
    effective_batch_prompt = (
        prompt
        + "\n複数の記事が番号付きで与えられます。"
        "記事ごとに1つの結果を返し、index フィールドに記事番号をそのまま入れてください。"
    )

    dry_run = os.getenv("DRY_RUN", "").lower() == "true"
    results: List[Tuple[object, Optional[AnalysisResult]]] = []
    consecutive_failures = 0
    for i in range(0, len(articles), batch_size):
        chunk = articles[i : i + batch_size]
        if dry_run:
            results.extend((a, _mock_analyze(a.title)) for a in chunk)
            continue
        if i > 0:
            time.sleep(interval)
        try:
            results.extend(_llm_analyze_batch(chunk, system_prompt=effective_batch_prompt, model=used_model))
            consecutive_failures = 0
        except Exception:
            logger.exception("Batch analysis failed for articles %s", [a.id for a in chunk])
            results.extend((a, None) for a in chunk)
            consecutive_failures += 1
            if consecutive_failures >= failures_limit:
                logger.error(
                    "Aborting after %d consecutive batch failures; %d articles left for next run",
                    consecutive_failures,
                    len(articles) - i - len(chunk),
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
    articles: list, system_prompt: str = _BATCH_SYSTEM_PROMPT, model: str = _MODEL, _retry: int = 3
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
        f"【記事{i}】\nタイトル：{a.title}\n本文：{a.body[:500]}"
        for i, a in enumerate(articles)
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
