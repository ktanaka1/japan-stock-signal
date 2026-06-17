# 機能仕様: 開示の数値を読む動的分析（dynamic body analysis / XBRL優先＋PDFテキスト ハイブリッド）

> ステータス: **未実装（Phase B）**
> 起案日: 2026-06-17 / 改訂: PDF案 → XBRL案 → **XBRL優先＋PDFテキストのハイブリッド**（網羅率スパイク結果による）/ 起案: system_architect
> 完了条件: 実装＆テスト完了後、要点をSPEC.mdへ統合し本ファイルを削除（_wips運用）

> **設計方針（確定）**: 数値系開示は **XBRLが有れば XBRL の標準タグから確定数値を抽出**（高信頼）。
> XBRLが無い開示（核心の「業績予想の修正」は約8割がこれ）は、**開示PDFの埋め込みテキストを抽出して
> LLMに読ませ**、厳格プロンプトで上方/下方を判定する。
> **OCR（画像読み取り）と、自前の脆い表組みパーサは作らない**。PDFは「テキスト抽出してLLMに渡すだけ」。

---

## 0. なぜハイブリッドなのか（スパイク結果・2026-06-17 実測）

6営業日・690開示を実集計して網羅率を確定:

| 開示種別 | 件数 | XBRL有り |
|---|---|---|
| **業績予想の修正（核心）** | 26 | **5（19%）** |
| 配当予想の修正 | 23 | 9（39%） |
| 決算短信 | 122 | 113（**93%**） |

- **核心の「業績予想の修正」はXBRLが19%しか付かない。** これはやのしんの欠陥ではなく **TDnet本体にXBRLが存在しない**（PDFの`<id>`からXBRL zipを導出するURL規則も実証したが、XBRL無し開示は全パターン404＝本当に無い）。発行体が軽微な予想修正にXBRLを添付しないため。
- 決算短信は93%付くがそれは別物。**XBRL限定では核心の8割を取りこぼす** → だからPDFテキストのフォールバックが必須。
- XBRL抽出自体は完璧に機能（実例 マルマエ6264 FY2026/8 業績予想修正: 売上17,700→20,000・経常+30.0% を `tse-rvfc` タクソノミから確定抽出）。
- これら開示PDFは **機械生成のテキスト埋め込み型**（スキャン画像ではない）。`前回予想/今回修正` の表が**プレーンテキストで入っており**、テキスト抽出→LLM読解が成立する（OCR不要）。

---

## 1. 目的・背景（致命的欠陥の明記）

現行 monitor 分析は RSSの **タイトル + 約36字の極短サマリーだけ** をLLMに渡す
（`articles.body` は実測で平均36字・最大101字。開示の数値・本文を一切読んでいない）。

### 致命的欠陥（確定）
- 「**通期業績予想の修正に関するお知らせ**」のような無機質タイトルで、上方/下方がタイトルに出ず
  LLMが情報不足で `neutral`（=見送り）に落とす。
- 業績修正・決算サプライズ・増減配は **デイトレ最重要カタリスト**（翌朝のギャップ）。取りこぼしは仕様バグ。
- 現状は「開示担当者がタイトルに結果を書いてくれた時だけ当たる」運任せ構造。

### ゴール
タイトルに株価変動トリガー語を含む**数値系開示**について:
1. **XBRLが有る** → 標準タグから「前回予想/今回修正/前期実績」をクリーン抽出し、方向を機械確定。
2. **XBRLが無い** → 開示PDFの埋め込みテキストを抽出し、LLMに数値表ごと読ませて方向判定。
いずれも厳格プロンプトで **上方修正/増配/増益=positive、下方修正/減配/減益=negative** と白黒つけさせ、
タイトルだけで判定を完結させない。

### 既存設計との整合
- ADR-002（digest直列実行）・ADR-003（朝1通・非リアルタイム）維持。リアルタイム化ではない。
- ADR-001/004（D1・SQLite二重・ORMなし）維持。マイグレーションは追記のみ。
- 無料運用（Gemini gemini-2.5-flash 既定）維持。トークン影響は §8。

---

## 2. トリガーワード一次フィルタ（二系統）

| 系統 | 対象開示 | 処理 |
|---|---|---|
| **A. 数値系** | 業績予想の修正 / 決算短信 / 四半期決算 / 配当予想の修正 / 剰余金処分 | **本文取得対象**。XBRL有→§4、無→§5（PDFテキスト） |
| **B. 非数値系** | 業務提携 / 資本提携 / M&A / TOB / 株式分割・併合 / 主要株主異動 / 第三者割当 等 | 本文取得しない。タイトル＋RSSサマリーで判定（現状ロジックで概ね機能） |

- 判定はタイトルの**部分一致**。系統A該当記事だけ collector が本文取得を試みる。
- 語彙（初版・settingsで上書き可能 `body_fetch_triggers_numeric` / `..._catalyst`、`rss_feeds`と同パターン）:
  - A数値系: 業績予想、通期業績、決算短信、決算、四半期、業績修正、上方修正、下方修正、配当予想、増配、減配、剰余金の処分、配当
  - B非数値系: 株式分割、株式併合、新株予約権、第三者割当、自己株式、業務提携、資本提携、M&A、合併、TOB、公開買付、子会社化、株式交換、主要株主の異動、減損、特別損失
- 取りこぼし回避を優先し広めに取る。過剰ヒットのコストは軽微（§8）。

---

## 3. 本文取得の全体フロー（collector段で実施）

系統A該当記事について collector が以下を順に試みる:

```
1. やのしんJSON（recent.json）から document_url(PDF) / company_code / url_xbrl を取得
2. url_xbrl が有る  → ZIP DL・解凍 → iXBRL を lxml で標準タグ抽出（§4）→ body_status='xbrl'
3. url_xbrl が無い  → document_url(PDF) を DL → pypdf で埋め込みテキスト抽出（§5）→ body_status='pdf_text'
   - PDFがテキストを持たない(スキャン画像) / DL失敗 / 抽出空 → body_status='error' でフォールバック（タイトル＋サマリー）
4. 抽出結果を articles の追加カラムに保存（§7）

（系統B・非該当はそもそも本文取得せず body_status='title_only' のまま。§7の4値に統一。）
```

- TDnetは **RSS→JSONエンドポイントに切替**（`https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit=N`）。
  `url_xbrl` は `rd.php?<実URL>` リダイレクトラッパ。httpx follow_redirects で実体取得。
- Yahoo/NHK は従来RSSのまま（系統B扱い、本文取得しない）。

---

## 4. XBRL経路（有る時・高信頼）【実証済み】

iXBRL htm を lxml の HTMLParser でパースし `ix:nonFraction`/`ix:nonNumeric` を走査。
**タグ名 × contextRef（前回/今回・予想/実績・連結/単体）の組でホワイトリスト抽出**。`sign="-"`・`scale`・単位に注意。

### 抽出ターゲット（ホワイトリスト・未知タグは無視＝クラッシュしない）
- 業績（実績・予想）: `NetSales` / `OperatingIncome` / `OrdinaryIncome` /
  `ProfitAttributableToOwnersOfParent`(or `NetIncome`) / `EarningsPerShare`
  （`jppfs_cor:` 財表 と `tse-ed-t:`/`tse-rvfc:` サマリ・改定予想の両名前空間を候補に）
- 配当: `tse-ed-t:DividendPerShare`（Previous/Current/Result × Annual/YearEnd）
- 変化率: `ChangeInNetSales` / `ChangeInOperatingIncome` / `ChangeInOrdinaryIncome` 等（あれば方向が一発確定）
- メタ: `SecuritiesCode` / `DocumentName`
- 定性: `ReasonForDividendForecastCorrection` / `NoticeOfForecastCorrection` / `QualitativeInfo`系

### 実証例
- 配当修正（24100 キャリアデザインセンター, `tse-ed-t`）: 年間配当 前回130.00→今回160.00・前期100.00 →**増配**。
- 業績予想修正（6264 マルマエ, `tse-rvfc`）: 売上17,700→20,000・経常3,000→3,900(+30.0%)・純利2,700→3,300 →**上方修正**。
- 決算（40210, `jppfs_cor`）: 売上251,365→279,586・営業56,833→63,552・経常58,018→65,897 →**増収増益**。

コンテキスト優先順位（実装時確定）: 連結優先/単体fallback、Annual優先/YearEnd代替、`Previous`/`Current`/`Result`の区別。
抽出結果は「指標名・前回予想・今回・前期実績・単位」の**正規化JSON**にまとめ、方向ラベル（増収/減収・増益/減益・増配/減配）を **collector で機械計算**して付帯する。

---

## 5. PDFテキスト経路（XBRL無し時・核心の8割をカバー）

XBRLが無い系統A開示は `document_url` のPDFを取得し、**埋め込みテキストを抽出してLLMに渡す**。

- 抽出は **pypdf でテキスト抽出のみ**（`page.extract_text()`）。`Referer: https://www.release.tdnet.info/` 必須。
- **OCR・画像処理・自前の表組みパーサは作らない**（ユーザー方針）。テキストが取れなければ degrade（後述）。
- 実開示PDFは機械生成のテキスト埋め込み型で、`修正予想`の表（前回予想/今回予想/増減率）が平文で取れる
  （配当修正PDFで1ページ・約1365字をpypdfで抽出できることを実証済み）。
- 抽出テキストは長すぎる場合のみ先頭側を優先して上限N字に truncate（修正テーブル・理由は前半に集中）。N字は settings化。
- **数値の解釈はLLMに委ねる**（自前パーサを作らない方針のため）。§6の厳格プロンプトで「PDF本文中の前回予想と今回予想を比較し方向を確定せよ」と命令する。
- フォールバック（degrade）: PDFがテキストを持たない（スキャン）/DL失敗/抽出空 → `body_status='error'` を記録し、タイトル＋RSSサマリーで判定（現状水準）。パイプラインは止めない。

---

## 6. LLM入力の結合とプロンプト厳格化

### 6-1. 入力の組み立て（経路別）
- **XBRL経路**: 確定数値表＋機械方向ラベル＋理由テキスト＋証券コードを構造化して付与（数字読解の不確実性ゼロ）。
  ```
  種別：業績予想修正  証券コード：6264
  抽出数値(XBRL確定)： 売上 前回17,700→今回20,000(+13.0%) / 経常 3,000→3,900(+30.0%) / 純利 2,700→3,300
  機械判定：上方修正（=ポジ方向）
  修正理由：{ReasonForCorrection}
  ```
- **PDFテキスト経路**: タイトル＋証券コード＋種別＋**PDF抽出テキスト（先頭N字）**を渡す。
  LLMに「本文中の前回予想と今回予想（または前期実績）を見つけ、数値比較で方向を判定せよ」と指示。
- 本文/数値が付くのは **系統A・取得成功記事のみ**（全記事ではない）。付いた記事はバッチサイズを縮小。

### 6-2. プロンプト厳格化（settings `gemini_system_prompt` 上書き）
- 「**業績予想の修正・決算・配当の開示で、数値（XBRL確定値 or 本文）が与えられている場合、安易に neutral にしてはならない。**」
- 「**今回値 > 前回予想（上方修正・増配・増益）= positive、今回値 < 前回予想（下方修正・減配・減益）= negative**。」
- 「数値や機械方向ラベルがある場合はそれを根拠に方向を確定し、reason に前回予想比/前期比を明記せよ。」
- 「neutral は『株価に影響しない』と確信できる時のみ。数値があるのに情報不足を理由に neutral にしない。」
- コード直書きの `_SYSTEM_PROMPT` は DRY_RUN・フォールバック用に残す。

---

## 7. データモデル（追記マイグレーションのみ）

`articles.body`（RSSサマリー）は据え置き。抽出結果は別カラムに追記（すべて nullable / 後方互換）。

```sql
-- migrations/004_body_analysis.sql（新規・冪等・追記のみ）
ALTER TABLE articles ADD COLUMN security_code     TEXT;
ALTER TABLE articles ADD COLUMN xbrl_metrics      TEXT;   -- 正規化数値+機械方向ラベルのJSON（XBRL経路）
ALTER TABLE articles ADD COLUMN full_body         TEXT;   -- PDF抽出テキスト（PDF経路）
ALTER TABLE articles ADD COLUMN correction_reason TEXT;   -- XBRL定性タグの修正理由
ALTER TABLE articles ADD COLUMN body_status       TEXT NOT NULL DEFAULT 'title_only';
```

- `body_status`: **その開示をどのルートで処理したかを記録する4値に統一**する
  — `'xbrl'`(XBRL経路で抽出) / `'pdf_text'`(PDFテキスト経路で抽出) / `'title_only'`(系統B・非該当・degrade＝タイトル＋RSSサマリーで判定) / `'error'`(取得・抽出失敗)。
  細分ステータス（`xbrl_parsed`/`pdf_empty`/`fetch_failed`/`skipped`/`no_match` 等）は使わず、この4値に丸める（後の分析・デバッグの命綱）。
- 既存行は新カラム NULL / `body_status='title_only'` で従来挙動を維持。`ALTER TABLE ADD COLUMN` は D1/SQLite両対応。
- store層（`store/models.py` の Article、`store/repository.py` の SELECT/INSERT/プレースホルダ数）に列追加。

---

## 8. パイプライン実行箇所とレート/コスト影響

### 本文取得（XBRL DL/解凍・PDF DL/テキスト抽出）は collector で行う
- collector は1時間ごとでI/Oが分散。monitor（2時間ごと・Gemini集中）に置くと分析直前にI/Oが集中する。
- collector が保存時に 系統A判定 → XBRL or PDF 取得 → 抽出 → 保存まで担い、monitor は出来上がりを読むだけ。

### レート・コスト
- 取得側（やのしん/release.tdnet.info）: 認証不要・公開。系統A該当のみ取得、1時間あたり数件〜十数件でI/Oは軽い。
  `Referer` ヘッダ必須（無いと403）。やのしんのレート/規約は要確認（§11）。
- Gemini: 本文付き記事の入力が増える（XBRL=数百トークン / PDFテキスト=N字上限で抑制）。
  **リクエスト数は増えない**（バッチ内で入力を太らせるだけ）。本文付きはバッチサイズ縮小で1リクエストの肥大を防ぐ。
  出力スキーマ不変（sentiment/summary/reason/stocks）。gemini-2.5-flash 無料枠に収まる想定。
- 依存追加: `lxml`（iXBRL）, `pypdf`（PDFテキスト抽出のみ・OCRなし）。

---

## 9. 段階的実装計画

### Phase 1（本丸）
1. migration 004（`security_code`/`xbrl_metrics`/`full_body`/`correction_reason`/`body_status`）。store層に列追加。
2. collector: TDnet を **RSS→JSON切替**（`document_url`・`company_code`・`url_xbrl` 取得）。Yahoo/NHKは従来RSS。
3. collector: 系統A該当記事に対し **XBRL有→§4抽出 / 無→§5 PDFテキスト抽出**。失敗は degrade（§5）。
4. monitor: `xbrl_metrics` or `full_body` があればLLM入力に付与（§6）。本文付きはバッチ縮小。
5. プロンプト厳格化（§6-2）を settings に反映。
6. requirements に `lxml` / `pypdf` 追加。

### Phase 2（補強）
- `tse-rvfc`/`tse-ed-t` Forecast系コンテキスト網羅を運用データで拡充。
- `body_status` 分布を監視（`xbrl`/`pdf_text`/`title_only`/`error` の比率）。`error` が多ければ対象開示の傾向を分析。
- 系統B（提携・分割・株主異動）のタイトル判定チューニング。

### 非対象（やらない）
- **OCR・画像PDF読み取り・自前の表組みパーサ**（ユーザー方針）。
- **J-Quants 本番一次ソース**（無料は約12週遅延で当日signalに使えない・§ J-Quants評価）。
- リアルタイム/場中通知（ADR-003）。

---

## J-Quants評価（不採用の根拠）
無料プランは財務諸表に**約12週間の遅延**（公式data spec・複数ソースで確認）。当日朝のデイトレsignalには原理的に使えないため**不採用**。直接TDnet（やのしんJSON＋ZIP/PDF）が当日タイムリー性を満たす唯一の現実解。将来、過去データのバックテスト用途で併用余地はあるが本番一次ソースには採らない。

---

## 10. 受け入れ条件（qa_engineer向け）

1. **業績予想 上方/下方（XBRL経路）**: XBRL付き上方修正開示で `sentiment=positive`、reason に前回予想比の数値根拠。下方は `negative`。
2. **業績予想 上方/下方（PDFテキスト経路）**: XBRL無し・PDFあり上方修正開示で、PDFテキストから方向を読み `positive`。下方は `negative`。（核心の8割をここで救えること）
3. **増配/減配**: 配当 前回130→今回160 で `positive`、減配で `negative`。
4. **決算 増収増益/減収減益**: 売上251,365→279,586 等で増益方向が `positive`。
5. **二系統**: 系統A取得成功は `body_status` が `'xbrl'` or `'pdf_text'`。系統B・非該当は `'title_only'` のまま従来分析（コスト非増）。
6. **degrade耐性**: XBRL無し＋PDFもテキスト無し/DL失敗の記事が、`body_status='error'` を記録しつつクラッシュせず、タイトル＋サマリーで完走する。
7. **想定外タクソノミ/PDF**: ホワイトリスト外タグのみのZIPや空テキストPDFでも例外を投げず degrade。
8. **後方互換**: migration 004 適用後、既存記事（新カラムNULL）が従来どおり分析・配信される。
9. **証券コード**: `security_code` が保存され、新形式英数字（例 `376A0`）でも壊れない。
10. **無料枠**: monitorのGeminiリクエスト数が対応前後で増えない（入力をバッチ内拡張するのみ）。
11. **ローカルテスト**: 本番D1事故防止のため `CLOUDFLARE_*= DB_PATH=/tmp/test.db` 明示で検証（CLAUDE.md）。

---

## 11. 未確定事項・リスク

- **PDFテキスト経路の精度**: 自前パーサを作らずLLM読解に委ねるため、PDFレイアウト次第で前回/今回の取り違えがあり得る。
  §6-2の厳格プロンプトと、抽出テキストN字上限（修正表が先頭に来る前提）で対処。スキャンPDFはテキスト空→degrade。
- **XBRL網羅率（実測）**: 業績予想修正19%・配当39%・決算93%。XBRL経路は高信頼だが核心の多くはPDF経路頼み。
  `body_status` 分布を運用監視（PDF経路の貢献度・degrade率を可視化）。
- **タクソノミのコンテキスト多様性**: contextID（`..._PreviousMember_ForecastMember`等）は開示・世代で揺れる。連結優先/Annual優先等の優先ルールを実装時確定。未知は抽出0件扱い。
- **`sign`属性・単位**: マイナス符号・`scale`・単位（百万円）の取り違えは方向誤判定の致命傷。要テスト。
- **やのしんWEB-APIのレート/規約**: JSON多用時のレート・商用可否は要確認（collector 1時間ごとで低頻度想定）。`Referer` 必須。
- **証券コード形式**: 4桁とは限らない（実測 `376A0`）。既存「4桁前提」ロジック・表示と整合確認（要 developer）。
- **理由テキスト/PDFテキストのN字上限**: settings化して運用調整。

---

## 関連
- ADR-002（digest直列実行）, ADR-003（朝ダイジェスト・非リアルタイム）, ADR-001/004（D1・二重ストア）
- 既存: `monitor/analyzer.py` / `collector/fetcher.py` / `collector/agent.py` / `store/repository.py` / `store/models.py` / `migrations/`
- 同 _wips: `data-retention.md`（抽出本文で保存容量がやや増える点に留意）
