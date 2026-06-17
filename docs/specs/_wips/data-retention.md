# 機能仕様: データ保持・ローテーション

> ステータス: **未実装（Phase Bで実装予定）**
> 決定日: 2026-06-17

## 背景

`articles` は約250件/日（≒9万件/年）で無制限に増え続ける。`article_analyses` も分析済み記事と1:1で増える。
ストレージ逼迫は当面（数年）問題にならないが、長期的な肥大化と全期間スキャンの重さを避けるため、価値の低い記事を一定期間後に削除する。

## 削除対象（決定済み）

**「中立（neutral）かつ 紐づく銘柄なし」かつ 90日より古い** 記事のみを削除する。

| 分析結果 | 削除 |
|---------|------|
| signals に登録済み（pos/neg + 銘柄あり） | ✗ 永続保持（成果物） |
| pos/neg + 銘柄なし | ✗ 残す（材料はある） |
| neutral + 銘柄あり | ✗ 残す |
| **neutral + 銘柄なし** | ✅ 90日経過で削除 |
| 未読（未分析） | ✗ 残す（分析待ち） |

理由: neutral かつ銘柄なしの記事は signals にならず、再利用価値がほぼ無い。signals は別テーブルで保持されるため、対象記事を消してもダイジェストや管理画面のシグナル表示には影響しない。

## 削除手順（実装方針）

1. 対象を `article_analyses`（sentiment='neutral' かつ stocks が空 `[]`）と `articles.fetched_at < 90日前` で特定
2. 対象の `article_analyses` 行と `articles` 行を削除（`signals` は触らない）
3. 安全のため、対象に `signals` 参照があるものは除外（本来該当しないが二重防御）

削除SQL（案）:
```sql
DELETE FROM article_analyses
WHERE article_id IN (
  SELECT aa.article_id FROM article_analyses aa
  JOIN articles a ON a.id = aa.article_id
  WHERE aa.sentiment = 'neutral' AND aa.stocks IN ('[]', '')
    AND a.fetched_at < datetime('now', '-90 days')
    AND aa.article_id NOT IN (SELECT article_id FROM signals)
);
DELETE FROM articles
WHERE id NOT IN (SELECT article_id FROM signals)
  AND id NOT IN (SELECT article_id FROM article_analyses)
  AND fetched_at < datetime('now', '-90 days')
  AND is_read = 1;
```
※ 正確な条件はPhase Bの実装時に確定（上記は方針メモ）。

## 実行方法

GitHub Actions schedule で**週次**実行（例: 日曜深夜）。collector/monitor/digest と同じ構成で `python -m maintenance.cleanup` 等の新エントリを追加する想定。

## 保持期間

**90日**（決定済み）。変更が必要なら settings テーブルに `retention_days` を追加して管理画面から変更可能にすることも検討（将来）。

## 未確定事項

- フラグ列（`article_analyses.disposable` 等）を立ててから削除する2段階方式にするか、条件で直接削除するか → 実装時に決定（透明性重視ならフラグ、簡潔さ重視なら直接）
- 削除件数のログ／通知（silent な削除を避ける）

## 関連

- 別課題: 未読バックログの肥大化（Geminiレート制限で分析が追いつかない）。本ポリシーとは別に対処が必要。
