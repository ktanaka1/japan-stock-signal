# 機能仕様: 捕捉率フィードバック（pickup logic ブラッシュアップ）

> ステータス: **設計中（実装前）**
> 起案日: 2026-06-23 / 起案: （フォワードテストの手作業FBを常設化）
> 完了条件: 実装＆テスト完了後、要点をSPEC.mdへ統合し本ファイルを削除（_wips運用）

## 背景

本システムは **ニュース駆動**（TDnet開示＋RSS）で銘柄を拾う。2026-06-23の手作業調査（Task3）で、
当日の**値上がり率トップ30のうち自システムが拾えたのは1件のみ**と判明した（118記事分析→有効銘柄6件、97件が中立）。
出来高/値上がり上位の多くは新規開示を伴わず収集網の外、あるいは収集しても中立化していた。

この「市場の動意 vs 自分が拾えた銘柄」の照合を**毎営業日・自動で回す**のが本機能。

## 目的（最重要・機能の第一義）

**単なる捕捉率KPIの可視化ではない。目的は「ピックアップロジックのブラッシュアップ」。**
各サイクルは必ず **現行ロジックへ追加／修正する具体提案** をアウトプットすること。
捕捉率は手段（取りこぼしの定量化）であり、出口は「次にロジックのどのレバーを動かすべきか」の提示。

## データソース（httpx実地検証済み 2026-06-23）

Yahoo Finance JP ランキングページに埋め込まれた `__PRELOADED_STATE__`（JSON）から取得する。
**素のHTMLに構造化JSONが入っており、httpx直叩きで取得可能**（JSレンダリング不要。WebFetch/headless不要）。

- 値上がり率: `https://finance.yahoo.co.jp/stocks/ranking/up?market=all&term=daily`
- 出来高:     `https://finance.yahoo.co.jp/stocks/ranking/volume?market=all&term=daily`
- （検討）出来高前日比急増: `previousVolumeRate` 系のランキングがあれば「急増」検知に有用

抽出パス: `__PRELOADED_STATE__.mainRankingList.results[]`、各要素:

```json
{"rank": "3", "stockCode": "6533", "marketName": "東証PRM",
 "stockName": "(株)Ｏｒｃｈｅｓｔｒａ　Ｈｏｌｄｉｎｇｓ", "savePrice": "1,252", "date": "15:30",
 "rankingResult": {"changePriceRate": {"changePriceRate": "+20.97", "volume": "279,400"}, ...}}
```

- 1ページ50件。`changePriceRate`（値上がり率）/ `volume`（出来高）は ranking_type で入る側が変わる
- `marketName`（東証PRM/STD/GRT/ETF）で **ETF・指数連動を除外**できる
- `date`/`updateDateTime` が 15:30 / 16:xx ＝ **引け後の確定値**
- 取得失敗は collector と同方針で graceful degrade（runは記録だけ残し中断しない）

抽出実装メモ:
```python
m = re.search(r'__PRELOADED_STATE__\s*=\s*(\{.*\})', html, re.S)
results = json.loads(m.group(1))["mainRankingList"]["results"]
```
※ HTMLではなくJSONを読むので比較的安定だが、Yahoo側の構造変更で壊れうる。パーサは独立関数に切り出し、
　取得0件・キー欠落は「障害」として検知（後述）。

## 実行タイミング・配置

- 平日 **引け後**（例: JST 16:30 = UTC 07:30）に1回。確定値が出てから回す。
- 新ワークフロー `.github/workflows/feedback.yml` + `python -m feedback.agent`（4番目の定期役）。
- 計測タスクなので cron 遅延・スキップは許容（ADR-002 の直列実行は不要）。

## 突き合わせロジック

ランキング日 D の上位N銘柄それぞれについて、自システムの当日関連データと照合し status を判定する。

| status | 定義 | 意味 |
|---|---|---|
| `delivered` | D朝のdigestで配信済み（signals.notified_at が判定窓内） | ✅ 捕捉成功＝届けた |
| `signaled_not_delivered` | signals化したがフィルタ除外（impact<閾値 or 大型株） | 拾ったが届けなかった |
| `analyzed_neutral` | article_analyses にあるがシグナル化せず（中立/銘柄なし） | 拾ったが評価で落とした |
| `not_collected` | 自システムのどこにも無い | 収集網の外（ニュース無し） |

- 照合キー: ランキングは4桁 `stockCode`。自分側は signals/article_analyses の銘柄コード（4桁正規化）と
  `articles.security_code` を突き合わせる。
- **判定窓**: D の動意に対し「事前/同時に把握していたか」。窓 = D-1 15:00 JST 〜 D の朝digestまで
  （その朝の配信対象に入りうる収集分）。正確な境界は実装時に詰める。

## 捕捉率KPI

- **主指標**: 「中小型・非ETFの動意」に対する捕捉率（後述の母集団の議論）。
- 補助: 収集率（`1 - not_collected/N`）、シグナル化率、配信率。
- 層別: 市場区分（PRM/STD/GRT）・値幅帯で分解できると示唆が出る。

## 改善提案（必須アウトプット・ヒューリスティック）

miss の支配的クラスを現行ロジックのレバーへマッピングして提示する（v1は決定的ルール、v2でLLM統合）。

| 支配クラス | 示唆 | 推奨レバー |
|---|---|---|
| `not_collected` 多 | 収集網が狭い | TDnet `limit` 拡張 / **ランキング逆引きディスカバリ**（未捕捉上位を翌朝候補に）/ 情報源追加 |
| `analyzed_neutral` 多 | LLMの中立判定が過剰 | 中立基準・プロンプト見直し（数値開示の方向確定を強化） |
| `signaled_not_delivered`（impact不足）多 | impact閾値が高すぎ | `min_impact_for_notify` を実証チューニング |
| `signaled_not_delivered`（大型株）多 | 大型株フィルタが効きすぎ | `exclude_large_cap` 条件の見直し |

各runで「件数内訳＋支配クラス→推奨レバー」を `coverage_runs.proposal` に保存し管理画面に表示。

## DBスキーマ（migration 009・案）

```sql
CREATE TABLE IF NOT EXISTS coverage_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL DEFAULT (datetime('now')),  -- UTC
    ranking_date  TEXT NOT NULL,        -- 対象営業日(JST, YYYY-MM-DD)
    ranking_type  TEXT NOT NULL,        -- 'up' | 'volume'
    top_n         INTEGER NOT NULL,
    captured      INTEGER NOT NULL DEFAULT 0,  -- delivered 件数
    signaled      INTEGER NOT NULL DEFAULT 0,  -- signaled_not_delivered 件数
    neutral       INTEGER NOT NULL DEFAULT 0,  -- analyzed_neutral 件数
    not_collected INTEGER NOT NULL DEFAULT 0,
    capture_rate  REAL,                 -- captured / 母集団
    detail        TEXT,                 -- 各銘柄 [{rank,code,name,market,pct,status}] のJSON
    proposal      TEXT                  -- 支配クラス→推奨レバーのサマリ
);
CREATE INDEX IF NOT EXISTS idx_coverage_runs_date ON coverage_runs (ranking_date DESC);
```

## 管理画面の表示

`/admin` に「捕捉率」セクションを追加（または専用ページ）。
- 捕捉率の時系列（直近N営業日）
- 最新runの miss 内訳（status別件数）＋ 取りこぼし銘柄リスト（コード/社名/値幅/status）
- `proposal`（次に動かすべきレバー）

## 段階（MVP → 拡張）

- **v1（本WIPの範囲）**: ランキング取得(up+volume) → 照合 → 分類 → `coverage_runs` 記録 → 管理画面表示 →
  ヒューリスティック提案。**まず基準値を取り、取りこぼしの内訳を可視化する。**
- **v2（別WIP・本ループの出力を見て判断）**:
  - **ランキング逆引きディスカバリ**: 未捕捉の上位銘柄を翌朝の収集/分析トリガーにする（取りこぼしを次サイクルで捕捉）
  - LLMによる提案統合（週次）
  - 出来高前日比急増ランキングの追加

## 柱2破棄との整合

本機能は**引け後の事後計測**であり、場中のリアルタイム追走ではない（柱2破棄の方針を侵さない）。
v2の逆引きディスカバリも「前日終値ベースの動意を翌朝の候補に絞る」範囲に限定し、「寄り前に主役候補を絞る」
という本システムの価値定義の内側に収める。

## テスト観点

- `FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db` 必須（本番D1誤接続防止）。`with TestClient(app)` でlifespan→migrate。
- ランキングパーサ: `__PRELOADED_STATE__` を含むHTMLフィクスチャから results を正しく抽出（ETF除外・数値正規化）。
- 照合ロジック: 合成データで delivered / signaled_not_delivered / analyzed_neutral / not_collected の各分類を検証。
- 取得失敗: パース不能・0件で graceful degrade し、連続失敗を検知（TDnetアラートと同基盤の通知）。
- capture_rate 算出と proposal 生成（支配クラス→レバー）の妥当性。

## 設計判断（確定 2026-06-23）

1. **捕捉率の母集団**: **値上がり率上位・中小型非ETFのみ**（確定）。大型株(TOPIX Core30相当・
   `monitor/large_cap.py`)とETF/指数連動(`marketName`=東証ETF等)は分母から除外する。システムが
   大型/ETFを意図的に除外する設計なので、KPIも「拾うべき中小型の動意」に揃える。
2. **主軸ランキング**: **`up`（値上がり率）が主**。`volume`（出来高）は参考取得に留める
   （出来高上位は大型・指数ETF・需給で埋まり対象と乖離するため）。
3. **N**: **上位30**（確定）。取得は50件/ページだが上位30に絞り主役級に集中、ノイズを減らす。
4. **判定窓**（残・実装時に確定）: D-1 15:00 JST 〜 D 朝digest を既定とする。

> 出来高前日比急増(`previousVolumeRate`)ベースの主指標化は v2 で検討（要・端点の取得検証）。
