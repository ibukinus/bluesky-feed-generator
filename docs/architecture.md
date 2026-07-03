# アーキテクチャ

Bluesky の Firehose から「アイドルマスター シャイニーカラーズ」関連の日本語投稿を収集し、AT Protocol のカスタムフィードとして配信する Feed Generator。MarshalX 氏の [bluesky-feed-generator](https://github.com/MarshalX/bluesky-feed-generator) をベースに、Sudachi 形態素解析による2段階キーワードマッチングを組み込んでいる。

セットアップ手順・環境変数・エンドポイント一覧は [ルートの README](../README.md) を参照。

## データフロー

```
Bluesky Firehose
      │  (WebSocket 購読: FirehoseSubscribeReposClient)
      ▼
data_stream.run()  ← Flask アプリ import 時にバックグラウンドスレッドで起動
      │  CAR ブロックをデコードし create/delete 操作を種別ごとに集約
      ▼
data_filter.operations_callback()
      │  1. アーカイブ投稿/リプライの除外（環境変数でオプトイン）
      │  2. EXCLUDED_DID_LIST による除外
      │  3. PRIORITY_DID_LIST は無条件採用
      │  4. langs に 'ja' を含まない投稿を除外
      │  5. 本文 + 画像 ALT テキストに対しキーワードマッチング
      ▼
SQLite (peewee ORM: Post / SubscriptionState)
      ▲
      │  indexed_at DESC, cid DESC + カーソルページネーション
algos/shiny_colors.handler()
      ▲
Flask (app.py)
  ├── /.well-known/did.json                        … did:web ドキュメント
  ├── /xrpc/app.bsky.feed.describeFeedGenerator     … フィード情報
  └── /xrpc/app.bsky.feed.getFeedSkeleton           … フィード本体
```

- Firehose のカーソルは約1,000イベントごとに `SubscriptionState` に永続化され、再起動時に途中から再開できる。
- 削除イベントを受けると DB からも該当投稿を削除し同期を維持する。
- フィードのカーソルは `{ミリ秒タイムスタンプ}::{cid}` 形式。末尾に達すると `eof` を返す。不正なカーソルは 400 を返す。
- `getFeedSkeleton` の `limit` は AT Protocol の仕様に合わせてサーバー側で 1〜100 にクランプされる。
- SIGINT / SIGTERM を受けると Firehose 購読スレッドに停止を通知してから終了する（`docker compose down` の graceful shutdown に対応）。

## モジュール構成

| ファイル | 役割 | 備考 |
|---|---|---|
| `server/app.py` | Flask アプリ・XRPC エンドポイント | import 時に Firehose スレッド起動 |
| `server/data_stream.py` | Firehose 購読・再接続・カーソル管理 | FirehoseError 時は自動再接続（DEBUG 時は再送出） |
| `server/data_filter.py` | 投稿フィルタリング・DB 書き込み | 本文と画像 ALT の両方を判定 |
| `server/matcher.py` | キーワードマッチングエンジン | 起動時に keyword.toml を読み正規表現をコンパイル |
| `server/database.py` | peewee モデル（Post, SubscriptionState） | import 時にテーブル作成 |
| `server/config.py` | 環境変数の読み込み・検証 | HOSTNAME / SHINY_URI 未設定なら起動時に例外 |
| `server/algos/shiny_colors.py` | フィードアルゴリズム（時系列 + カーソル） | |
| `server/auth.py` | JWT 検証（未使用の参考実装） | フィードがユーザー非依存のため |
| `publish_feed.py` | フィードレコードの公開/更新スクリプト | |
| `conftest.py` | テスト用環境変数（in-memory SQLite 等） | |

## キーワードマッチング仕様

`server/matcher.py` + `keyword.toml` + Sudachi（`sudachi.json` / `user.csv`）で構成。

1. 投稿本文を SudachiPy でトークン化し、各トークンの `normalized_form()`（正規化形）を判定に使う。
2. **rank1**: 1トークンでも完全一致すれば即採用。作品名・ユニット名・キャラクターのフルネーム・楽曲名など固有性の高い語。
3. **rank2**: 「櫻木」「灯織」など姓のみ・名のみの語。**異なる2語以上**がマッチした場合のみ採用（「田中」「鈴木」単独のような誤検出を防ぐ）。
4. 正規表現は起動時に `^word1$|^word2$|...` 形式で1本にコンパイル（IGNORECASE）。各語は `re.escape` でエスケープされ、メタ文字を含む語も文字どおりに解釈される。
5. `user.csv` は Sudachi ユーザー辞書のソース。表記ゆれ（例: `shiny colors` → `シャイニーカラーズ`）を正規化形に集約し、`283プロダクション` などの分割防止も担う。

## デプロイ構成

- **Dockerfile**: マルチステージビルド。builder ステージで uv sync（frozen）+ `scripts/build_user_dict.py`（Sudachi ユーザー辞書ビルドと `sudachi.json` の配置）を実行し、runner は slim イメージに site-packages をコピーして gunicorn で `0.0.0.0:8000` を公開する。
- **compose.yml**: GHCR のイメージ（`ghcr.io/ibukinus/bluesky-feed-generator:latest`）を使用。`./db` を `/app/db` にマウント、`.env` を読み込み、ポート 8000 を公開。
- **CI/CD（GitHub Actions）**: PR で `ci.yml` がテストを実行。`shiny` への push で `deploy.yml` が テスト → イメージビルド → GHCR への push → SSH 経由で VM の `docker compose pull && up -d` を実行する。必要なシークレットは README を参照。
- ローカル開発は `uv sync` + `flask --debug run`（`.flaskenv` で port 8000）。

## 設計上の注意点（恒久的な制約）

- **Sudachi ユーザー辞書は `scripts/build_user_dict.py` で sudachipy の resources ディレクトリに組み込む。** Docker ビルドと pytest（conftest.py）は自動実行するため、テストは本番と同じ辞書で走る。素の `flask run` で辞書を使うには事前に同スクリプトを実行する。
- **SQLite の保存先はデフォルトで `feed.db`（コンテナ内 `/app/feed.db`）。** compose のボリュームマウント（`/app/db`）の外にあるため、Docker 運用では `.env` で `FEEDGEN_SQLITE_LOCATION=db/feed.db` を明示しないと再作成時に DB が消える。
- **gunicorn は1ワーカー前提。** Firehose 購読スレッドは import 時に起動するため、ワーカーを増やすと購読が重複する。
- 収集した投稿に保持期限はなく、DB は単調増加する。
