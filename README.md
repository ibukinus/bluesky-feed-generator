# Bluesky シャイニーカラーズ Feed Generator

Bluesky の Firehose からシャイニーカラーズ関連の投稿をリアルタイムで収集し、カスタムフィードとして配信する Feed Generator です。

[AT Protocol SDK for Python](https://github.com/MarshalX/atproto) を使用しています。

## 仕組み

1. Bluesky Firehose（全投稿のリアルタイムストリーム）を購読
2. 日本語の投稿を対象に、Sudachi 形態素解析でキーワードマッチングを実行
3. マッチした投稿を SQLite に保存
4. AT Protocol 標準エンドポイントを通じてフィードを配信

### キーワードマッチング

`keyword.toml` で2段階のマッチングを定義しています。

- **rank1**: 完全一致で即採用（キャラクター名、ユニット名、楽曲名など）
- **rank2**: 2つ以上の異なるキーワードが含まれる場合に採用（姓のみ、略称など）

画像の ALT テキストもマッチング対象です。

## 技術スタック

- **Python 3.11+**
- **Flask** - API サーバー
- **gunicorn** - WSGI サーバー
- **atproto** - AT Protocol SDK
- **peewee** - ORM（SQLite）
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
flask --debug run
```

サーバーは `http://127.0.0.1:8000` で起動します。

### Docker（本番環境）

```shell
docker compose up
```

- gunicorn で `0.0.0.0:8000` にバインド
- `./db` をボリュームマウントして DB を永続化
- `.env` から環境変数を読み込み

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
    ├── data_stream.py   # Firehose 購読
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
