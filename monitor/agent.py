"""
精査役エージェント
  - 未読記事をLLMでまとめて分析（ポジティブ/ネガティブ/中立 + 関連銘柄）
  - 銘柄が特定できたポジ/ネガ記事だけをシグナルとしてDBに蓄積する
  - 通知は行わない（通知役 monitor.digest が朝にまとめて配信）
  - 実行: python -m monitor.agent
"""
from __future__ import annotations

import logging
import re
import sys

from store.db import migrate
from store import repository, signals, analyses
from store import settings as cfg
from monitor.analyzer import analyze_batch
from monitor import quotes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# 東証の証券コードは4文字（数字、または新コード体系の英数字）。
# 空コードや桁違い（例: 5桁の "89720"）はLLM由来の誤りなので登録時に除外する。
_CODE_RE = re.compile(r"^[0-9A-Za-z]{4}$")


def _normalize_security_code(code) -> str | None:
    """開示由来コードを4桁(英数)の証券コードへ正規化する。該当しなければ None。

    yanoshin の company_code は '65330'(=6533) のような5桁（証券コード4桁＋チェック桁）。
    先頭4桁を採る。新形式英数 '587A4'→'587A' も同様。XBRL由来の4桁はそのまま。
    """
    code = str(code or "").strip()
    if len(code) == 5:
        code = code[:4]
    return code if _CODE_RE.match(code) else None


def _reconcile_identity(article: object, stocks: list) -> list:
    """TDnet開示の開示会社を権威ある(コード, 社名)で確定する。

    記事が権威ある security_code / security_name を持つ場合、その証券コードの銘柄の
    社名をLLM生成値で上書きせず権威値に確定する（社名ハルシネーション対策）。LLMが当該
    コードを挙げていなければ先頭に追加する。RSS由来（権威情報なし）は何もしない。
    """
    name = (getattr(article, "security_name", None) or "").strip()
    auth_code = _normalize_security_code(getattr(article, "security_code", None))
    if not auth_code or not name:
        return stocks
    matched = False
    for s in stocks:
        if str(s.get("code", "")).strip() == auth_code:
            s["name"] = name  # LLMが捏造した社名を権威社名で確定する
            matched = True
    if not matched:
        # LLMが開示会社のコードを挙げていない＝同定漏れ。権威データで補う。
        stocks.insert(0, {"name": name, "code": auth_code})
    return stocks


def _enrich_and_validate(article: object, stocks: list) -> list:
    """株価を付与し、Yahooに実在しない(HTTP 404)コードの銘柄を除外する。

    LLMが生成した存在しない証券コード（捏造）を配信前に弾く。ただし:
      - 404以外（タイムアウト/5xx等のtransient）は実在判定不能として除外しない
      - TDnet由来の権威コード(security_code)は404でも残す（Yahoo未収録の実在銘柄の誤除外を防ぐ）
    enrich が付ける一時フラグ _nonexistent はここで全銘柄から取り除く（保存JSONに残さない）。
    """
    quotes.enrich(stocks)
    auth_code = _normalize_security_code(getattr(article, "security_code", None))
    kept = []
    for s in stocks:
        nonexistent = s.pop("_nonexistent", False)
        if nonexistent and s.get("code") != auth_code:
            logger.info("Dropping nonexistent code on Yahoo: %s (article %s)",
                        s.get("code"), getattr(article, "id", None))
            continue
        kept.append(s)
    return kept


def _clean_stocks(stocks: list) -> list:
    """銘柄リストから不正なコードの銘柄を除外し、コードを trim して返す。

    4文字の数字/英数コードのみ残す。空・桁違いの銘柄は捨てる。
    これにより銘柄ゼロのシグナル化や、株価が引けない不正コードの保存を防ぐ。
    """
    cleaned = []
    for s in stocks:
        code = str(s.get("code", "")).strip()
        if _CODE_RE.match(code):
            s["code"] = code
            cleaned.append(s)
        else:
            logger.info("Dropping stock with invalid code: name=%s code=%r", s.get("name"), code)
    return cleaned


def _is_prefiltered_tdnet(article: object, denylist: list) -> bool:
    """TDnet開示のうち、タイトルが非材料の定型開示に該当するか（LLMに掛けず除外）。

    TDnet開示（url が tdnet.info）にのみ適用する。RSSニュースは対象外（タイトルが
    多様で誤除外のリスクが高いため）。捕捉率を下げないよう denylist は保守的に保つ。
    """
    if "tdnet.info" not in (getattr(article, "url", "") or "").lower():
        return False
    title = getattr(article, "title", "") or ""
    return any(kw and kw in title for kw in denylist)


def _apply_title_prefilter(articles: list, settings: dict) -> list:
    """事前フィルタが有効なら、非材料の定型TDnet開示をLLM前に除外して既読化する。

    除外分は neutral・銘柄なしの分析として確定（既読化）し、未読バックログから外す。
    silent な除外を避けるため件数をログ出力する。戻り値は分析対象として残す記事。
    """
    if settings.get("tdnet_title_prefilter_enabled", "false").lower() != "true":
        return articles
    denylist = [k.strip() for k in settings.get("tdnet_title_prefilter_denylist", "").splitlines() if k.strip()]
    if not denylist:
        return articles

    to_analyze, filtered = [], []
    for a in articles:
        (filtered if _is_prefiltered_tdnet(a, denylist) else to_analyze).append(a)
    if filtered:
        for a in filtered:
            analyses.save(
                a.id, "neutral", a.title,
                "事前フィルタ: 非材料の定型開示（タイトル該当）でLLM分析をスキップ",
                [], False, impact=1,
            )
        repository.mark_many_as_read([a.id for a in filtered])
        logger.info("Pre-filtered %d non-material TDnet disclosures by title (LLM skipped)", len(filtered))
    return to_analyze


def run() -> None:
    migrate()
    settings = cfg.get_all()

    # 1回の実行で処理する未読の上限。背景タスクとして許容できる件数に抑え、
    # 大量バックログは複数回の実行で漸減させる（仕様書§3）。
    articles = repository.get_unread(limit=int(settings["max_articles_per_run"]))
    if not articles:
        logger.info("No unread articles")
        return

    # TDnetタイトル事前フィルタ（既定OFF）。非材料の定型開示をLLM前に除外し枠を節約する。
    articles = _apply_title_prefilter(articles, settings)
    if not articles:
        logger.info("No unread articles to analyze after prefilter")
        return

    logger.info("Analyzing %d unread articles", len(articles))

    signal_sentiments = set(settings["signal_sentiments"].split(","))
    require_stocks = settings["signal_require_stocks"].lower() == "true"

    signal_count = 0
    skipped = 0
    failed = 0

    # バッチが届くたびに保存・既読化する（逐次保存）。
    # 途中で中断・打ち切りされても、処理済みバッチは確定して未読から外れる。
    batches = analyze_batch(
        articles,
        system_prompt=settings["gemini_system_prompt"],
        batch_interval=int(settings["batch_interval_seconds"]),
        max_failures=int(settings["max_consecutive_failures"]),
        model=settings["gemini_model"],
        body_max_chars=int(settings["body_max_chars"]),
    )
    for chunk in batches:
        processed_ids = []
        for article, result in chunk:
            if result is None:
                failed += 1
                continue  # 分析失敗分は未読のまま残し、次回再挑戦する
            # 不正コードの銘柄を登録前に除外する（空コード・桁違いを弾く）。
            result.stocks = _clean_stocks(result.stocks)
            # TDnet開示は開示会社を権威データで確定する（LLMの社名捏造・同定漏れを補正）。
            result.stocks = _reconcile_identity(article, result.stocks)
            is_signal = result.sentiment in signal_sentiments and (
                not require_stocks or bool(result.stocks)
            )
            if is_signal:
                # シグナル銘柄に株価（終値・前日比%・出来高）を付与し、Yahooに実在しない
                # コード（LLM捏造）を配信前に除外する。result.stocks をインプレース更新するため
                # 後続の analyses.save も同じ内容になる。取得失敗(transient)は価格なしで進む。
                result.stocks = _enrich_and_validate(article, result.stocks)
                # 実在コードが全て消えた（捏造のみだった）場合はシグナルにしない。
                if require_stocks and not result.stocks:
                    is_signal = False
            if is_signal:
                signals.add(
                    article.id, result.sentiment, result.summary, result.stocks, article.url,
                    impact=result.impact,
                )
                signal_count += 1
                logger.info(
                    "Signal: article %d, %s, stocks=%s",
                    article.id,
                    result.sentiment,
                    [s["code"] for s in result.stocks],
                )
            else:
                skipped += 1
            analyses.save(
                article.id,
                result.sentiment,
                result.summary,
                result.reason,
                result.stocks,
                is_signal,
                impact=result.impact,
            )
            processed_ids.append(article.id)
        # バッチ完了ごとに既読を確定する（次回はこの分を除いた未読から再開）。
        if processed_ids:
            repository.mark_many_as_read(processed_ids)

    logger.info(
        "Done: %d signals, %d skipped (neutral/no-stock), %d failed",
        signal_count,
        skipped,
        failed,
    )


if __name__ == "__main__":
    run()
