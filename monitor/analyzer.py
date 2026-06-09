"""
監視役エージェント - LLM判定モジュール
ニュース記事の株価センチメント分析と関連銘柄の特定を行う。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Literal

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "あなたは日本株デイトレーダーを支援するアナリストです。\n"
    "ニュース記事を読み、以下のフィールドを必ず埋めてください：\n"
    "sentiment: positive（株価上昇要因）/ negative（株価下落要因）/ neutral（株価影響なし）\n"
    "summary: 日本語で1〜2行の要約\n"
    "stocks: 関連する日本株銘柄のリスト（証券コード4桁と銘柄名）。"
    "銘柄が特定できない場合は空リストにする。"
)


@dataclass
class AnalysisResult:
    sentiment: str  # "positive" | "negative" | "neutral"
    summary: str
    stocks: List[dict]  # [{"name": "日本製鉄", "code": "5401"}]


def analyze(title: str, body: str) -> AnalysisResult:
    """記事を分析する。DRY_RUN=true のときはモック結果を返す。"""
    if os.getenv("DRY_RUN", "").lower() == "true":
        return _mock_analyze(title)
    return _llm_analyze(title, body)


def _mock_analyze(title: str) -> AnalysisResult:
    return AnalysisResult(
        sentiment="positive",
        summary=f"[DRY RUN] {title[:60]}",
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
        stocks: List[Stock]

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(_retry):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
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
                stocks=[{"name": s.name, "code": s.code} for s in result.stocks],
            )
        except Exception as e:
            if attempt < _retry - 1 and "503" in str(e):
                wait = 10 * (attempt + 1)
                logger.warning("503 received, retrying in %ds...", wait)
                time.sleep(wait)
            else:
                raise
