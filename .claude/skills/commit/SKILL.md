---
name: commit
description: プロジェクトルールに従ったgitコミットを実行する
disable-model-invocation: true
---

# /commit - プロジェクト専用コミットスキル

## ルール

1. **修正内容ごとに分けてコミット**: 1コミット = 1論理的変更
2. **Conventional Commits形式も可だが、既存履歴は日本語の説明的メッセージが主**。リポジトリの既存スタイルに合わせる
3. **Co-Authored-By を必ず付与**:
   ```
   Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
   ```
4. **HEREDOCでメッセージ作成**（フォーマット維持のため）

## 手順

### 1. 変更状況の確認
```bash
git status
git diff --stat
git log --oneline -5
```

### 2. 変更のグループ化
論理的単位でグループ化する（機能追加 / 修正 / リファクタ / ドキュメント / 雑務）。

### 3. グループごとにコミット
```bash
git add <関連ファイル>
git commit -m "$(cat <<'EOF'
<簡潔な説明（日本語可）>

<詳細（必要に応じて）>

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

## 注意事項（このプロジェクト固有）

- `git add -A` / `git add .` は避け、ファイルを明示指定する
- `.env` や認証情報を含むファイルはコミットしない
- **push はユーザーの明示指示がある場合のみ**。リモートは SSH（HTTPSではworkflowファイルをpushできない）
- workflowファイル（`.github/workflows/`）を含むpushはSSH経由であることを確認する
