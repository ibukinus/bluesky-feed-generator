# Bluesky シャイニーカラーズ Feed Generator

Bluesky の Jetstream からシャイニーカラーズ関連の投稿をリアルタイムで収集し、カスタムフィードとして配信する Feed Generator です。

[AT Protocol SDK for Python](https://github.com/MarshalX/atproto) を使用しています。

## 仕組み

1. Bluesky Jetstream（投稿のリアルタイムストリーム）を収集プロセス（`server/ingest.py`）が購読
2. 日本語の投稿を対象に、Sudachi 形態素解析でキーワードマッチングを実行
3. マッチした投稿を SQLite に保存（保持期限を過ぎた投稿は自動削除）
4. 配信プロセス（Flask）が AT Protocol 標準エンドポイントを通じてフィードを配信

### キーワードマッチング

`keyword.toml` で2段階のマッチングを定義しています。

- **rank1**: 完全一致で即採用（キャラクター名、ユニット名、楽曲名など）
- **rank1_surface**: 書かれたままの表記（表面形）の完全一致でのみ採用。正規化で誤検出する語（例: seeds → シーズ）に使う
- **rank2**: 2つ以上の異なるキーワードが含まれる場合に採用（姓のみ、略称など）

画像の ALT テキストもマッチング対象です。

## 技術スタック

- **Python 3.11+**
- **Flask** - API サーバー
- **gunicorn** - WSGI サーバー
- **atproto** - AT Protocol SDK
- **websockets** - Jetstream 購読
- **peewee** - ORM（SQLite、WAL モード）
- **SudachiPy** - 日本語形態素解析
- **uv** - パッケージ管理

## セットアップ

### 環境変数

`.env.example` を `.env` にコピーし、必要な値を設定します。

```shell
cp .env.example .env
```

**サーバー実行時に必要:**

| 変数名 | 説明 | 必須 |
|--------|------|------|
| `HOSTNAME` | did:web 解決用のドメイン名 | Yes |
| `SHINY_URI` | フィード URI（公開後に取得） | Yes |
| `SERVICE_DID` | カスタム DID（デフォルト: `did:web:{HOSTNAME}`） | No |
| `FEEDGEN_SQLITE_LOCATION` | SQLite DB の保存先（デフォルト: `feed.db`。compose 実行時は両サービス共有の `db/feed.db` が自動設定される） | No |
| `JETSTREAM_ENDPOINT` | Jetstream の WebSocket URL（デフォルト: `wss://jetstream2.us-east.bsky.network/subscribe`） | No |
| `FEEDGEN_POST_RETENTION_DAYS` | 投稿の保持日数（デフォルト: `30`、`0` で無期限） | No |
| `EXCLUDED_DID` | 除外する DID（セミコロン区切り） | No |
| `PRIORITY_DID` | 優先する DID（セミコロン区切り） | No |
| `IGNORE_ARCHIVED_POSTS` | Twitter/X からのインポート投稿を除外 | No |
| `IGNORE_REPLY_POSTS` | リプライ投稿を除外 | No |

**フィード公開時（`publish_feed.py`）に必要:**

| 変数名 | 説明 | 必須 |
|--------|------|------|
| `HANDLE` | Bluesky ハンドル | Yes |
| `PASSWORD` | Bluesky アプリパスワード | Yes |
| `RECORD_NAME` | フィード識別子（小文字、スペースなし） | Yes |
| `DISPLAY_NAME` | フィードの表示名 | Yes |
| `DESCRIPTION` | フィードの説明文 | No |
| `AVATAR_PATH` | アバター画像のパス | No |
| `ACCEPTS_INTERACTIONS` | クライアントからのインタラクションを受け付けるか | No |
| `IS_VIDEO_FEED` | 動画フィードかどうか | No |

### 開発環境

```shell
uv sync
uv run python scripts/build_user_dict.py  # Sudachi ユーザー辞書の組み込み（初回と user.csv 更新時）
flask --debug run                         # 配信 API（http://127.0.0.1:8000）
uv run python -m server.ingest            # 投稿収集（別ターミナルで。配信だけ試すなら不要）
```

pytest 実行時は `conftest.py` が辞書ビルドを自動で行うため、手動実行は不要です。

### Docker（本番環境）

```shell
docker compose up
```

- 2つのサービスが起動する: `app`（配信 API、gunicorn で `0.0.0.0:8000`）と `ingest`（Jetstream 購読・投稿収集）。同じイメージを共有する
- CI が push した GHCR イメージ（`ghcr.io/ibukinus/bluesky-feed-generator:latest`）を使用（ローカルでビルドする場合は `docker compose build`）
- GHCR イメージは **linux/arm64 のみ**（デプロイ先の OCI VM と Apple Silicon Mac に対応）。x86_64 ホストでは `docker compose build` でローカルビルドすること
- `./db` をボリュームマウントして DB を永続化（compose が `FEEDGEN_SQLITE_LOCATION=db/feed.db` を自動設定し、app と ingest が同じ DB を共有する）
- `.env` から環境変数を読み込み

### デプロイ（CI/CD）

`shiny` ブランチへの push で GitHub Actions（`.github/workflows/deploy.yml`）が「テスト → Docker イメージビルド → GHCR への push → VM へのデプロイ」を自動実行します。PR には CI（`.github/workflows/ci.yml`）がテストを実行します。

**必要なリポジトリシークレット:**

| シークレット | 説明 |
|------|------|
| `DEPLOY_SSH_HOST` | デプロイ先 VM のホスト名 / IP |
| `DEPLOY_SSH_USER` | SSH ユーザー名 |
| `DEPLOY_SSH_KEY` | SSH 秘密鍵（PEM 形式） |
| `DEPLOY_APP_DIR` | VM 上の `compose.yml` と `.env` を配置したディレクトリ |

**VM 側の事前準備（初回のみ）:**

1. `.env` を `DEPLOY_APP_DIR` に配置する（`compose.yml` はデプロイ時に自動配布される）
2. GHCR のパッケージ（`ghcr.io/ibukinus/bluesky-feed-generator`）を public に設定する（private のままにする場合は VM 上で `docker login ghcr.io` を済ませておく）

## フィードの公開

```shell
uv run python publish_feed.py
```

公開後に出力されるフィード URI を `.env` の `SHINY_URI` に設定してください。

フィードの表示名・説明・アバターを更新する場合は `.env` を編集して再実行します。

## エンドポイント

| パス | 説明 |
|------|------|
| `/.well-known/did.json` | DID ドキュメント |
| `/xrpc/app.bsky.feed.describeFeedGenerator` | フィード情報 |
| `/xrpc/app.bsky.feed.getFeedSkeleton` | フィード本体 |

## プロジェクト構成

```
├── .github/workflows/   # CI・自動デプロイ（GitHub Actions）
├── scripts/
│   └── build_user_dict.py  # Sudachi ユーザー辞書ビルド（pytest/Docker が自動実行）
├── Dockerfile           # マルチステージビルド
├── compose.yml          # Docker Compose 設定
├── pyproject.toml       # プロジェクト設定・依存関係
├── keyword.toml         # キーワードマッチング設定
├── sudachi.json         # Sudachi 設定
├── user.csv             # Sudachi カスタム辞書
├── publish_feed.py      # フィード公開スクリプト
├── .flaskenv            # Flask 開発サーバー設定
├── LICENSE              # MIT ライセンス
└── server/
    ├── __init__.py
    ├── __main__.py      # デバッグ用エントリーポイント
    ├── app.py           # Flask アプリケーション
    ├── auth.py          # JWT 認証
    ├── config.py        # 設定読み込み
    ├── logger.py        # ロギング設定
    ├── database.py      # DB モデル（Post, SubscriptionState）
    ├── ingest.py        # Jetstream 購読・投稿収集（独立プロセス）
    ├── data_filter.py   # フィルタリングロジック
    ├── matcher.py       # キーワードマッチングエンジン
    └── algos/
        ├── __init__.py  # アルゴリズム登録
        └── shiny_colors.py  # フィードアルゴリズム
```

## ドキュメント

詳細な文書は [docs/](docs/README.md) にあります。

- [アーキテクチャ](docs/architecture.md) — データフロー、モジュール構成、キーワードマッチング仕様、設計上の注意点
- [調査レポート](docs/README.md#レポート一覧) — 日付つきのスナップショット

## ライセンス

MIT
