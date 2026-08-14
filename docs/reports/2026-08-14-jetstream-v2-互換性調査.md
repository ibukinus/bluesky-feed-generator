# Jetstream v2 互換性調査（2026-08-14 時点）

## 結論

**現行実装はそのまま動く。緊急の対応は不要。**

- 本フィードが購読している v1 ワイヤ（`wss://jetstream2.us-east.bsky.network/subscribe`）は「凍結（frozen）」扱いになったが、公式ドキュメントは「既存の consumer が動き続けるように」旧ホストでも v2 ホストでも提供し続けると明記している。停止予定日の告知は見つからなかった。
- ただし **v2 ワイヤはプロトコル互換ではない**。エンドポイントパス・クエリパラメータ名・イベント JSON の形・カーソルの単位がすべて変わっているため、接続先を v2 のパスに向けるだけでは動かない。
- 一方で **レコード本体（`record`）の JSON は完全に同一**で、`atproto` SDK の `models.get_or_create` はそのまま通る。移行コストは `server/ingest.py` 内の URL 組み立て・イベント変換・カーソル管理に閉じる。

## 現行実装（調査時点）

| 箇所 | 内容 |
| --- | --- |
| `server/config.py:31` | `JETSTREAM_ENDPOINT` 既定値 `wss://jetstream2.us-east.bsky.network/subscribe` |
| `server/ingest.py` `build_url` | `wantedCollections=app.bsky.feed.post`、`cursor` は保存済み `time_us` から 5 秒（`RECONNECT_REWIND_US`）巻き戻した値 |
| `server/ingest.py` `ops_from_event` | `event['kind'] == 'commit'` と `event['commit']['collection']` で判定し、`commit.record/cid/rkey` と `event['did']` から ops を組む |
| `server/ingest.py` カーソル | `SubscriptionState(service='jetstream')` に `time_us` を保存 |
| `server/ingest.py` `should_reset_cursor` | ハンドシェイクの HTTP 400 のときだけカーソルを破棄 |

## Jetstream v2 の概要

`bluesky-social/jetstream` は「full-network archive, replay, and streaming service for atproto」として書き直され、GitHub のリリースは v0.1.0 / v0.2.0（いずれも 2026-08-13）。

- **公開 v2 エンドポイント**: `wss://jetstream.us-east.bsky.network` / `wss://jetstream.us-west.bsky.network`
- **ライブテールのパス**: `/xrpc/network.bsky.jetstream.subscribeEvents`（Lexicon `network.bsky.jetstream.subscribeEvents`、subprotocol `xrpc.v1.json`）
- **ライブテールは認証不要・従量課金なし**。アーカイブ／スナップショットの HTTP エンドポイント（`planSnapshot` / `getSegment` / `getBlock`）のみ API キーが必要で、バイト単位で従量計測され、超過時は `429`。
- **新機能**: `kinds` フィルタ、リプレイ（ライブソケットの lookback は Bluesky ホスト実装で 36 時間）、スナップショットによる過去データのバックフィル、`zstdDictionary` による辞書 zstd 圧縮（opt-in）、`maxMessageSizeBytes`。
- **公式 SDK は Go と TypeScript のみ**。Python クライアントは無いので、移行しても `websockets` 直叩きのままになる。

## 接続の実測結果

`websockets` 15.0.1 で各組み合わせに実接続して確認した。

| 接続先 | 結果 |
| --- | --- |
| `jetstream2.us-east` + `/subscribe`（現行設定） | **OK**。イベント形状は従来どおり |
| `jetstream.us-east`（v2 ホスト）+ `/subscribe` | **OK**。v1 形状のまま、`cursor`（seq）フィールドが 1 つ増えるだけ。既存コードは無視するので影響なし |
| `jetstream.us-east` + `/xrpc/network.bsky.jetstream.subscribeEvents` | **OK**（v2 形状） |
| `jetstream2.us-east` + `/xrpc/network.bsky.jetstream.subscribeEvents` | **HTTP 404**。旧ホストは v2 ワイヤ非対応 |

## v1 → v2 の差分と、本実装で必要になる変更

### 1. エンドポイントとクエリパラメータ

| | v1 | v2 |
| --- | --- | --- |
| パス | `/subscribe` | `/xrpc/network.bsky.jetstream.subscribeEvents` |
| コレクション指定 | `wantedCollections`（最大 100） | `collections`（最大 100、`app.bsky.feed.*` 形式のワイルドカード可） |
| DID 指定 | `wantedDids` | `dids`（最大 10,000） |
| 種別フィルタ | なし（identity/account は必ず届く） | `kinds`（`commit` / `identity` / `account` / `sync`） |
| 圧縮 | `compress=true` | `zstdDictionary=<辞書ID>`（`getZstdDictionary` で取得） |
| その他 | — | `maxMessageSizeBytes` |

`JETSTREAM_ENDPOINT` はパスまで含めた値なので、移行時は `server/config.py:31` / `.env.example` / `README.md` の既定値を差し替える。`kinds=commit` を付ければ現行より受信量が減る（v1 では種別を絞れなかった）。

### 2. イベント JSON の形

```jsonc
// v1
{"did": "...", "time_us": 1786675904507992, "kind": "commit",
 "commit": {"rev": "...", "operation": "create", "collection": "app.bsky.feed.post",
            "rkey": "...", "record": {...}, "cid": "..."}}

// v2（$type: "message" の封筒に入り、commit のフィールドが payload 直下へ平坦化される）
{"$type": "message",
 "payload": {"$type": "network.bsky.jetstream.subscribeEvents#commit",
             "seq": 24708718469, "time": "2026-08-14T02:51:46.096759Z",
             "did": "...", "operation": "create", "collection": "app.bsky.feed.post",
             "rkey": "...", "rev": "...", "cid": "...", "record": {...}}}
```

- `ops_from_event` は書き換えが必要（種別判定は `payload['$type']` のフラグメント、commit 各値は `payload` 直下）。
- `delete` は `record` / `cid` を持たず `did` / `collection` / `rkey` は持つので、URI 組み立てロジックは変更不要。
- **`record` の中身は v1 と完全に同一**。blob 参照も `{"$link": ...}` のまま。v2 ワイヤから取得した投稿レコード 201 件すべてが `models.get_or_create(..., strict=False)` + `models.is_record_type(..., AppBskyFeedPost)` を通過した（失敗 0 件）。したがって `server/data_filter.py` は無変更でよい。
- テスト側は `tests/test_ingest.py` の `make_create_event` など、イベント形状のフィクスチャを v2 形状に作り直す必要がある。

### 3. カーソル

- 単位が `time_us`（マイクロ秒）から `seq`（全ネットワーク単調増加の整数、現在 2.47×10^10 前後）に変わる。
- **`cursor` は inclusive**（指定した seq のイベント自体が再送される）。したがって v2 では `RECONNECT_REWIND_US` のような明示的な巻き戻しは不要で、最後に処理した seq をそのまま渡せばよい。seq に対して 5,000,000 を引くと数時間分の巻き戻しになってしまうので、移行時は必ず外すこと。
- **1×10^15 以上の値は unix マイクロ秒として解釈され、最寄りの seq に変換される**。実測で、保存済みの `time_us` をそのまま v2 の `cursor` に渡して 30 秒前から再生できた。**既存カーソルを捨てずに移行できる**。
  - ただし移行後に保存する値は seq（10^10 台）になるため、同じカラムに 2 つの単位が混在する。ADR 0001 で Firehose → Jetstream のときにやったのと同様に、`SubscriptionState` のキーを分ける（例: `service='jetstream2'`）のが安全。

### 4. エラーと `#info` フレーム

| 状況 | 挙動 | 現行コードへの影響 |
| --- | --- | --- |
| retention floor より古い **seq** カーソル | ハンドシェイクで **HTTP 400**（`CursorTooOld`）。実測で `cursor=1` は 400 | `should_reset_cursor`（400 判定）はそのまま有効 |
| retention floor より古い **タイムスタンプ** カーソル | 400 にはならず floor まで繰り上げられ、`#info` `OutdatedCursor` フレームが 1 件届く | `#info` は `seq` を持たないので、カーソル保存処理でスキップする必要がある |
| 極端に読み取りが遅い | `ConsumerTooSlow` で切断 | 再接続ループで復帰する |
| 未知の zstd 辞書 ID | HTTP 400（`UnknownZstdDictionary`） | 圧縮を使わない限り無関係 |

`#info` の実測例:

```json
{"$type": "message", "payload": {"$type": "network.bsky.jetstream.subscribeEvents#info",
 "name": "OutdatedCursor",
 "message": "requested timestamp cursor below retention floor; starting at seq 24647190112"}}
```

### 5. 圧縮方針

ADR 0001 の「zstd 圧縮は使わない」判断は v2 でも維持でよい。v2 の辞書 zstd は `zstdDictionary` パラメータでの opt-in であり、既定は非圧縮のままだった（実測でもテキストフレームで届いた）。

## v1 側で観測した変化

現行コードのコメントは「Jetstream は不正なカーソルをハンドシェイクの 400 Bad Request で拒否する」としているが、**旧ホストに 2024-01-01 のカーソルを渡しても 400 にならず、約 36 時間前まで繰り上げられて再生が始まった**（`1786546382` ≒ 測定時刻の 35.99 時間前）。実害はない（単に巻き戻し再生になるだけ）が、`should_reset_cursor` が発火する条件は現状ほぼ無いと考えてよい。

## 推奨

1. **短期（今回は不要）**: 何もしなくてよい。強いて言えば `JETSTREAM_ENDPOINT` を `wss://jetstream.us-east.bsky.network/subscribe`（v2 ホストが提供する v1 ワイヤ）に向けておくと、将来 `jetstream1/2` 系ホストだけが停止した場合の影響を避けられる。コード変更は不要。
2. **中期**: v2 ワイヤへの移行は `build_url` / `ops_from_event` / カーソル管理の局所改修で済む。得られるもの:
   - `kinds=commit` による受信量削減
   - リプレイ（36 時間）で ingest 停止中の取りこぼしを取り戻せる
   - 将来的にスナップショット（API キー・従量課金）で過去投稿のバックフィルが可能
3. 移行する場合は ADR（0002）を追加し、`SubscriptionState` のキーを分けて旧カーソルと混ざらないようにする。

## 参照

- [Jetstream ドキュメント（bsky.network）](https://bsky.network/docs/jetstream)
- [Network Replay](https://bsky.network/docs/jetstream-replay)
- [Jetstream SDK](https://bsky.network/docs/jetstream-sdk)
- [Lexicon: network.bsky.jetstream.subscribeEvents](https://github.com/bluesky-social/jetstream/blob/main/lexicons/network/bsky/jetstream/subscribeEvents.json)
- [bluesky-social/jetstream リリース](https://github.com/bluesky-social/jetstream/releases)
- [Go パッケージドキュメント](https://pkg.go.dev/github.com/bluesky-social/jetstream)
