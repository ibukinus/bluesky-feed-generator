import tomllib

import pytest

from scripts.check_keyword import (
    KEYWORD_TOML,
    alias_makes_add_redundant,
    build_user_csv_row,
    covering_alias,
    diagnose,
    insert_keyword_line,
    kana_reading,
    registered_rank,
)


class TestKanaReading:
    """読み（カタカナ）の自動導出のテスト"""

    def test_katakana_as_is(self):
        assert kana_reading("ノフィロ") == "ノフィロ"

    def test_hiragana_converted(self):
        assert kana_reading("もちほわ") == "モチホワ"

    def test_long_vowel_kept(self):
        assert kana_reading("イルミネーションスターズ") == "イルミネーションスターズ"

    def test_middle_dot_dropped(self):
        # 既存 user.csv の慣例（ラビリンス・レジスタンス → ラビリンスレジスタンス）に合わせる
        assert kana_reading("ラビリンス・レジスタンス") == "ラビリンスレジスタンス"

    def test_ascii_not_derivable(self):
        assert kana_reading("shiny colors") is None

    def test_kanji_not_derivable(self):
        assert kana_reading("櫻木真乃") is None


class TestBuildUserCsvRow:
    """user.csv 候補行の生成のテスト"""

    def test_field_count(self):
        assert len(build_user_csv_row("ノフィロ", "ノフィロ").split(",")) == 18

    def test_surface_lowercased(self):
        # Sudachi は入力を小文字化して照合するため、見出しは小文字にする
        row = build_user_csv_row("Shiny Song", "シャイニーソング").split(",")
        assert row[0] == "shiny song"
        assert row[4] == "shiny song"

    def test_normalized_form_keeps_original(self):
        # 正規化表記は keyword.toml 側の表記と一致させる（マッチは IGNORECASE）
        row = build_user_csv_row("Shiny Song", "シャイニーソング").split(",")
        assert row[12] == "Shiny Song"

    def test_reading_field(self):
        row = build_user_csv_row("ノフィロ", "ノフィロ").split(",")
        assert row[11] == "ノフィロ"


class TestInsertKeywordLine:
    """keyword.toml への追記のテスト"""

    TOML = 'rank1 = [\n    "シャニマス",\n]\n\nrank2 = [\n    "櫻木",\n]\n'

    def test_appends_to_target_rank(self):
        result = insert_keyword_line(self.TOML, "新曲", "rank1")
        parsed = tomllib.loads(result)
        assert parsed["rank1"] == ["シャニマス", "新曲"]
        assert parsed["rank2"] == ["櫻木"]

    def test_appends_to_rank2(self):
        result = insert_keyword_line(self.TOML, "新姓", "rank2")
        parsed = tomllib.loads(result)
        assert parsed["rank1"] == ["シャニマス"]
        assert parsed["rank2"] == ["櫻木", "新姓"]

    def test_metachar_word_kept_literal(self):
        result = insert_keyword_line(self.TOML, "w.i.n.g.+(仮)", "rank1")
        assert "w.i.n.g.+(仮)" in tomllib.loads(result)["rank1"]

    def test_rejects_quote_and_backslash(self):
        with pytest.raises(ValueError):
            insert_keyword_line(self.TOML, 'foo"bar', "rank1")
        with pytest.raises(ValueError):
            insert_keyword_line(self.TOML, "foo\\bar", "rank1")

    def test_missing_section_raises(self):
        with pytest.raises(ValueError):
            insert_keyword_line(self.TOML, "新曲", "rank1_surface")

    def test_real_keyword_toml_roundtrip(self):
        # 実ファイルの整形（セクション・コメント・閉じ括弧）でも壊れないこと
        real = KEYWORD_TOML.read_text()
        result = insert_keyword_line(real, "テスト用の語", "rank1_surface")
        parsed = tomllib.loads(result)
        assert "テスト用の語" in parsed["rank1_surface"]


class TestCoveringAlias:
    """既存キーワードへ正規化される別表記の検出（--add の冗長追記防止に使う）"""

    @staticmethod
    def run(word):
        from server import matcher

        keywords = tomllib.loads(KEYWORD_TOML.read_text())
        return covering_alias(word, keywords, matcher.tokenizer)

    def test_alias_returns_normalized_target(self):
        assert self.run("shiny colors") == "シャイニーカラーズ"

    def test_canonical_keyword_is_not_alias(self):
        # 正規化形が自分自身の登録キーワードは別表記ではない
        assert self.run("シャニマス") is None

    def test_unrelated_word(self):
        assert self.run("東京") is None

    def test_multi_token_word(self):
        assert self.run("完全に無関係な新曲名") is None


class TestAliasMakesAddRedundant:
    """別表記があるときに rank への追加をスキップすべきかの判定"""

    KEYWORDS = {"rank1": ["シャイニーカラーズ"], "rank2": ["真乃"], "rank1_surface": []}

    def test_normalized_rank_add_is_always_redundant(self):
        # rank1/rank2 は正規化形マッチのため、別表記自身のエントリは発火しない
        assert alias_makes_add_redundant("rank1", "シャイニーカラーズ", self.KEYWORDS)
        assert alias_makes_add_redundant("rank2", "真乃", self.KEYWORDS)

    def test_surface_add_for_rank1_alias_is_redundant(self):
        # 正規化先が rank1 なら既に単独採用されるため表面形エントリも不要
        assert alias_makes_add_redundant("rank1_surface", "シャイニーカラーズ", self.KEYWORDS)

    def test_surface_add_for_rank2_alias_is_meaningful(self):
        # 正規化先が rank2 だと単独では採用されないため、表面形での追加には意味がある
        assert not alias_makes_add_redundant("rank1_surface", "真乃", self.KEYWORDS)


class TestDiagnose:
    """診断本体のテスト（conftest.py がビルドした本番同等の辞書で検証する）"""

    @staticmethod
    def run(word):
        from server import matcher

        keywords = tomllib.loads(KEYWORD_TOML.read_text())
        return diagnose(
            word, registered_rank(word, keywords), keywords, matcher.tokenizer, matcher.match_shiny_colors
        )

    def test_registered_rank1(self):
        result = self.run("シャニマス")
        assert result["recognized"] and result["ok"]

    def test_registered_rank2(self):
        result = self.run("櫻木")
        assert result["recognized"] and result["ok"]

    def test_alias_normalized_to_existing_keyword(self):
        # user.csv で既存キーワードへ正規化される別表記は「認識済み」と判定する
        result = self.run("shiny colors")
        assert result["recognized"] and result["ok"]

    def test_unrecognized_multi_token(self):
        result = self.run("完全に無関係な新曲名")
        assert not result["recognized"]
