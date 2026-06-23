"""開示本文の取得・テキスト化（Step②）。

系統A（数値系）該当の TDnet 開示について、collector が新規記事保存時に1回だけ呼ぶ。
  - url_xbrl 有 → ZIP DL・解凍 → iXBRL htm のテキストを抽出（body_status='xbrl'）
  - url_xbrl 無 → document_url(PDF) DL → pypdf でテキスト抽出（body_status='pdf_text'）
  - 失敗・空・スキャンPDF・想定外 Content-Type → body_status='error'（full_body は None）

※ XBRL数値の構造化抽出（タグ×コンテキスト→xbrl_metrics）は Step③。ここは生テキスト保存のみ。

ログレベル方針（ユーザー必須要件）:
  404・XBRL無し等の「仕様どおりの想定内フォールバック」は INFO/DEBUG で出す。
  真に異常な失敗（予期せぬ例外）だけ warning にする。想定内の404でログを埋めない。
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from dataclasses import dataclass
from typing import Optional

import httpx
import pypdf
from lxml import etree

from collector import xbrl_parser

logger = logging.getLogger(__name__)

# 1リクエストの上限 ~20s（connect 5s / read 15s）
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
# 一過性失敗のみ計3試行（1s→3s バックオフ）。404/403 はリトライしない。
_BACKOFFS = (1.0, 3.0)
_MAX_BYTES = 10 * 1024 * 1024  # サイズガード ~10MB
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +collector)"
    ),
    # release.tdnet.info は Referer が無いと 403 を返す
    "Referer": "https://www.release.tdnet.info/",
}


@dataclass
class BodyResult:
    """本文取得の結果。Article に転記する値だけを持つ。"""
    body_status: str  # 'xbrl' | 'pdf_text' | 'error'
    full_body: Optional[str] = None
    security_code: Optional[str] = None
    security_name: Optional[str] = None  # 開示由来の権威ある社名（yanoshin company_name）
    # XBRL経路の構造化抽出結果（Step③）。PDF経路では常に None。
    xbrl_metrics: Optional[str] = None       # 正規化数値＋機械方向ラベルのJSON文字列（抽出0件なら None）
    correction_reason: Optional[str] = None  # 定性タグ由来の修正理由テキスト（無ければ None）


def fetch_body(
    *,
    document_url: Optional[str],
    url_xbrl: Optional[str],
    company_code: Optional[str],
    company_name: Optional[str] = None,
) -> BodyResult:
    """系統A該当開示の本文を取得・テキスト化する。

    例外は内部で握り、失敗時は body_status='error' を返す（呼び出し側を止めない）。
    company_name は本文取得の成否に関わらず権威ある社名として結果に付与する
    （銘柄同定をLLM任せにせず開示会社を確定するため）。
    """
    code = (company_code or "").strip() or None
    name = (company_name or "").strip() or None
    try:
        if url_xbrl:
            result = _fetch_xbrl(url_xbrl, code)
        elif document_url:
            result = _fetch_pdf(document_url, code)
        else:
            # メタ不足（想定内）。degrade。
            logger.info("body fetch: no document_url/url_xbrl; degrade to error")
            result = BodyResult(body_status="error", security_code=code)
    except Exception:
        # 予期せぬ例外のみ warning（想定内の404等はここまで来ない＝下層で握る）。
        logger.warning("body fetch: unexpected error; degrade to error", exc_info=True)
        result = BodyResult(body_status="error", security_code=code)
    # 社名は本文取得の経路・成否に依らず常に付与する（取得失敗でも開示会社は確定できる）。
    result.security_name = name
    return result


# --- XBRL（zip → iXBRL htm テキスト） ---


def _fetch_xbrl(url_xbrl: str, code: Optional[str]) -> BodyResult:
    resp = _http_get(url_xbrl)
    if resp is None:
        return BodyResult(body_status="error", security_code=code)

    ctype = (resp.headers.get("content-type") or "").lower()
    content = resp.content
    if len(content) > _MAX_BYTES:
        logger.info("body fetch: xbrl zip too large (%d bytes); degrade", len(content))
        return BodyResult(body_status="error", security_code=code)
    # zip の判定は Content-Type に依らずマジックバイトで（やのしんは text/html を返す事あり）
    if not content[:2] == b"PK":
        logger.info("body fetch: xbrl response not a zip (ctype=%s); degrade", ctype)
        return BodyResult(body_status="error", security_code=code)

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        logger.info("body fetch: xbrl bad zip; degrade")
        return BodyResult(body_status="error", security_code=code)

    target = _pick_ixbrl_entry(zf.namelist())
    if target is None:
        logger.info("body fetch: no ixbrl htm in zip; degrade")
        return BodyResult(body_status="error", security_code=code)

    text = _extract_html_text(zf.read(target))
    if not text:
        logger.info("body fetch: ixbrl text empty; degrade")
        return BodyResult(body_status="error", security_code=code)

    # Step③: zip 全体から構造化数値（前回↔今回 比較）と修正理由を抽出して付帯する。
    # 抽出0件でも body_status は 'xbrl' のまま（full_body テキストは Step④で LLM に渡せる）。
    metrics_json: Optional[str] = None
    correction_reason: Optional[str] = None
    try:
        metrics_json, correction_reason = xbrl_parser.parse(content)
        # company_code 未取得時は開示由来の SecuritiesCode で補完（新形式英数字に対応）。
        if not code:
            code = xbrl_parser.extract_security_code(content) or code
    except Exception:
        # 構造化抽出の失敗で本文保存まで落とさない（テキストは取れている）。
        logger.info("body fetch: xbrl structured extract failed; keep text only", exc_info=True)

    return BodyResult(
        body_status="xbrl",
        full_body=text,
        security_code=code,
        xbrl_metrics=metrics_json,
        correction_reason=correction_reason,
    )


def _pick_ixbrl_entry(names: list[str]) -> Optional[str]:
    """zip 内の iXBRL htm を選ぶ。'ixbrl' を含む htm/html を優先、無ければ任意の htm/html。"""
    htmls = [n for n in names if n.lower().endswith((".htm", ".html"))]
    if not htmls:
        return None
    for n in htmls:
        if "ixbrl" in n.lower():
            return n
    return htmls[0]


def _extract_html_text(raw: bytes) -> str:
    """iXBRL htm からテキストを抽出する（style/script は除外してノイズを抑える）。"""
    tree = etree.parse(io.BytesIO(raw), etree.HTMLParser(recover=True))
    root = tree.getroot()
    if root is None:
        return ""
    # CSS/JS のテキストが本文に混ざるのを防ぐ
    for bad in root.iter("style", "script"):
        bad.text = None
    parts = [t.strip() for t in root.itertext() if t and t.strip()]
    return _sanitize(" ".join(parts))


# --- PDF（document_url → pypdf テキスト） ---


def _fetch_pdf(document_url: str, code: Optional[str]) -> BodyResult:
    resp = _http_get(document_url)
    if resp is None:
        return BodyResult(body_status="error", security_code=code)

    ctype = (resp.headers.get("content-type") or "").lower()
    content = resp.content
    if len(content) > _MAX_BYTES:
        logger.info("body fetch: pdf too large (%d bytes); degrade", len(content))
        return BodyResult(body_status="error", security_code=code)
    if not content[:4] == b"%PDF":
        logger.info("body fetch: response not a pdf (ctype=%s); degrade", ctype)
        return BodyResult(body_status="error", security_code=code)

    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        logger.info("body fetch: pdf parse failed; degrade", exc_info=True)
        return BodyResult(body_status="error", security_code=code)

    text = _sanitize(text)
    if not text:
        # スキャン画像PDF等（テキスト無し）。想定内の degrade。
        logger.info("body fetch: pdf text empty (likely scanned); degrade")
        return BodyResult(body_status="error", security_code=code)

    return BodyResult(body_status="pdf_text", full_body=text, security_code=code)


# --- HTTP（堅牢化：一過性のみリトライ・404/403 は即失敗） ---


def _http_get(url: str) -> Optional[httpx.Response]:
    """GET。一過性失敗のみ計3試行。404/403/想定外は None を返す（degrade）。

    リトライ対象: ネットワーク例外・タイムアウト・5xx・429。
    """
    attempts = len(_BACKOFFS) + 1
    for i in range(attempts):
        try:
            resp = httpx.get(
                url,
                headers=_HEADERS,
                timeout=_TIMEOUT,
                follow_redirects=True,  # rd.php?... ラッパを実体まで辿る
            )
        except httpx.HTTPError as exc:
            # ネットワーク例外・タイムアウト＝一過性。リトライ。
            if i < len(_BACKOFFS):
                time.sleep(_BACKOFFS[i])
                continue
            logger.info("body fetch: transient http error after retries (%s)", type(exc).__name__)
            return None

        status = resp.status_code
        if status == 200:
            return resp
        if status == 404:
            # 実体が無い＝想定内。リトライせず即フォールバック（INFOで出す）。
            logger.info("body fetch: 404 (expected, no entity); degrade %s", url)
            return None
        if status == 403:
            # Referer は付与済み。リトライしても無駄＝失敗扱い。
            logger.info("body fetch: 403 forbidden; degrade %s", url)
            return None
        if status == 429 or 500 <= status < 600:
            # 一過性。リトライ。
            if i < len(_BACKOFFS):
                time.sleep(_BACKOFFS[i])
                continue
            logger.info("body fetch: %d after retries; degrade", status)
            return None
        # その他のステータスは想定外だが致命ではない。degrade。
        logger.info("body fetch: unexpected status %d; degrade %s", status, url)
        return None
    return None


def _sanitize(text: str) -> str:
    """連続する空白・改行を畳んで前後を strip する（DB保存・LLM入力の正規化）。"""
    if not text:
        return ""
    # 各行を strip し空行を捨て、過剰な空白を1つに畳む
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines).strip()
