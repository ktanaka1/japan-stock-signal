# WIP: PR TIMES RSS 取込（TDnetの死角＝非開示カタリストの収集）

> 状態: **Phase1 実装＆テスト完了**（要点は SPEC.md「PR TIMES プレスリリース取込（Phase 1）」へ反映済み）。
> 本ファイルは Phase2（カテゴリ別RDF/辞書のD1化＋管理画面メンテ）の設計メモとして残置する。
> 関連: メモリ [[news-source-evaluation]]。捕捉率FBの支配要因 `not_collected` の回収策。

## Phase1 実装メモ（完了）

- `store/listed_companies.py`: `data/listed_companies.tsv` を csv だけでロード。`normalize_name()`/`lookup()`。
  正規化キーが複数社に衝突する曖昧キーは除外（実データで8キー除外・有効3718社）。
- `scripts/refresh_listed_companies.py`: JPX `data_j.xls`→TSV 再生成（オフライン・xlrd はスクリプト専用、
  requirements.txt には入れない）。内国株式（プライム/スタンダード/グロース）3734社のみ採用。
- `collector/fetcher.py` `_fetch_prtimes()`: `prtimes_enabled!="true"` で即 no-op。dc_corp を名寄せ一致のみ
  Article化し権威コード/社名を付与（body=title）。`fetch_all()` から呼ぶ（TDnet/他RSSの失敗と独立）。
- settings: `prtimes_enabled`(既定false)・`prtimes_rss_url`(index.rdf)。
- `.gitignore`: `data/` を `data/*` に変え `!data/listed_companies.tsv` で辞書のみ追跡（ローカルDBは除外維持）。
- tests: `tests/test_prtimes.py`（正規化/辞書ロード/曖昧除外/収集の上場フィルタ/no-op）。

## 目的・背景

中小型/グロース株は「人気IPとのコラボ」「新技術の特許」「有名企業への採用」など、
TDnet（適時開示）に載らないプレスリリースで株価・出来高が動くことが多い。PR TIMES の
RSS(RDF)は**機械可読・403にならない**。本体の収集源（Yahoo/NHK/TDnet）に追加し、既存の
LLMインパクトスコア🔥(1〜5)で材料性を選別する。

## データソース（検証済み事実）

- `GET https://prtimes.jp/index.rdf` → 200・**1回200件**・RDF(RSS1.0)
- feedparser で取得可能。entry の主なフィールド:
  - `title`（PR見出し）／`link`（記事URL）／`updated`
  - **`dc_corp`（発表企業の社名。例: "株式会社ノジマ" "ReYuu Japan株式会社"）** ← 名寄せの鍵
  - `business_form`（"企業・官公庁・団体" 固定で選別には使えない）
- カテゴリ別RDFは未確認（推測URLは404）。**index.rdf ＋ 上場社名辞書での選別**を基本設計とする。

## 最大の課題と解（銘柄同定 ＝ ノイズ除去 を兼ねる）

PR TIMESは(1)RSSに証券コードが無い (2)大半が**非上場企業**。本システムは
「コードをLLMに推測させない（TDnetのように権威ソースで確定）」方針。よって:

**上場企業名→証券コードの辞書で `dc_corp` を名寄せし、一致したものだけ取り込む。**
これで「非上場のノイズ除去」と「権威あるコード付与」を**同時に**達成する。

### 辞書の作り方
- JPXの上場会社一覧（`data_j.xls`）から `銘柄名→コード4桁` を生成
- 配置案: `store/listed_companies.py`（起動時にロード）＋ データは `data/listed_companies.tsv`
  をリポジトリ同梱（月次でメンテ）。将来 D1 テーブル化も可
- **正規化**: 「株式会社」「(株)」「ホールディングス/HD」全角半角・空白を吸収して突合。
  一致は保守的に（高信頼一致のみ採用、表記ゆれは取りこぼしを許容＝誤爆を避ける）

## 統合方式

`collector/fetcher.py` に `_fetch_prtimes()` を追加し `fetch_all()` から呼ぶ:

1. index.rdf を feedparser でパース
2. 各 entry の `dc_corp` を正規化 → 上場辞書で突合
3. 一致した場合のみ `Article(title=title, body=title, url=link)` を生成し、
   `article.security_code = <辞書のコード>`（**権威コード**）/ `security_name=<正式社名>` を付与
4. 非一致（非上場・突合不可）は**破棄**（収集すらしない＝Gemini枠を浪費しない）
5. 既存どおり `repository.save_many` → 本体の精査(LLM)→インパクトスコア選別→シグナル

※ body はタイトルのみ（TDnetの系統B＝本文取得しないと同様）。LLMには title＋確定コードが渡る。

## 設定（settings）

- `prtimes_enabled`（既定 "false" → 検証後ON）
- `prtimes_rss_url`（既定 index.rdf。将来カテゴリ別が判明すれば差し替え可）

## テスト（FORCE_LOCAL_DB=1）

- index.rdf のサンプルRDFをモック → 上場社名(ノジマ等)はコード付きで Article 化、非上場は破棄
- 社名正規化（株式会社/(株)/HD/空白）の突合ユニットテスト
- 辞書ロード・未一致時の安全な破棄

## リスク・留意

- 表記ゆれ（ブランド名≠登記社名、子会社PR）で**取りこぼし（recall低下）はあり得る**が、
  誤ったコード付与（precision低下）の方が害が大きいので保守的突合を優先
- 1回200件・高頻度なので、collector の `max_articles_per_run` と Gemini レート枠に注意
  （非上場破棄で実数は大きく減る見込み）
- 辞書の鮮度（新規上場/社名変更）→ 月次メンテ。メンテ漏れは「取りこぼし」に留まり事故化しない

## Phase1 レビューでの補強（2026-07-02）と残リスク

- **二段引き化**: 剥がしキーのみだと別法人の上場2社（ソフトバンク9434/ソフトバンクグループ9984、
  ＧＭＯ4784/9449 等8キー16社）が曖昧破棄されていた → 一次キー（接尾辞を剥がさない完全一致）を
  先に引く二段構えで回収。剥がし結果が3文字未満のキー（ＣＥホールディングス→"ce"等）は
  誤爆面になるため剥がし辞書に載せない。
- **TSVから5桁コード除外**: 優先株・社債型種類株式6行（25935等）を除去、refreshスクリプトに
  `len(code)==4` フィルタ追加。
- **残リスク（Phase2で対処）**: JPXの銘柄名列は**略称**（例: 4849「エン」＝エン・ジャパン）。
  (a) 正式商号「エン・ジャパン株式会社」は不一致で取れない（recall損・安全側）、
  (b) 逆に無関係の非上場「エン株式会社」等が完全一致して誤コード付与し得る（precision損）。
  → **本番ONの前に「一致したが誤り」の目視検証を行う**。恒久対処は辞書のD1化＋管理画面での
  例外メンテ（略称→正式商号のオーバーライド、危険な短キーのdenylist）。

## 段階

- **Phase 1**: index.rdf ＋ 上場辞書突合 → 一致のみ収集 → 既存パイプライン。← まずここ
- **Phase 2**: カテゴリ別RDF特定でノイズ源を更に削減 / 辞書のD1テーブル化＋管理画面メンテ。
