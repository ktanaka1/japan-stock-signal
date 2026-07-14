"""設定値のDB管理。コードに埋め込まれた条件を管理画面から変更できるようにする。"""
from __future__ import annotations

import logging

from .db import execute

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_DEFAULT = (
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

_RSS_FEEDS_DEFAULT = "\n".join([
    "https://news.yahoo.co.jp/rss/topics/business.xml",
    "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.rss",
    "https://www.nhk.or.jp/rss/news/cat6.xml",
])

# 本文取得の一次フィルタ語彙（タイトル部分一致・rss_feedsと同パターンで改行区切り）。
# A: 数値系（業績・配当）= 本文取得対象。B: 非数値系（提携・分割等）= 取得しない。
_BODY_FETCH_TRIGGERS_NUMERIC_DEFAULT = "\n".join([
    "業績予想", "通期業績", "決算短信", "決算", "四半期",
    "業績修正", "上方修正", "下方修正", "配当予想", "増配", "減配",
    "剰余金の処分", "配当",
])
_BODY_FETCH_TRIGGERS_CATALYST_DEFAULT = "\n".join([
    "株式分割", "株式併合", "新株予約権", "第三者割当", "自己株式",
    "業務提携", "資本提携", "M&A", "合併", "TOB", "公開買付",
    "子会社化", "株式交換", "主要株主の異動", "減損", "特別損失",
])

# TDnetタイトル事前フィルタの保守的デナイリスト（タイトル部分一致で非材料の定型開示を除外）。
# デイトレのシグナルになり得ない定型・手続き的開示のみに絞る。材料性のある修正・決算・配当・
# 提携等は決して入れない（捕捉率を下げないため）。改行区切り。
_TDNET_TITLE_DENYLIST_DEFAULT = "\n".join([
    "コーポレート・ガバナンスに関する報告書",
    "独立役員届出書",
    "内部統制報告書",
    "有価証券報告書",
    "四半期報告書",
    "株主総会招集",
    "定時株主総会",
    "自己株券買付状況報告書",
    "決算説明会",
    "補足説明資料",
])

# EDINET 大量保有報告書アラートで除外する提出者（パッシブ/インデックス運用・信託口）。
# 部分一致で除外。これらは指数組換え等の機械的保有でデイトレ需給シグナルにならない。
_EDINET_FILER_DENYLIST_DEFAULT = "\n".join([
    "野村アセットマネジメント", "日興アセットマネジメント", "三菱ＵＦＪアセットマネジメント",
    "三井住友トラスト・アセットマネジメント", "三井住友ＤＳアセットマネジメント",
    "アセットマネジメントＯｎｅ", "ブラックロック", "ステート・ストリート", "バンガード",
    "日本マスタートラスト信託銀行", "日本カストディ銀行", "ＪＰモルガン",
])

DEFAULTS: dict[str, str] = {
    "gemini_model": "gemini-2.5-flash",
    "gemini_system_prompt": _SYSTEM_PROMPT_DEFAULT,
    "signal_sentiments": "positive,negative",
    "signal_require_stocks": "true",
    "rss_feeds": _RSS_FEEDS_DEFAULT,
    "batch_interval_seconds": "7",
    "max_consecutive_failures": "3",
    "max_articles_per_run": "200",
    "body_max_chars": "4000",
    "body_fetch_triggers_numeric": _BODY_FETCH_TRIGGERS_NUMERIC_DEFAULT,
    "body_fetch_triggers_catalyst": _BODY_FETCH_TRIGGERS_CATALYST_DEFAULT,
    # インパクトスコア(1〜5) がこの値以上のシグナルのみ LINE 配信する（旬の選別）。
    # 0 にすると全件配信（従来挙動）。既定は 4（サプライズ度4〜5を最優先）。
    "min_impact_for_notify": "4",
    # 大型株(TOPIX Core30相当・全銘柄が大型)のシグナルを配信から除外する（柱1）。
    "exclude_large_cap": "true",
    # TDnet取得がこの回数連続で失敗したら障害アラートメールを送る（collector）。
    "tdnet_fail_alert_threshold": "3",
    # TDnet(やのしん)から1回に取得する開示件数。引け後の窓溢れ対策で既定600。
    # 300だと重い日（2026-06-26は649件）にcronスキップが重なると窓から溢れる実害が出た。
    "tdnet_fetch_limit": "600",
    # テクニカル版 朝のシグナル（価格/出来高駆動の別系統スキャナ）の検知パラメータ。
    "tech_min_change_pct": "5.0",      # 値上がり率の下限(%)
    "tech_min_volume_surge": "2.0",    # 出来高急増倍率の下限(倍。当日出来高/直近平均)
    "tech_top_n": "30",                # 値上がり率ランキング上位何件を母集団にするか
    "tech_dedup_with_news": "true",    # 本体ニュース版で当日配信済みの銘柄を除外するか
    # 売買代金フロア(億円)。close×volume がこの額未満は薄商いノイズとして除外（全picks共通）。
    "tech_min_turnover_oku": "1.0",
    # 出来高ランキング(volume)も第2探索源にするか。価格より先に動く出来高先行を拾う。
    "tech_scan_volume": "true",
    # 出来高源の値上がり率下限(%)。up源(tech_min_change_pct)より緩く早期の上昇を拾う。
    "tech_vol_min_change_pct": "3.0",
    # 出来高源専用の急増倍率下限(倍)。出来高ランキング上位は恒常的に商いが多く
    # 自分の直近平均の2倍(tech_min_volume_surge)を超えることが稀なため、up源と分離する
    # （2026-07-14実測: 共用2.0だと12営業日でvol pickが3件しか生まれず成功基準の判定不能）。
    "tech_vol_min_surge": "1.3",
    # EDINET 大量保有報告書アラート（需給シグナル・別レーン）。
    "edinet_enabled": "false",              # キー設定後に有効化
    "edinet_new_only": "true",              # 新規(初回5%超)のみ配信。変更報告書は方向不明なので既定で抑制
    "edinet_filer_denylist": _EDINET_FILER_DENYLIST_DEFAULT,
    # データ保持: 中立かつ銘柄なしの記事をこの日数経過後に削除する（maintenance.cleanup）。
    "retention_days": "90",
    # TDnetタイトル事前フィルタ: 非材料の定型開示をLLMに掛けず除外し、Gemini枠/未読バックログを節約。
    # 既定OFF（捕捉率への影響を避けるため）。ON時のみ denylist のタイトル部分一致で除外する。
    "tdnet_title_prefilter_enabled": "false",
    "tdnet_title_prefilter_denylist": _TDNET_TITLE_DENYLIST_DEFAULT,
    # PR TIMES RSS 取込（TDnet非開示のカタリスト=中小型株プレスリリースの回収）。
    # dc_corp を上場辞書(data/listed_companies.tsv)で名寄せし一致のみ収集。既定OFF（検証後ON）。
    "prtimes_enabled": "false",
    "prtimes_rss_url": "https://prtimes.jp/index.rdf",
    # 逆引き昇格（rescue）: impact閾値で落としたが値上がり率ランキング上位に入ったシグナルを
    # 翌朝の配信に昇格する（signaled層の交差救済）。capture_rate には別枠計上（自己言及防止）。
    "rescue_enabled": "true",
    # これ未満のimpactは市場反応があってもノイズとして昇格しない。
    "rescue_min_impact": "3",
}


def get(key: str) -> str:
    try:
        result = execute("SELECT value FROM settings WHERE key = ?", (key,))
        if result.rows:
            return result.rows[0]["value"]
    except Exception:
        # settings テーブル未作成（マイグレーション前）や一時的なDBエラー時は
        # デフォルトにフォールバックする。原因調査のため警告は残す。
        logger.warning("settings.get(%s) failed; using default", key, exc_info=True)
    return DEFAULTS.get(key, "")


def set_value(key: str, value: str) -> None:
    execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
        (key, value),
    )


def get_all() -> dict[str, str]:
    try:
        result = execute("SELECT key, value FROM settings")
        db_data = {r["key"]: r["value"] for r in result.rows}
    except Exception:
        logger.warning("settings.get_all() failed; using defaults", exc_info=True)
        db_data = {}
    return {k: db_data.get(k, v) for k, v in DEFAULTS.items()}


def save_all(data: dict[str, str]) -> None:
    for key, value in data.items():
        if key in DEFAULTS:
            set_value(key, value)
