# 機能仕様: LINE配信ログ記録・障害検知（digest run logging）

> ステータス: **実装中（DB記録＋メールアラートは実装済み / 管理画面は未実装）**
> 起案日: 2026-06-18 / 起案: system_architect
> 完了条件: 実装＆テスト完了後、要点をSPEC.mdへ統合し本ファイルを削除（_wips運用）

## 背景

`monitor/digest.py` の LINE 配信は成否を `logging` するだけで、結果がどこにも永続化されていなかった。
「最後にLINE通知が成功したのはいつか」を GitHub Actions のジョブログでしか追えず、配信失敗が起きても運営者が気づく手段がない。

SPEC.md の「無通知＝障害と区別」方針は**受信者側**の視点（シグナルゼロでも「該当なし」を1通送る）で実現済みだが、
配信そのものの失敗（LINE API 4xx/5xx）を検知する**運営者側**の手段が欠けていた。これを埋める。

なお、Gemini分析の失敗（`agent.py`、例: 6/18朝の「60件中30件失敗」）は分析レポートメール（`mailer.py`）で
可視化されている。本機能が対象とするのは配信層（LINE API）の失敗であり、分析層とは別系統。

## 方針（決定済み）

- **記録先**: `digest_runs` テーブルにrun単位で1行記録（遡って障害調査できるように）
- **即時検知**: 失敗時は既存SMTP基盤（`mailer.py`）で管理者へアラートメール
- **可視化**: 管理画面ダッシュボード（`/admin`）に直近の配信ログを表示
- GitHub Actions の exit code 制御（ジョブ失敗化）は今回採用しない（メール＋DBで十分なため）

## DBスキーマ（実装済み: `migrations/005_digest_runs.sql`）

```sql
CREATE TABLE IF NOT EXISTS digest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT    NOT NULL DEFAULT (datetime('now')),  -- UTC
    status       TEXT    NOT NULL,  -- 'ok' | 'partial' | 'error' | 'no_recipients'
    signal_count INTEGER NOT NULL DEFAULT 0,   -- 配信対象シグナル数（0=該当なし通知）
    line_ok      INTEGER NOT NULL DEFAULT 0,   -- LINE multicast 成功チャンク数
    line_error   INTEGER NOT NULL DEFAULT 0,   -- LINE multicast 失敗チャンク数
    error_detail TEXT,                          -- 例外traceback／失敗サマリ（正常時NULL）
    notified_ids TEXT                           -- 通知済みにしたsignal_idのJSON配列
);
CREATE INDEX IF NOT EXISTS idx_digest_runs_run_at ON digest_runs (run_at DESC);
```

`status` の意味:

| status | 意味 |
|--------|------|
| `ok` | 全チャンク送信成功 |
| `partial` | 一部チャンク失敗（受信者500件超で複数チャンクのとき） |
| `error` | 例外で送信できず／全チャンク失敗（LINE API 4xx/5xx・例外・トークン欠落等） |
| `no_recipients` | 受信者ゼロでスキップ（障害ではない第三の状態）。**受信者が全員不正IDで送信先ゼロになった場合もここに分類** |

## LINE配信の堅牢化（2026-06-18 追加）

本番で不正なuserId `U_test_dummy` が `recipients` に混入し、LINE multicast が `to[1] is invalid`
で 400 を返し、**1件の不正IDで全受信者への配信が落ちる**事故が発生した。さらに失敗理由
（HTTPステータス＋レスポンス本文）が `error_detail` に残らず Render ログ頼りだった。以下で恒久対策する。

1. **登録時バリデーション** — `store/recipients.add()` は userId が `^U[0-9a-f]{32}$` に合致しない
   ものを INSERT せず `logger.warning("invalid LINE userId rejected: ...")` のみ出す（例外は投げない
   ＝webhookハンドラを巻き込まない）。判定は `recipients.is_valid_user_id()` に切り出し、notifier側でも再利用。
2. **送信時フィルタ** — `notifier._multicast()` は送信前に同正規表現で受信者を絞り込み、不正IDを除外
   してから multicast する。除外件数は `logger.warning` で出す。全員除外で送信先ゼロなら `(0,0)` を返す
   →digest側で `no_recipients` 相当に扱う（誤って signals を notified にマークしない）。
3. **失敗詳細の伝播** — `notifier` の戻り値を `SendResult(ok, error, error_details)`（NamedTuple）に拡張。
   `error_details` は失敗チャンクごとの `"LINE {status}: {body}"` 文字列リスト。`digest.run()` は
   これを集約し `_build_line_error_detail()` で件数サマリ＋実エラー本文を `digest_runs.error_detail` と
   メールアラートに載せる。`SendResult` の先頭2要素は従来通り `(ok, error)` なので後方互換。

テスト: `tests/test_line_robustness.py`（pytest非依存の自前ハーネス、`FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db`、
`with TestClient(app)` でlifespan発火、httpx をモックして 200/400 両ケース検証）。10件全PASS。

## 実装範囲

### 済み（実装＆ローカルDB動作確認済み）

1. `migrations/005_digest_runs.sql` — 上記スキーマ
2. `store/digest_runs.py` — `record()` / `get_recent(limit)`（`signals.py` と同じ `db.execute()` 直書きパターン、`notified_ids` はJSON）
3. `monitor/notifier.py` — `send_text()` / `_multicast()` が `SendResult(ok, error, error_details)` を返す（先頭2要素は従来の `(ok, error)` 互換）。DRY_RUN時 `(1,0,[])`、受信者なし `(0,0,[])`。不正ID除外・失敗本文集約は「LINE配信の堅牢化」節を参照
4. `monitor/mailer.py` — `send_digest_alert(error_detail, run_at_jst)` 追加。`REPORT_EMAIL` 未設定時はスキップ。件名 `[株シグナル] LINE配信エラー {run_at_jst}`
5. `monitor/digest.py` — `run()` を `try/except/finally` 化。`finally` で `digest_runs.record()` を必ず実行、`error_detail` があれば `mailer.send_digest_alert()`。LINE失敗時はシグナルを通知済みにマークせず次回再送

### 未実装（これから）

6. `admin/routes.py` — `dashboard()` に `digest_runs.get_recent(30)` を追加し `runs` をテンプレートへ渡す
7. `admin/templates/index.html` — 「直近の配信ログ」テーブルを追加

## 管理画面の表示仕様（未実装）

ダッシュボード（`/admin`）の既存の日次統計カードの下に「直近の配信ログ」セクションを追加。

- 直近 **30件** を新しい順
- 列: `日時(JST)` / `結果` / `シグナル数` / `LINE送信(成/失)` / `エラー詳細`
- `run_at` はUTC保存なのでJST変換して表示（既存 `_to_jst_time()` を流用）
- `status` はバッジ表示: `ok`=緑 / `partial`=黄 / `error`=赤 / `no_recipients`=グレー
- `error_detail` は長いので折り返しまたは省略表示（traceback全文が入りうる）
- 別ページ遷移なしで「今日のパイプラインが動いたか」と同じ目線で確認できることを優先（独立ページは作らない）

## 影響・コスト

- D1: INSERT が1回/日増えるのみ（無料枠10万req/日に対し無視できる）
- ADR追記不要（通知チャネルの追加は既存方針の範囲内、新たな構造判断ではない）

## テスト観点

- `FORCE_LOCAL_DB=1 DB_PATH=/tmp/test.db` 必須（本番D1誤接続防止）
- `with TestClient(app) as c:` でlifespan→migrate発火を確認
- DRY_RUN時に `record('ok', ...)` が残ること
- LINE失敗（status_code≠200をモック）で `status='partial'/'error'`、`send_digest_alert` 呼び出し、シグナル未マークを確認
- `/admin` が `runs` を描画すること（認証済みリクエスト）
