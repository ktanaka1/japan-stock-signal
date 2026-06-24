# WIP: Yahoo 出来高ランキング合流（母集団の拡張・小改修）

> 状態: 設計中（未実装）。実装＋テスト完了でこのファイルを削除し、要点を SPEC.md へ統合する。
> 関連: メモリ [[news-source-evaluation]]、テクニカル版 `technical/agent.py`。

## 目的・背景（前提の補正あり）

当初案は「Yahooの出来高**増加率**ランキングを直接使う」だったが、検証の結果:

- Yahooに在るのは **`kind=volume`＝生の出来高ランキング**（200・実証ルートで取得可）。
  **「出来高増加率(相対出来高)」のランキングは存在しない**（候補kindは全て400）。
- 相対出来高(surge=当日出来高/直近平均)は **既に `technical/agent.py` が自前で計算済み**
  （`monitor.quotes.get_daily_metrics` の `surge`）。＝狙いの指標は実装済み。

よって本WIPは「新指標の導入」ではなく、**テクニカル版スキャナの母集団を広げる小改修**に限定する。
現状の母集団は「値上がり率top30」のみ。+5%未満でも出来高急増した銘柄を取りこぼしている可能性を、
出来高ランキングの合流で埋める。

## 統合方式

`feedback/ranking.py` の `fetch_ranking("volume", top_n)` は既に "volume" ブロック対応済み。

`technical/agent.py` `_scan()` を以下に変更:
1. `up` ランキング（既存）に加え、設定ONなら `volume` ランキングも取得
2. **コードで重複排除**してマージした母集団に対し、既存フィルタ（`in_universe` で大型/ETF除外、
   `change_pct≥min_change`、`surge≥min_surge`、本体配信済み除外）をそのまま適用
3. 生の出来高ランキングは大型株偏重だが、`in_universe`＋`surge` フィルタで中小型の急増のみ残る

## 設定（settings）

- `tech_include_volume_ranking`（既定 "false" → 検証後ON）
- `tech_top_n` は既存を流用（up/volume 双方に適用）

## （任意）捕捉率FBの volume 計測

FBは現在 `ranking_type="up"` のみ記録。`volume` ランキングに対する捕捉も別 `coverage_runs` 行
として記録すれば、出来高主役の動意の捕捉率も時系列で見える（`feedback/agent.py` を ranking_type
ループ化）。効果検証が目的なら有用だが、本WIPの必須ではない（Phase 2扱い）。

## テスト（FORCE_LOCAL_DB=1）

- `fetch_ranking("volume")` のパース（既存の `_parse_state`/`_to_item` のvolumeブロック）
- `_scan` が up＋volume をコード重複排除でマージし、フィルタ後の picks が正しいこと
- 設定OFF時は従来どおり up のみ（後方互換）

## リスク・留意

- 生の出来高は大型株が上位を独占 → フィルタ前提。効果は限定的（小〜中）
- Yahoo連続アクセスは適度なインターバルで（既存 ranking.py の作法に合わせる）

## 段階

- **Phase 1**: `tech_include_volume_ranking` で母集団に volume を合流（小改修）。← まずここ
- **Phase 2（任意）**: 捕捉率FBの ranking_type ループ化で volume 捕捉率も計測。
