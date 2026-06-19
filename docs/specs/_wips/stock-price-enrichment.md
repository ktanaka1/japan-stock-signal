# 機能仕様: シグナルへの株価付与・配信・絞り込み（stock price enrichment）

> ステータス: **実装中**
> 起案日: 2026-06-19 / 起案: ユーザー要望（LINE/メールに株価が無く判断材料が不足）
> 完了条件: 実装＆テスト完了後、要点をSPEC.mdへ統合し本ファイルを削除（_wips運用）

## 背景

LINE/メールの配信内容に **株価が無く**、デイトレ判断（値ごろ感・建玉サイズ・値動き）ができない。
シグナルの銘柄に株価（終値・前日比%・出来高）を付与し、配信に載せ、管理画面で価格・値動きで絞り込めるようにする。

## 確定した方針（ユーザー判断）

- **データ源**: Yahoo Finance（無料・前日終値が取れる）。J-Quants無料は約12週間遅延のため不採用。
- **載せる値**: 終値 + 前日比% + 出来高
- **絞り込み軸**: 価格帯（min/max円）+ 前日比%（min/max）の両方

## 実装方針（技術判断）

### 1. 取得モジュール `monitor/quotes.py`
- 新規依存なし。既存の `httpx` で Yahoo chart API を叩く。
  `https://query1.finance.yahoo.com/v8/finance/chart/{code}.T?interval=1d&range=5d`
- 日足 close 配列の末尾2点から `close`（直近終値）・`change_pct`（前日比%）を、volume 末尾から `volume` を算出。`asof` は末尾 timestamp（JST日付）。
- 取得失敗（通信/4xx/パース不能/データ不足）は **握りつぶして None を返す**。株価が取れなくてもシグナル生成・配信は止めない。
- `enrich(stocks: list[dict]) -> None`: 各銘柄dictに `close/change_pct/volume/asof` を**インプレースで追加**（取得失敗時はキーを付けない）。

### 2. 取得タイミング・保存（`monitor/agent.py`）
- **シグナル生成時のみ**（`became_signal` の銘柄）`quotes.enrich(result.stocks)` を呼ぶ。Yahoo呼び出しをシグナル数に限定。
- 朝の直列実行（収集→分析→配信, ADR-002）では「前営業日終値」になり目的に合致。
- スキーマ追加なし。`stocks` JSON（`[{name,code}]`）に価格キーを埋め込む。`signals.stocks` と `article_analyses.stocks` の両方に同じ enriched stocks が入る（agent.py で enrich 後に両方へ保存）。

### 3. 配信フォーマット
- `monitor/digest.py` `_format_item`: 銘柄ごとに株価行を追加。
  例: `📊 2,850円 (+1.8%) 出来高 123万株`。価格が無い銘柄は株価行を省略。
- `monitor/mailer.py` `_format_entry`（分析レポート）: 同様に株価を付記。

### 4. 管理画面の絞り込み（`/admin/articles`）
- `store/analyses.py` `_build_article_filter` に `price_min/price_max/chg_min/chg_max` を追加。
  `json_each(aa.stocks)` で **いずれかの銘柄が条件に合致** する記事を抽出（既存のcode絞り込みと同方式）。
  - 価格: `CAST(json_extract(je.value,'$.close') AS REAL) BETWEEN ? AND ?`
  - 前日比%: `CAST(json_extract(je.value,'$.change_pct') AS REAL) BETWEEN ? AND ?`
- `admin/routes.py` `articles_view` にクエリパラメータ追加 → store へ委譲、テンプレートへ受け渡し。
- `admin/templates/articles.html`: 銘柄列に株価表示＋価格/前日比%の入力フィルタUI。ページャーに条件引き継ぎ。

## 注意・既知の制約
- 既存シグナル（過去データ）は価格を持たない → 価格表示は「—」、価格フィルタには非マッチ。
- v1 は **シグナル生成時スナップショット**。場中に生成されたシグナルを翌朝配信する場合、終値ではなく生成時点の値になりうる（許容。将来 digest 時リフレッシュで改善余地）。
- Yahoo chart API は非公式エンドポイント。レート制限/仕様変更リスクあり。シグナル数は少数のため当面許容。User-Agent を付与する。

## テスト（FORCE_LOCAL_DB=1 + TestClient）
- `quotes`: Yahooレスポンスをモックして close/change_pct/volume 算出、失敗時 None。
- agent: signal生成時に enrich が呼ばれ stocks に価格が乗る（DRY_RUN）。
- digest/mailer: 価格あり/なしの整形。
- 管理画面: price_min/max・chg_min/max でのフィルタ一致と件数、ページャー引き継ぎ。
