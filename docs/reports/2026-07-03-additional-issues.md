# 追加課題調査レポート

- 作成日: 2026-07-03
- 種別: スナップショット（この時点の状況記録。以後の変更はここに反映しない）
- 前提: [2026-07-03 プロジェクト全体分析](2026-07-03-project-analysis.md) の検証と補完。同レポートの指摘事項はすべて有効であることをコードと突き合わせて確認済み。テストは51件全パス。

## 総括

既存レポートの課題に加えて、新たに6件の課題を確認した。うち1件は既存レポートの記載の訂正（正規表現メタ文字の問題は「将来のリスク」ではなく既に発生している）、1件は外部入力で 500 エラーを誘発できる実バグ（カーソルの `OverflowError` 未捕捉）。いずれも修正は小規模で済む。

## 新規に確認した課題

### 1. 正規表現メタ文字が既に keyword.toml に存在する（既存レポートの訂正）

既存レポートは「現状のキーワードでは実害がない」としているが、`keyword.toml` の rank1 には未エスケープの `.` を含む語が既に存在する:

- `W.I.N.G.` / `G.R.A.D.` / `S.T.E.P.`
- `dye the sky.`

`^W.I.N.G.$` は `.` が任意の1文字にマッチするため、理論上の誤検出経路が現存する。一方で `spread the wings\!\!` のように `!` だけ手動エスケープされた語もあり（`!` は正規表現メタ文字ではないため不要）、扱いが一貫していない。AGENTS.md の「メタ文字を含む語は追加しない」ルールは既に破られている状態であり、`matcher.py` に `re.escape` を導入するのが現実的な解決策。

### 2. 不正なカーソルで 500 エラー（OverflowError 未捕捉）

`server/algos/shiny_colors.py` のカーソル解析で、タイムスタンプ部に巨大な数値（例: `9999999999999999999999999::x`）を渡すと `datetime.fromtimestamp` が `OverflowError` を投げる。捕捉しているのは `ValueError` と `OSError` のみ（shiny_colors.py:29、app.py:87）のため、外部入力で 500 Internal Server Error を誘発できる。`except` 節に `OverflowError` を追加すれば解消する。

再現（この時点の Python 3.11 で確認）:

```python
>>> datetime.fromtimestamp(int('9'*25)/1000)
OverflowError: timestamp out of range for platform time_t
```

### 3. getFeedSkeleton の limit が無検証

`server/app.py:85` で `limit` クエリパラメータをそのままクエリに渡している。`limit=100000` のような値で直接叩かれると DB 全件を返す。AT Protocol の仕様上は 1〜100 のため、サーバー側でもクランプすべき。

### 4. EXCLUDED_DID / PRIORITY_DID の空白が除去されない

`server/config.py:32,35` の `did.strip()` は空要素のフィルタにのみ使われ、値自体は strip されない。`.env` で `;` の後に空白を入れて書くと（`did:a; did:b`）、2つ目以降の DID が前後の空白付きでリストに入り、`author in config.EXCLUDED_DID_LIST` の比較が静かに失敗する。

### 5. SIGTERM でストリームスレッドが正常停止しない

`server/app.py:28` はシグナルハンドラを SIGINT にのみ登録しているが、`docker compose down` や gunicorn のワーカー再起動は SIGTERM を送る。Firehose 購読スレッドは non-daemon のため、graceful shutdown がタイムアウトまで待たされた後に強制 kill される。SIGTERM にも同じハンドラを登録するのが望ましい。

### 6. 複数語キーワードはローカルでは構造的にマッチしない

`dye the sky.` `spread the wings\!\!` のような複数語の rank1 キーワードは、`user.csv`（Docker ビルド時のみ組み込み）で1トークンに正規化されて初めてマッチする。既知の「ローカルと本番でマッチング挙動が異なる」課題の具体的な帰結として、これらの語はローカル実行・pytest では絶対にマッチせず、テストで検証できていない。

## 推奨事項（優先度順）

1. `matcher.py` に `re.escape` を導入し、`shiny_colors.py` の `except` に `OverflowError` を追加し、`get_feed_skeleton` で `limit` を 1〜100 にクランプする（いずれも小さな変更でテスト追加も容易）。
2. `.env.example` と README に `FEEDGEN_SQLITE_LOCATION` を追記する（既存レポートの推奨事項1と同じ。この時点でも未対応）。
3. SIGTERM ハンドラを追加する。
4. DID リストの各要素に `strip()` を適用する。
