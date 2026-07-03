# AGENTS.md

Bluesky の Firehose から「シャイニーカラーズ」関連の日本語投稿を収集し、カスタムフィードとして配信する Feed Generator。詳細は [docs/architecture.md](docs/architecture.md) を参照。

## 言語

- コミットメッセージ・ドキュメント・コード内コメントは日本語で書く（既存の履歴・文書に合わせる）。

## コマンド

```shell
uv sync                # 依存関係のインストール
uv run pytest          # テスト実行（全件パスを維持すること）
uv run python scripts/build_user_dict.py  # Sudachi ユーザー辞書の組み込み（pytest / Docker は自動実行）
flask --debug run      # 開発サーバー起動（.flaskenv で port 8000）
docker compose up      # 本番相当の起動
uv run python publish_feed.py  # フィードレコードの公開/更新
```

- パッケージ管理は uv。pip や requirements.txt は使わない。依存を変更したら `uv.lock` も更新してコミットする。

## テスト

- 変更後は必ず `uv run pytest` を実行し、全件パスを確認してからコミットする。
- テスト用の環境変数はルートの `conftest.py` が設定する（in-memory SQLite 等）。Sudachi ユーザー辞書も `conftest.py` が自動ビルドするため、テストは本番と同じ辞書で走る。
- `server/config.py` は import 時に `HOSTNAME` / `SHINY_URI` 未設定だと例外を投げる。server 配下のモジュールを REPL やスクリプトから import する場合は、先にこれらの環境変数を設定すること。

## import 時の副作用（重要）

- `server/app.py` は import した時点で Firehose 購読スレッドを起動する。
- `server/database.py` は import した時点で DB へ接続しテーブルを作成する。
- `server/matcher.py` は import した時点で `keyword.toml` を読み Sudachi 辞書をロードする（`keyword.toml` はカレントディレクトリ基準なのでリポジトリルートから実行すること）。

## keyword.toml の編集ルール

- **rank1**: 1トークンの完全一致で即採用。固有性の高い語（作品名・ユニット名・フルネーム・楽曲名）のみ追加する。一般語を入れると誤検出が急増する。
- **rank2**: 異なる2語以上のマッチで採用。姓のみ・名のみなど単独では曖昧な語を入れる。
- マッチングは Sudachi の `normalized_form()`（正規化形）に対して行われる。表記ゆれを吸収したい場合は `user.csv`（ユーザー辞書）に正規化エントリを追加する。
- キーワードは `re.escape` でエスケープされてから正規表現へ連結されるため、`.` `+` `(` などのメタ文字を含む語もそのまま書いてよい。手動で `\` エスケープしないこと（バックスラッシュ自体が文字として解釈され、マッチしなくなる）。
- コミットメッセージは既存の慣例に従い「キーワードの追加」とする。

## 環境差の罠

- **Sudachi ユーザー辞書（user.csv）は `scripts/build_user_dict.py` で venv 内の sudachipy に組み込む。** pytest（conftest.py）と Docker ビルドは自動実行する。`uv sync` で venv を作り直すと辞書は消えるが、次回の pytest かスクリプト実行で再ビルドされる。`flask run` で辞書を使う場合は先にスクリプトを実行すること。
- **Docker 運用では `.env` に `FEEDGEN_SQLITE_LOCATION=db/feed.db` を明示する。** デフォルトの `feed.db` はボリュームマウント外で、コンテナ再作成時に消える。
- **gunicorn は1ワーカー前提。** ワーカーを増やすと Firehose 購読が重複するため、ワーカー数を変更しないこと。

## ドキュメント管理

- 実装を変更したら [docs/architecture.md](docs/architecture.md) を同じコミット/PR で更新する。
- 時点依存の調査結果は `docs/reports/YYYY-MM-DD-題名.md` として追加し、作成後は編集しない。
- 設計上の意思決定は `docs/adr/NNNN-題名.md` に記録する。
- セットアップ手順・環境変数・エンドポイント一覧の唯一の情報源はルートの `README.md`。他の文書に重複させない。
