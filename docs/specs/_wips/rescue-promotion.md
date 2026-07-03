# WIP: 逆引き昇格（rescue）— signaled層の交差救済

> 状態: **Phase 1（signaled層の交差救済）実装＆テスト完了（2026-07-02）・要点はSPEC.md
> 「逆引き昇格」へ反映済**。本ファイルは Phase 2（not_collected層の能動コード逆引き）の
> 設計メモとして残置する。
> 関連: メモリ [[coverage-fb-comparison-pending]]。捕捉率FBの `signaled_not_delivered` 回収策。

## 目的 — 3段構えアーキテクチャの第3レーン

1. **本体（朝7:30配信）**: impact4〜5の「強力なニュース起因カタリスト」を先制配信
2. **テクニカル版（朝8:00配信）**: ニュース無しの「需給・モメンタム」を捕捉
3. **逆引き昇格（夕方・本機能）**: 「impact3（AIには凡庸に見えた）だが市場が激しく反応した
   （値上がり率ランキング上位に入った）」シグナルを事後的に拾い上げ、**翌朝の配信へ昇格**する

実測根拠（2026-07-01）: `signaled_not_delivered` 4銘柄は全件が impact=3 の閾値落ちで、
うち ReYuu Japan は +30.3%。ニュースは収集・分析済みなので**追加コストゼロで救済できる**。
一方 impact=3 は約97件/日あり、閾値を一律3に下げると配信量が3倍化する。
「impact3 × ランキング上位」の交差条件なら1日0〜4件の追加で済む。

## 設計（Phase 1）

### トリガと配置

- `feedback/agent.py run()`（snapshot・D夕方 16:30 cron）が classify 後に
  `feedback/rescue.py promote_rescues()` を呼ぶ。
- **feedbackパッケージは原則「読み取り専用の計測器」だが、rescue だけは例外**として
  `signals.promoted_at / promote_reason` を書く（notified_at 等の配信状態には触れない）。
  この例外は本WIPと SPEC.md に明記する。

### 昇格条件（1銘柄1シグナル）

対象 = detail のうち `in_universe=True` かつ `status=signaled_not_delivered` の銘柄。
その銘柄コードを含む窓内シグナルで:

- `notified_at IS NULL AND promoted_at IS NULL`（未配信・未昇格 → 冪等）
- `rescue_min_impact(既定3) <= impact < min_impact_for_notify`
  （閾値以上は翌朝どうせ配信されるので昇格不要。min_impact=0 なら全件配信＝昇格は自然に空）
- 複数候補は **impact最大→id最大（最新）** の1件だけ昇格（LINEノイズ抑制）

`promote_reason` に根拠を保存: `"{ranking_date} 値上がり率#{rank} {pct:+.1f}%"`

### 配信（monitor/digest.py）

- eligible 条件を `impact >= min_impact **OR promoted_at あり**` に拡張
- 昇格シグナルは本文に `🔁 逆引き昇格（{promote_reason}）` 行を付けて通常フローで配信
- exclude_large_cap は昇格分にも適用（ランキング母集団が中小型なので実質無関係）

### 捕捉率FBの整合 — **rescueは capture_rate に入れない（自己言及の防止）**

ランキングを見て昇格させたものをランキング捕捉として数えると指標が自己成就する。
- matcher に `STATUS_RESCUED = "delivered_rescue"` を追加
  （notified_at と promoted_at が両方あるシグナル由来。優先度: delivered > tech > rescued > signaled > neutral > not_collected）
- `capture_rate = (delivered + delivered_technical) / universe` は**不変**
- `coverage_runs.rescued` 列（migration 015）に別枠計上し、管理画面に 🔁 バッジ表示

### スキーマ（migration 015・追記のみ）

```sql
ALTER TABLE signals ADD COLUMN promoted_at TEXT;
ALTER TABLE signals ADD COLUMN promote_reason TEXT;
ALTER TABLE coverage_runs ADD COLUMN rescued INTEGER NOT NULL DEFAULT 0;
```

### settings（管理画面「配信フィルタ・収集ソース」カード）

- `rescue_enabled`（既定 "true"。交差救済のON/OFF）
- `rescue_min_impact`（既定 "3"。これ未満のimpactは市場反応があってもノイズとして昇格しない）

### タイムライン例

```
D 15:00   窓クローズ（signals.created_at ∈ [D-1 15:00, D 15:00)）
D 16:30〜 feedback snapshot: ランキング照合 → impact3×ランキング上位を promoted_at 付与（暫定行 finalized=0）
D+1 7:30  digest: impact>=4 ＋ 🔁昇格分 を配信（notified_at 付与）
D+1 7:3x  feedback --finalize: D行を確定。昇格分は delivered_rescue として rescued に別枠計上
```

### テスト（FORCE_LOCAL_DB=1・tests/test_rescue.py）

- snapshot昇格: impact3×signaled銘柄 → promoted_at/reason 付与。impact4（どうせ配信）・
  impact2（floor未満）・配信済み・母集団外・rescue_enabled=false は昇格しない
- 1銘柄1件: 同一銘柄に impact3 が2本 → 最新1本のみ
- digest配信: 昇格シグナルが min_impact=4 でも配信され 🔁 行が付く
- finalize: 配信後に status=delivered_rescue・rescued=1・capture_rate には不算入
- 冪等: snapshot再実行で二重昇格しない

## Phase 2（未実装・設計メモ）: not_collected層の能動コード逆引き

支配要因 `not_collected`（20/30件規模）のうち材料実在率は14〜17%（2026-06-30プローブ）。
- 未捕捉ランキング上位の銘柄コードで **TDnet/Yahoo個別ニュースを能動取得** →
  取れた材料を articles に投入 → 既存LLM精査 → 翌朝候補化
- プローブ実装（scripts/probe_hidden_materials.py の Yahoo quote/news 正規表現）は
  スクレイピング依存で脆い。本実装では TDnet コード検索 or 安定APIを優先検討
- 残り大半（材料なし＝純モメンタム）はテクニカル版の領域（出来高源で捕捉中）
