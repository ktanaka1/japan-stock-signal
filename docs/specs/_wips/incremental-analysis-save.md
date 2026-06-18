# 機能仕様: 分析の逐次保存・実行上限・リトライ修正

> ステータス: **実装中**
> 起案日: 2026-06-18 / 起案: system_architect（課金有効化後のバックログ消化中に判明）
> 完了条件: 実装＆テスト完了後、要点をSPEC.mdへ統合し本ファイルを削除（_wips運用）

## 背景

`monitor/agent.py` の `run()` は `analyze_batch(全未読記事)` の戻り値を**全件処理後にまとめて保存**していた。
2026-06-18、Gemini課金有効化後に808件のバックログを処理しようとしたところ：

- `analyze_batch` は全バッチ完了まで返らない（808件で数十分〜数時間）
- 完了前に Render 再デプロイ／アイドル再起動が入ると **1件も保存されない**
- → Gemini課金が丸ごと無駄 ＋ 記事は未読のまま → 次回また最初から再分析（**二重課金**）

これを逐次保存・実行上限で恒久対策する。あわせて collector のリトライ例外指定の誤りも直す。

## 1. `analyze_batch` のジェネレータ化（バッチ単位でyield）

`monitor/analyzer.py` の `analyze_batch` を、**1バッチ処理するたびにそのチャンクの結果 `list[(article, result)]` を yield** するジェネレータに変更する。
- 連続失敗の打ち切り（`max_failures`）・バッチ間スリープ（`batch_interval`）はジェネレータ内に維持
- 打ち切り時は yield を止めて return（呼び出し側は保存済み分で完了）
- 既存の呼び出し元（`agent.run`）以外に直接利用箇所がないこと前提（要grep確認）

## 2. `agent.run` でバッチ完了ごとに保存・既読化

`analyses.save` / `signals.add` / `repository.mark_many_as_read` を**バッチごとのループ内**に移す。
集計（signal_count/skipped/failed）はループ横断で累積し最後にログ。

```python
for chunk in iter_analyze_batches(articles, ...):   # ← バッチごとに届く
    processed_ids = []
    for article, result in chunk:
        if result is None:
            failed += 1
            continue
        is_signal = ...
        if is_signal:
            signals.add(...)
        analyses.save(...)
        processed_ids.append(article.id)
    if processed_ids:
        repository.mark_many_as_read(processed_ids)   # ← バッチ完了ごとに確定
```

**中断耐性の根拠（冪等性）**:
- `analyses.save` は `INSERT OR IGNORE`（article_id UNIQUE）、`mark_as_read` は `is_read=1`
- → 中断されても保存済みバッチは確定。次回は既読化済み記事が未読から外れ、**重複なく続きから再開**

## 3. 1回あたりの処理上限（新設定 `max_articles_per_run`）

`agent.run` の未読取得を `get_unread(limit=settings["max_articles_per_run"])` に変更。
- **デフォルト 200**（約20〜30バッチ＝十数分。背景タスクとして許容）
- settings テーブルに追加し管理画面から変更可（既存 settings の作法に従う。マイグレーションで既定値INSERT）
- バックログは即時配信＋2時間ごとの定期monitorの**複数回実行で漸減**

## 4. `collector/fetcher.py` `_http_get_json` のリトライ例外修正

現状 `except httpx.HTTPError` は `HTTPStatusError`（4xx/5xx）も捕捉するため、**404/401/400 など恒久エラーまで無駄リトライ**していた。

- `except httpx.HTTPError` → **`except httpx.RequestError`**（通信エラー＝ConnectTimeout等のみリトライ）
- 4xx は `resp.raise_for_status()` が `HTTPStatusError` を投げ、RequestErrorに該当しないため**即時失敗**
- 5xx/429 は従来どおり status 判定で `continue` リトライ。リトライ枯渇後の `raise_for_status()` はそのまま伝播
- 重複した `raise_for_status()` の整理

## テスト観点

- `FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db` 必須
- `with TestClient(app) as c:` でlifespan→migrate（新設定の既定値INSERT含む）発火
- 逐次保存: バッチを複数に分けたとき、**途中で打ち切られても処理済みバッチが保存・既読化されている**こと（analyze_batchをモック or 小batchで検証）
- 冪等性: 同じ未読集合で2回流しても重複INSERTされない（INSERT OR IGNORE）こと
- 実行上限: 未読がN件あっても `max_articles_per_run` 件だけ処理されること
- fetcher: 404 は即raise（リトライしない=sleep回数0）、ConnectTimeout は計3試行、5xx は3試行後raise
