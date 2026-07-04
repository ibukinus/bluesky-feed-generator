# 0001: Jetstream 移行と ingest/serve プロセス分離

- 日付: 2026-07-04
- ステータス: 採用

## 文脈

フォーク元（MarshalX/bluesky-feed-generator）由来の構成には次の問題があった。

- Firehose（`com.atproto.sync.subscribeRepos`）を生で購読し、CAR ブロックのデコードや Like / Follow など無関係なレコードのデコードに CPU と帯域を浪費していた。
- Flask アプリの import 時に購読スレッドが起動する副作用があり、gunicorn のワーカー数を1に固定する制約・graceful shutdown の複雑さ・テストのしづらさの根源になっていた。
- 収集した投稿に保持期限がなく、SQLite が単調増加していた。

詳細は [2026-07-03 刷新方針レポート](../reports/2026-07-03-renewal-assessment.md) の Phase 2 を参照。

## 決定

1. **Firehose の購読をやめ、Bluesky 公式の Jetstream を購読する。** `wantedCollections=app.bsky.feed.post` で投稿のみを JSON で受信する。
2. **購読を `server/ingest.py` の独立プロセスに分離する**（compose の `ingest` サービス、`python -m server.ingest`）。Flask アプリ（`server/app.py`）は配信 API のみとし、import 時副作用を持たない。
3. **SQLite は WAL モード + busy_timeout で2プロセス共有する**（ingest が書き込み、app が読み取り）。
4. **収集投稿に保持期限を設ける**（`FEEDGEN_POST_RETENTION_DAYS`、デフォルト30日、0で無効）。ingest が1時間ごとに期限切れを削除する。

## 理由

- 投稿のみの JSON 受信により CAR デコードが不要になり、帯域・CPU が大幅に減る。
- プロセス分離により gunicorn のワーカー数制約が消え、シグナル処理が単純になり、配信 API を購読なしでテスト・起動できる。
- atproto SDK のモデル（`models.get_or_create`）は Jetstream の JSON レコードをそのまま解釈できるため、既存のフィルタリング処理（`data_filter.operations_callback`）とテストを変更せずに流用できる。

## 影響・トレードオフ

- **カーソルの単位が変わる。** Firehose のシーケンス番号から Jetstream の `time_us`（マイクロ秒タイムスタンプ）になるため、旧カーソルは引き継がない（`SubscriptionState` に `jetstream` キーで別管理）。移行直後は接続時点のライブテールから収集を開始する。
- **再接続時は5秒巻き戻す**ため重複イベントが発生し得る。insert 時の URI 存在チェックで排除する。
- Jetstream は bsky.network のマネージドインフラ（公開4インスタンス）に依存する。エンドポイントは `JETSTREAM_ENDPOINT` で変更でき、セルフホストにも切り替え可能。
- zstd 圧縮は使わない（投稿のみの購読では帯域が十分小さく、依存を増やす価値がない）。
