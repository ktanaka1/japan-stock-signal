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
