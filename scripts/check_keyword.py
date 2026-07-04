"""keyword.toml へのキーワード追加を省力化する診断・自動化ツール。

追加したい語が Sudachi のトークン分割を経てマッチするかを検査し、
1トークンで認識されない場合は user.csv の候補行を提示・追記する。
従来の「keyword.toml 編集 → matcher 手動実行 → user.csv 編集 →
辞書ビルド → 再実行」の手順を1コマンドにまとめたもの。

使い方:
    # 診断のみ（ファイルは変更しない）
    uv run python scripts/check_keyword.py "新曲タイトル"

    # keyword.toml へ追記してから検証
    uv run python scripts/check_keyword.py "新曲タイトル" --add rank1

    # 認識されない語を user.csv へ追記し、辞書を再ビルドして再検証
    uv run python scripts/check_keyword.py "new song title" --add rank1 --fix --reading ニューソングタイトル

読み（--reading）はカタカナで指定する。ひらがな・カタカナのみの語は自動導出される。
"""
import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

KEYWORD_TOML = REPO_ROOT / "keyword.toml"
USER_CSV = REPO_ROOT / "user.csv"

RANKS = ("rank1", "rank2", "rank1_surface")

# rank2 は異なる2語以上でしか採用されないため、動作確認には相方が必要
RANK2_PARTNERS = ("真乃", "灯織")


def kana_reading(word: str) -> str | None:
    """ひらがな・カタカナのみの語から読み（カタカナ）を導出する。導出できなければ None。

    既存の user.csv の慣例に合わせ、中黒・空白は読みから落とす。
    """
    reading = []
    for ch in word:
        if "ぁ" <= ch <= "ゖ":
            reading.append(chr(ord(ch) + 0x60))
        elif "ァ" <= ch <= "ヶ" or ch == "ー":
            reading.append(ch)
        elif ch in "・ 　":
            continue
        else:
            return None
    return "".join(reading) or None


def build_user_csv_row(word: str, reading: str) -> str:
    """名詞・固有名詞・一般として1トークンに固定する user.csv 行を生成する。

    見出しは小文字にする（Sudachi は入力を小文字化してから照合する）。
    正規化表記は語をそのまま使い、keyword.toml 側の表記と一致させる。
    """
    surface = word.lower()
    return f"{surface},4786,4786,5000,{surface},名詞,固有名詞,一般,*,*,*,{reading},{word},*,*,*,*,*"


def insert_keyword_line(toml_text: str, word: str, rank: str) -> str:
    """keyword.toml のテキストの指定 rank 配列末尾に語を1行追記する。"""
    if '"' in word or "\\" in word:
        raise ValueError(f"「{word}」は引用符・バックスラッシュを含むため追記できません")
    lines = toml_text.splitlines(keepends=True)
    out = []
    in_section = False
    inserted = False
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped == f"{rank} = [":
            in_section = True
        elif in_section and stripped == "]":
            out.append(f'    "{word}",\n')
            in_section = False
            inserted = True
        out.append(line)
    if not inserted:
        raise ValueError(f"keyword.toml に {rank} セクションが見つかりません")
    return "".join(out)


def registered_rank(word: str, keywords: dict[str, list[str]]) -> str | None:
    """語が keyword.toml のどの rank に登録済みかを返す（マッチングと同じく大文字小文字は無視）。"""
    lowered = word.lower()
    for rank in RANKS:
        if lowered in {w.lower() for w in keywords.get(rank, [])}:
            return rank
    return None


def covering_alias(word: str, keywords_toml: dict, tokenizer) -> str | None:
    """user.csv の正規化により既存キーワードとして採用される語なら、その正規化先を返す。

    例: shiny colors → シャイニーカラーズ（rank1 登録済み）。この場合 keyword.toml へ
    追加しても正規化形と一致せず発火しない死にエントリになるため、追加は不要。
    """
    lowered = word.lower()
    known = {w.lower() for w in keywords_toml.get("rank1", []) + keywords_toml.get("rank2", [])}
    result: set[str] | None = None
    for text in (word, f"{word}が好きです"):
        hits = {
            t.normalized_form().lower()
            for t in tokenizer.tokenize(text)
            if t.surface().lower() == lowered and t.normalized_form().lower() in known
        } - {lowered}
        result = hits if result is None else result & hits
    return next(iter(result or ()), None)


def alias_makes_add_redundant(rank: str, alias: str, keywords_toml: dict) -> bool:
    """既存キーワードへ正規化される別表記について、rank への追加が死にエントリになるか。

    rank1 / rank2（正規化形マッチ層）への追加は、トークンの正規化形が別表記自身に
    ならないため常に不要。rank1_surface（表面形マッチ層）は、正規化先が rank1 なら
    既に単独採用されるため不要だが、正規化先が rank2 の場合は単独では採用されない
    ため、表面形エントリとして追加する意味がある。
    """
    if rank != "rank1_surface":
        return True
    return alias in {w.lower() for w in keywords_toml.get("rank1", [])}


def diagnose(word: str, registered: str | None, keywords_toml: dict, tokenizer, match_fn) -> dict:
    """語が1トークンで認識されるかを検査し、結果を表示して返す。"""
    print(f"\n=== 「{word}」 ===")
    print(f"  keyword.toml: {f'{registered} に登録済み' if registered else '未登録'}")

    lowered = word.lower()
    rank1_set = {w.lower() for w in keywords_toml.get("rank1", [])}
    texts = [word, f"{word}が好きです"]
    normalized_ok = True
    surface_ok = True
    for text in texts:
        tokens = tokenizer.tokenize(text)
        normalized_ok &= any(t.normalized_form().lower() == lowered for t in tokens)
        surface_ok &= any(t.surface().lower() == lowered for t in tokens)
        breakdown = " / ".join(f"{t.surface()}→{t.normalized_form()}" for t in tokens)
        print(f"  分割「{text}」: {breakdown}")
    # user.csv により既存キーワードへ正規化される別表記（例: shiny colors → シャイニーカラーズ）
    alias_target = covering_alias(word, keywords_toml, tokenizer)

    # rank1_surface は表面形、それ以外（未登録含む）は正規化形か既存キーワードへの正規化で認識される
    recognized = surface_ok if registered == "rank1_surface" else (normalized_ok or alias_target is not None)

    # 語自体の登録 rank、なければ別表記先の rank で match_shiny_colors の動作を確認する
    effective_rank = registered or (alias_target and ("rank1" if alias_target in rank1_set else "rank2"))
    ok = recognized
    if effective_rank:
        if effective_rank == "rank2":
            partner = next(p for p in RANK2_PARTNERS if p.lower() != lowered)
            sample = f"{word}と{partner}"
        else:
            sample = f"{word}が好きです"
        matched = match_fn(sample)
        print(f"  match_shiny_colors(「{sample}」) = {matched}")
        if matched and not recognized:
            print("  ⚠ 別の登録キーワードが反応しています（この語自体は認識されていません）")
        ok = matched and recognized

    if not recognized:
        if surface_ok and registered != "rank1_surface":
            print("  ⚠ 表面形は一致します（正規化形が別の語になる場合は rank1_surface も検討）")
        print("  ❌ 1トークンで認識されません。user.csv への登録が必要です:")
        print(f"     {build_user_csv_row(word, kana_reading(word) or '【読みをカタカナで指定】')}")
        print("     --fix を付けて再実行すると自動追記します（読みが導出できない語は --reading <カタカナ> も指定）")
    elif effective_rank and not ok:
        print("  ❌ トークンは認識されますが match_shiny_colors が False です")
    elif alias_target and not normalized_ok:
        print(f"  ✅ 既存キーワード「{alias_target}」の別表記として認識されます（user.csv で正規化済み）")
        if registered is None:
            print("     keyword.toml への追加は不要です")
    elif registered is None:
        print("  ✅ 1トークンで認識されます。固有性が高ければ rank1、単独で曖昧なら rank2 へ（--add で追記できます）")
    else:
        print("  ✅ マッチします")

    return {"word": word, "recognized": recognized, "ok": ok}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="キーワードが Sudachi 分割を経てマッチするか検査し、必要なら keyword.toml / user.csv を更新する",
    )
    parser.add_argument("keywords", nargs="+", metavar="キーワード")
    parser.add_argument("--add", choices=RANKS, help="検証前に keyword.toml の指定 rank へ追記する")
    parser.add_argument(
        "--fix", action="store_true", help="認識されない語を user.csv へ追記し、辞書を再ビルドして再検証する"
    )
    parser.add_argument("--reading", help="user.csv に書く読み（カタカナ）。キーワードが1語のときのみ指定可")
    args = parser.parse_args(argv)

    if args.reading and len(args.keywords) != 1:
        parser.error("--reading はキーワードが1語のときのみ指定できます")

    # keyword.toml と server/matcher.py はリポジトリルート基準で読み込まれる
    os.chdir(REPO_ROOT)

    keywords_toml = tomllib.loads(KEYWORD_TOML.read_text())

    # --add の別表記チェックと matcher の両方が使うため、先に辞書をビルドする
    from scripts.build_user_dict import build_user_dict

    build_user_dict()

    if args.add:
        # matcher は import 時に追記後の keyword.toml を読む必要があるため、
        # 追記前のチェックには独自にロードしたトークナイザーを使う
        from sudachipy import dictionary

        pre_tokenizer = dictionary.Dictionary().create()
        toml_text = KEYWORD_TOML.read_text()
        for word in args.keywords:
            rank = registered_rank(word, keywords_toml)
            if rank:
                print(f"「{word}」は {rank} に登録済みのため追記をスキップします")
                continue
            alias = covering_alias(word, keywords_toml, pre_tokenizer)
            if alias and alias_makes_add_redundant(args.add, alias, keywords_toml):
                print(
                    f"「{word}」は user.csv により既存キーワード「{alias}」へ正規化され採用されるため、"
                    "keyword.toml への追記をスキップします"
                )
                continue
            toml_text = insert_keyword_line(toml_text, word, args.add)
            keywords_toml = tomllib.loads(toml_text)  # 構文検証を兼ねる
            print(f"keyword.toml の {args.add} に「{word}」を追記しました")
        KEYWORD_TOML.write_text(toml_text)

    # matcher は import 時に keyword.toml を読むため、追記後に import する
    from server import matcher

    results = [
        diagnose(word, registered_rank(word, keywords_toml), keywords_toml, matcher.tokenizer, matcher.match_shiny_colors)
        for word in args.keywords
    ]
    needs_dict = [r["word"] for r in results if not r["recognized"]]

    if args.fix and needs_dict:
        rows = []
        missing = []
        for word in needs_dict:
            if "," in word:
                print(f"「{word}」はカンマを含むため user.csv に登録できません（表記を見直してください）")
                return 1
            reading = args.reading or kana_reading(word)
            if reading is None:
                missing.append(word)
            else:
                rows.append(build_user_csv_row(word, reading))
        if missing:
            print(f"\n読みを導出できません: {'、'.join(missing)}")
            print("1語ずつ --reading <カタカナ> を指定して実行してください")
            return 1

        csv_text = USER_CSV.read_text()
        if not csv_text.endswith("\n"):
            csv_text += "\n"
        USER_CSV.write_text(csv_text + "\n".join(rows) + "\n")
        build_user_dict()
        print(f"\nuser.csv に {len(rows)} 行を追記し、辞書を再ビルドしました。新しいプロセスで再検証します。")
        # ロード済みの Sudachi 辞書には反映されないため、別プロセスで検証し直す
        sys.stdout.flush()
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), *args.keywords], cwd=REPO_ROOT)
        return proc.returncode

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
