# アーキテクチャ

Bluesky の Jetstream から「アイドルマスター シャイニーカラーズ」関連の日本語投稿を収集し、AT Protocol のカスタムフィードとして配信する Feed Generator。MarshalX 氏の [bluesky-feed-generator](https://github.com/MarshalX/bluesky-feed-generator) をベースに、Sudachi 形態素解析による2段階キーワードマッチングを組み込み、Jetstream 移行とプロセス分離（[ADR-0001](adr/0001-jetstream-移行とプロセス分離.md)）を行っている。

セットアップ手順・環境変数・エンドポイント一覧は [ルートの README](../README.md) を参照。

## データフロー

収集（ingest）と配信（app）は独立したプロセスで、SQLite（WAL モード）を介して連携する。

```
Bluesky Jetstream (JETSTREAM_ENDPOINT / JETSTREAM_FALLBACK_ENDPOINTS)
      │  (WebSocket 購読: wantedCollections=app.bsky.feed.post, JSON)
      ▼
server/ingest.py  ← 独立プロセス（python -m server.ingest / compose の ingest サービス）
      │  commit イベントを atproto モデルに変換し create/delete を集約
      ▼
data_filter.operations_callback()
      │  1. アーカイブ投稿/リプライの除外（環境変数でオプトイン）
      │  2. EXCLUDED_DID_LIST による除外
      │  3. PRIORITY_DID_LIST は無条件採用
      │  4. langs に 'ja' を含まない投稿を除外
      │  5. 本文 + 画像 ALT テキストに対しキーワードマッチング
      │  6. 登録済み URI はスキップ（再接続巻き戻しの重複排除）
      ▼
SQLite (WAL モード, peewee ORM: Post / SubscriptionState / IngestMeta)
      ▲
      │  indexed_at DESC, cid DESC + カーソルページネーション
algos/shiny_colors.handler()
      ▲
Flask (app.py) ← 配信専用（import 時副作用なし）
  ├── /.well-known/did.json                        … did:web ドキュメント
  ├── /xrpc/app.bsky.feed.describeFeedGenerator     … フィード情報
  └── /xrpc/app.bsky.feed.getFeedSkeleton           … フィード本体
```

- Jetstream のカーソル（`time_us`）は約5秒ごとに `SubscriptionState`（キー: `jetstream`）へ永続化され、再接続時は5秒巻き戻して再開する（取りこぼし防止。重複は insert 時に排除）。
- **購読接続の堅牢化**（詳細は [2026-08-14 の配信遅延レポート](reports/2026-08-14-jetstream2-配信遅延.md)）:
  - `ws.recv(timeout=RECV_TIMEOUT_SECONDS)`（60秒）で受信の停滞を検知して繋ぎ直す。投稿ストリームは常時流れているため、無音は接続が死んだ兆候。
  - `max_queue`（1024）と `ping_timeout`（60秒）を既定値から引き上げる。既定の `max_queue=16` は 60件/秒 のストリームでは0.3秒分しかなく、一瞬の処理遅延で受信スレッドが停止して Pong を返せなくなり、自分で keepalive タイムアウトを起こす。
  - `StalenessMonitor` が「イベントの `time_us` と投稿の `createdAt` の差」の中央値を5分ごとに評価し、15分以上ならホストの配信遅延として WARNING を出す。ホストが遅れても `time_us` は現在時刻のまま中身だけが古くなるため、カーソルの遅れでは検知できない。
  - 切断時のログにはカーソルの実時刻からの遅れを併記する。
- **購読先の自動フェイルオーバー**（詳細は [2026-08-23 の us-east 停止レポート](reports/2026-08-23-jetstream-us-east停止.md)）: `EndpointRotator` が `config.JETSTREAM_ENDPOINTS`（`JETSTREAM_ENDPOINT` + `JETSTREAM_FALLBACK_ENDPOINTS`）を順に切り替える。候補を使い切ったら先頭へ戻り、全ホストが落ちていても再接続を続ける。切り替えの契機は2つ。
  - **接続失敗が続いたとき**: 失敗が `FAILOVER_AFTER_FAILURES`（3回）連続したら次の候補へ移る。ハンドシェイクは通るのにイベントを流さないホストも捕まえるため、失敗の数え直しは「接続できた時点」ではなく「イベントが1件届いた時点」で行い、受信タイムアウトも失敗として数える。
  - **配信が遅れているとき**: 遅延の中央値が `STALENESS_FAILOVER_SECONDS`（30分）を超えたら次の候補へ移る。警告だけ出す15分としきい値を分け、一時的な遅れで切り替えないようにしている。
  - **切り替え時は理由によらずカーソルを8時間巻き戻す**（手動でのホスト変更と同じ扱い）。接続できずに切り替える場合も「落ちる直前まで遅れて配信していた」可能性は排除できず（遅延判定は5分間隔なので遅れ始めてすぐ落ちれば気づけない）、読み飛ばしより再生のコストを取る。長時間繋がらなかった場合は保存済みカーソルの方が古いため、`cursor_for_endpoint` の min により前進はしない。
  - **自動で移った先は再起動後も引き継ぐ**（`initial_endpoint`）。第一候補へ戻すと、落ちたままのホストへ毎回繋ぎに行き、切り替えのたびに8時間分を再生し直すことになるため。人が `JETSTREAM_ENDPOINT` を変えた場合と区別できるよう、直近の起動時の第一候補を `IngestMeta`（キー: `jetstream_primary`）に記録し、それが変わっていれば記録を無視して第一候補から始める。
- **購読ホストを変えたら起動時にカーソルを8時間巻き戻す。** 直近の購読先は `IngestMeta`（キー: `jetstream_endpoint`）に記録し、`JETSTREAM_ENDPOINT` と食い違ったら巻き戻す。切り替え前のホストが遅れていた場合、カーソルは実時刻付近を指していても投稿は未収集で、そのまま繋ぐと恒久的に読み飛ばすため。それより前まで戻すには `scripts/rewind_cursor.py --hours N` を使う。
- 削除イベントを受けると DB からも該当投稿を削除し同期を維持する。
- 保持期限（`FEEDGEN_POST_RETENTION_DAYS`、デフォルト30日）を過ぎた投稿は ingest が1時間ごとに削除する。
- フィードのカーソルは `{ミリ秒タイムスタンプ}::{cid}` 形式。末尾に達すると `eof` を返す。不正なカーソルは 400 を返す。
- `getFeedSkeleton` の `limit` は AT Protocol の仕様に合わせてサーバー側で 1〜100 にクランプされる。
- SIGINT / SIGTERM を受けると Firehose 購読スレッドに停止を通知してから終了する（`docker compose down` の graceful shutdown に対応）。

## モジュール構成

| ファイル | 役割 | 備考 |
|---|---|---|
| `server/app.py` | Flask アプリ・XRPC エンドポイント | 配信専用。import 時副作用なし |
| `server/ingest.py` | Jetstream 購読・再接続・カーソル管理・配信遅延監視・保持期限削除 | 独立プロセス。切断時は5秒後に再接続 |
| `server/data_filter.py` | 投稿フィルタリング・DB 書き込み | 本文と画像 ALT の両方を判定。URI 重複は排除 |
| `server/matcher.py` | キーワードマッチングエンジン | 起動時に keyword.toml を読み正規表現をコンパイル |
| `server/database.py` | peewee モデル（Post, SubscriptionState, IngestMeta） | import 時にテーブル作成 |
| `server/config.py` | 環境変数の読み込み・検証 | HOSTNAME / SHINY_URI 未設定なら起動時に例外 |
| `server/algos/shiny_colors.py` | フィードアルゴリズム（時系列 + カーソル） | |
| `server/auth.py` | JWT 検証（未使用の参考実装） | フィードがユーザー非依存のため |
| `publish_feed.py` | フィードレコードの公開/更新スクリプト | |
| `conftest.py` | テスト用環境変数（in-memory SQLite 等） | |

## キーワードマッチング仕様

`server/matcher.py` + `keyword.toml` + Sudachi（`sudachi.json` / `user.csv`）で構成。

1. 投稿本文を SudachiPy でトークン化し、各トークンの `normalized_form()`（正規化形）を判定に使う。
2. **rank1**: 1トークンでも完全一致すれば即採用。作品名・ユニット名・キャラクターのフルネーム・楽曲名など固有性の高い語。
3. **rank1_surface**: トークンの**表面形（書かれたまま）**との完全一致でのみ採用する語。正規化形マッチでは誤検出する語（例: 英単語 seeds の正規化形が「シーズ」になる）を置く（[ADR-0002](adr/0002-表面形マッチ層の導入.md)）。
4. **rank2**: 「櫻木」「灯織」など姓のみ・名のみの語。**異なる2語以上**がマッチした場合のみ採用（「田中」「鈴木」単独のような誤検出を防ぐ）。
5. 正規表現は起動時に `^word1$|^word2$|...` 形式で1本にコンパイル（IGNORECASE）。各語は `re.escape` でエスケープされ、メタ文字を含む語も文字どおりに解釈される。
6. `user.csv` は Sudachi ユーザー辞書のソース。表記ゆれ（例: `shiny colors` → `シャイニーカラーズ`）を正規化形に集約し、`283プロダクション` などの分割防止も担う。
7. キーワードの追加は `scripts/check_keyword.py` で行う。語が1トークンで認識されるかの診断、keyword.toml への追記（`--add`）、認識されない語の user.csv への追記と辞書再ビルド・再検証（`--fix`）までを自動化する。`.claude/skills/add-keyword` はこのスクリプトを使って rank 判定からテストまでの一連の作業を行う Claude Code スキル。

## デプロイ構成

- **Dockerfile**: マルチステージビルド。builder ステージで uv sync（frozen）+ `scripts/build_user_dict.py`（Sudachi ユーザー辞書ビルドと `sudachi.json` の配置）を実行し、runner は slim イメージに site-packages をコピーして gunicorn で `0.0.0.0:8000` を公開する。
- **compose.yml**: GHCR のイメージ（`ghcr.io/ibukinus/bluesky-feed-generator:latest`）を使用。`./db` を `/app/db` にマウント、`.env` を読み込み、ポート 8000 を公開。
- **CI/CD（GitHub Actions）**: PR で `ci.yml` がテストを実行。`shiny` への push で `deploy.yml` が テスト → イメージビルド → GHCR への push → SSH 経由で VM の `docker compose pull && up -d` を実行する。必要なシークレットは README を参照。
- **イメージは linux/arm64 のみ。** デプロイ先の OCI VM が ARM のため、ARM ランナー（`ubuntu-24.04-arm`）でネイティブビルドする（QEMU エミュレーションはビルドが遅くなるため使わない）。
- ローカル開発は `uv sync` + `flask --debug run`（`.flaskenv` で port 8000）。

## 設計上の注意点（恒久的な制約）

- **Sudachi ユーザー辞書は `scripts/build_user_dict.py` で sudachipy の resources ディレクトリに組み込む。** Docker ビルドと pytest（conftest.py）は自動実行するため、テストは本番と同じ辞書で走る。素の `flask run` で辞書を使うには事前に同スクリプトを実行する。
- **SQLite の保存先はデフォルトで `feed.db`（コンテナ内 `/app/feed.db`）。** これはボリュームマウント（`/app/db`）の外かつコンテナごとに別ファイルになるため、compose.yml が両サービスに `FEEDGEN_SQLITE_LOCATION=db/feed.db` をデフォルト設定している（`.env` で上書き可）。compose を使わず Docker を直接実行する場合はこの変数を必ず設定すること。
- **SQLite は WAL モードで2プロセス共有。** ingest（書き込み）と app（読み取り）が同じ DB ファイルを使う。購読が別プロセスになったため gunicorn のワーカー数は増やせる（デフォルト1）。
- **ingest は必ず1プロセスのみ。** 複数起動すると購読とカーソル管理が競合する。
