# Jetstream us-east 停止による収集断（2026-08-23 時点）

## 結論

**購読先 `jetstream.us-east.bsky.network` のバックエンドが全滅しており、ホスト側の障害である。本実装に問題はない。** 他の Jetstream ホスト（`us-west` および レガシーの `jetstream1/2`）はすべて正常なので、`JETSTREAM_ENDPOINT` を切り替えれば復旧する。

- 収集は **2026-08-20 19:59 UTC（08-21 04:59 JST）** から止まっている（調査時点で 55.6 時間）。
- Jetstream のライブ再生 lookback は 36 時間のため、**2026-08-20 19:59 〜 08-21 14:19 UTC の約 18.3 時間分は再生不能**。この区間の投稿はライブソケットからは回収できない。
- Bluesky 本体（AppView・Relay）は正常。公式ステータスページ（status.bsky.app）にインシデントの掲載はなく、**未アナウンスの障害**。

## 症状

ingest が以下を 5 秒ごとに繰り返す。カーソルは進まない。

```
[ERROR] Jetstream 接続に失敗しました: did not receive a valid HTTP response（カーソルは実時刻より 200163秒 前）。5秒後に再接続します。
```

`did not receive a valid HTTP response` は `websockets` の `InvalidMessage`。WebSocket ハンドシェイクに対して HTTP 101 以外が返ったときに出る。

## 各ホストの実測（2026-08-23 03:35〜03:40 UTC）

HTTP ハンドシェイク（`/subscribe`）と、`websockets` での実接続を両方試した。

| ホスト | HTTP | 実接続 | 配信遅延の中央値 |
| --- | --- | --- | --- |
| `jetstream.us-east`（**現在の購読先**） | **503** | **NG** `InvalidMessage` | — |
| `jetstream.us-west` | 426 | OK 200 件受信 | +0.9 秒 |
| `jetstream1.us-east`（レガシー） | 400 | OK 200 件受信 | +1.1 秒 |
| `jetstream2.us-east`（レガシー） | 400 | OK 200 件受信 | +1.0 秒 |
| `jetstream1.us-west`（レガシー） | 400 | OK 200 件受信 | +0.9 秒 |
| `jetstream2.us-west`（レガシー） | 400 | OK 200 件受信 | +0.9 秒 |

- 426 / 400 は curl の擬似ハンドシェイクに対する正常な拒否応答であり、健全性を示す。
- `time_us` の実時刻からの遅れはどのホストも +0.1 秒。2026-08-14 のような「時刻は現在なのに中身が古い」配信遅延は起きていない（`docs/reports/2026-08-14-jetstream2-配信遅延.md` 参照）。

us-east が返す 503 の本文は次のとおりで、ロードバランサが振り先を 1 台も持っていない状態を示す。

```
HTTP/2 503
content-type: text/html

<html><body><h1>503 Service Unavailable</h1>
No server is available to handle this request.
</body></html>
```

- ルートパス `/`、v1 ワイヤ `/subscribe`、v2 ワイヤ `/xrpc/network.bsky.jetstream.subscribeEvents` のいずれも 503。パス単位ではなくホスト単位の停止。
- 3 回連続で 503。一過性ではない。
- 参考: AppView（`app.bsky.actor.getProfile`）200、Relay（`com.atproto.sync.listRepos`）200。Bluesky 本体は正常。

## 再生可能範囲の実測

`us-west` に対し、カーソルを過去に指定して最初に届いたイベントの `time_us` を測った。

| 要求したカーソル | 実際の再生開始位置 |
| --- | --- |
| 200180 秒前（本番のカーソル相当） | **134283 秒前（37.3 時間前）にクランプ** |
| 24 時間前 | 要求どおり |
| 8 時間前 | 要求どおり |
| 1 時間前 | 1.86 時間前（ブロック境界で少し余分に戻る） |

公式ドキュメントの「ライブソケットの lookback は 36 時間」と整合する（`scripts/rewind_cursor.py` の `REPLAY_WINDOW_HOURS = 36` も同じ前提）。保存済みカーソルが lookback 外でも接続は成功し、再生可能な最古から始まる。

## 動作の評価

- `should_reset_cursor` は HTTP 400 のときだけカーソルを破棄する。今回は 503 でカーソルが保持されており、意図どおり。
- 停止から 55.6 時間、5 秒間隔で再接続し続けた。無限リトライ自体は正しいが、**ホストが復旧しない限り自力では回復しない**。カーソルの遅れは ERROR ログに出るものの、閾値を超えたときの通知・自動フェイルオーバーは無い。

## 復旧手順（案）

`JETSTREAM_ENDPOINT` を健全なホストへ向ける。lookback 外の 18.3 時間は取り戻せないため、カーソルの巻き戻しは 36 時間以内に収める意味しかない（切り替え後の初回接続で自動的に再生可能な最古へクランプされる）。

```shell
docker compose stop ingest
# .env に JETSTREAM_ENDPOINT=wss://jetstream.us-west.bsky.network/subscribe を設定
docker compose up -d ingest
```

`ENDPOINT_CHANGE_REWIND_US`（8 時間）による自動巻き戻しが働くが、現在のカーソルはそれより遥かに古いため実質的な影響はない。

## 残課題

- 回収不能な 18.3 時間分の穴。v2 のスナップショット / アーカイブ API（`planSnapshot` / `getSegment` / `getBlock`）なら理論上バックフィルできるが、API キーと従量課金が必要で、現行実装に取り込み口は無い。
- カーソルの遅れが一定を超えたときに別ホストへ自動フェイルオーバーする仕組み。今回の 55.6 時間の断は、それがあれば数分で済んだ。
