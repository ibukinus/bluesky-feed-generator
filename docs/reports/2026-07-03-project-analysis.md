# プロジェクト全体分析レポート

- 作成日: 2026-07-03
- 種別: スナップショット（この時点の状況記録。以後の変更はここに反映しない）
- 恒久的な構成情報は [architecture.md](../architecture.md) を参照

## 総括

全体として健全な状態にある。テスト51件は全てパスし、Firehose の再接続・カーソル永続化・削除同期などの基本機能は堅実に実装されている。直近の開発は主にキーワード追加の反復で、過去の Revert 往復（取得順序・エラーハンドリング・フィルター処理）は再適用済みの安定状態にある。

対応を要する主な事項は、(1) Docker 運用時の SQLite 保存先がボリューム外でデータ消失リスクがあること、(2) `server/matcher.py` に未コミットのデバッグコードが残っていること、(3) DB に保持期限がなく無制限に成長すること、の3点。

## テスト状況

`uv run pytest` → **51件全パス**（実行時間 1.5秒）。

| ファイル | 対象 |
|---|---|
| `tests/test_matcher.py` | マッチングロジック |
| `tests/test_data_filter.py` | フィルタ処理（229行、最も厚い） |
| `tests/test_shiny_colors_algo.py` | カーソルページネーション |
| `tests/test_app.py` | エンドポイント |
| `tests/test_config.py` | 設定読み込み |

テストは commit `986a79b` で追加された。

## Git / 作業状態

- ブランチ `shiny`（main 相当）、origin と同期済み。
- **未コミットの変更**: `server/matcher.py` に `if __name__ == "__main__":` の動作確認ブロック（テキスト `"No 1 feel alone"` を判定して print）が追加されている。デバッグ用の一時コードの可能性が高く、末尾の改行も欠落している。コミットするか破棄するかの判断が必要。
- 直近の履歴は「キーワードの追加」が大半。パッケージ管理は pip/requirements.txt から uv/pyproject.toml へ移行済み（`378fd13`）。

## キーワード規模（この時点）

- rank1（完全一致で即採用）: 278語
- rank2（異なる2語以上で採用）: 95語
- Sudachi ユーザー辞書ソース `user.csv`: 383行

## 検出した課題・リスク

### 中程度

1. **SQLite の保存先が Docker ボリューム外（データ消失リスク）**: compose は `./db:/app/db` をマウントするが、`FEEDGEN_SQLITE_LOCATION` のデフォルトは `feed.db`（= `/app/feed.db`）。`.env` で `FEEDGEN_SQLITE_LOCATION=db/feed.db` を明示しない限り、DB はコンテナ再作成で消える。`.env.example` と README にこの変数の記載がないため気づきにくい。
2. **ローカルと本番でマッチング挙動が異なる**: Sudachi ユーザー辞書は Docker ビルド時のみ組み込まれる。ローカル実行・pytest はシステム辞書のみで動くため、`shiny colors` → `シャイニーカラーズ` のような正規化に依存するマッチはローカルで再現せず、テストがこの差分を検証できない。
3. **DB の無制限成長**: 収集した投稿に保持期限・クリーンアップ処理がなく、SQLite が単調増加する。

### 軽微

4. **不要なレコード種別のデコード**: `data_stream.py` の `_INTERESTED_RECORDS` に Like / Follow が含まれるが、`operations_callback` は投稿しか処理しない。Firehose の大部分を占める Like のデコードは CPU の無駄。
5. **正規表現エスケープなし**: `keyword.toml` の語はエスケープせず正規表現に連結される。現状のキーワードでは実害がないが、`.` や `+` を含む語（例: 曲名）を追加すると意図しないマッチが起こり得る。
6. **タイムゾーンの不整合の可能性**: `Post.indexed_at` は `datetime.utcnow`（naive UTC）で保存する一方、カーソル復元は `datetime.fromtimestamp`（ローカル TZ）を使う。コンテナ（TZ=UTC）では一致するが、TZ が UTC 以外の環境ではページネーションがずれる。`utcnow` は Python 3.12 で非推奨。
7. **gunicorn 多重ワーカー非対応**: Firehose 購読スレッドは import 時に起動するため、ワーカー数を増やすと購読が重複する（現状はデフォルト1ワーカーのため問題なし）。
8. **`Post.uri` に unique 制約がない**: Firehose の再接続・カーソル巻き戻し時に同一投稿が重複登録され得る。

## 推奨事項（優先度順）

1. `.env.example` と README に `FEEDGEN_SQLITE_LOCATION` を追記し、compose 運用では `db/feed.db` を明示する。
2. `server/matcher.py` の未コミット変更（デバッグブロック）をコミットするか破棄するか決める。残すなら末尾改行を追加。
3. 古い投稿の定期削除（例: indexed_at が N 日超のレコードを削除）を追加する。
4. `_INTERESTED_RECORDS` を `AppBskyFeedPost` のみに絞り、Firehose 処理の CPU を削減する。
5. キーワードを正規表現に組み込む際に `re.escape` を適用する。
6. `Post.uri` に unique 制約 + `INSERT OR IGNORE` 相当の処理を入れる。
