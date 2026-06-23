from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Article:
    title: str
    body: str
    url: str
    id: Optional[int] = None
    fetched_at: Optional[datetime] = None
    is_read: bool = False
    # --- 動的本文分析（migration 004）。すべて nullable・後方互換 ---
    security_code: Optional[str] = None       # 開示由来の証券コード（新形式英数字あり 例 376A0）
    security_name: Optional[str] = None       # 開示由来の権威ある社名（yanoshin company_name）
    xbrl_metrics: Optional[str] = None        # XBRL経路の正規化数値＋機械方向ラベルのJSON文字列
    full_body: Optional[str] = None           # PDF経路で抽出した本文テキスト
    correction_reason: Optional[str] = None   # XBRL定性タグの修正理由
    body_status: str = "title_only"           # 処理ルート: 'xbrl' | 'pdf_text' | 'title_only' | 'error'
