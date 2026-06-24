# ADR-005: Renderコールドスタート対策は外部無料pingerで warm 保持する

## 状況

LINE Webhook 受信サーバーは Render Free Web Service で動いている（受信者管理＝
follow/unfollow 処理）。Render Free は **15分間アクセスが無いとスピンダウン**し、次の
リクエストで起動（コールドスタート）に数十秒かかる。この間に届いた LINE Webhook の
応答が遅れる可能性がある。

## 選択肢

- **外部の無料 uptime pinger（UptimeRobot / cron-job.org 等）から `/health` を定期 ping** して warm 保持
- GitHub Actions の cron で warm 保持
- Render 有料プラン（$7/月〜・スピンダウンなし）

## 決定

**外部無料 pinger から `/health` を数分間隔（5〜10分）で叩いて warm 保持する**。

## 理由

- **無料**で、本システムの「全経路を無料枠で回す」前提を崩さない（有料プランは不採用）。
- **`/health` は静的レスポンス（`{"status":"ok"}`）で D1 を一切触らない**ため、高頻度 ping でも
  D1 のリクエスト枠を消費しない。ping 先は必ず `/health` を使う（D1 を叩くページを叩かない）。
- GitHub Actions の cron は**遅延・スキップが常態**（ADR-002）で warm 保持の間隔保証に向かない上、
  Actions 実行枠も消費するため不採用。

## 設定手順（外部 pinger）

UptimeRobot（無料枠で5分間隔まで）を例にする。cron-job.org でも同等。

1. UptimeRobot に登録（無料）。
2. **Add New Monitor** → Monitor Type: `HTTP(s)`。
3. URL: `https://<RenderのサービスURL>/health`。
4. Monitoring Interval: 5分（無料枠の最短）。Render の 15分スピンダウンより十分短い。
5. 保存。以後 `/health` が5分ごとに叩かれ、サービスが warm に保たれる。

> cron-job.org を使う場合は同じ `/health` URL を 5〜10分間隔のジョブに登録するだけ。

## 補足

- スピンダウン自体は障害ではない。webhook は低頻度の follow/unfollow 処理が中心で、コールド
  スタートは初回応答を数十秒遅らせる程度（LINE 側もリトライする）。本対策は堅牢化であって必須ではない。
- 監視を兼ねるため、pinger のダウン検知通知を有効にしておくと Render 障害の早期把握にも使える。
