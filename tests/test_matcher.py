from server.matcher import match_shiny_colors, rank1, rank1_surface, rank2, rank1_regex


class TestMatchShinyColorsRank1:
    """rank1キーワード（単体でマッチ）のテスト"""

    def test_exact_keyword(self):
        assert match_shiny_colors("シャニマス") is True

    def test_keyword_in_sentence(self):
        assert match_shiny_colors("今日もシャニマスやるぞ") is True

    def test_unit_name(self):
        assert match_shiny_colors("イルミネーションスターズが好き") is True

    def test_character_full_name(self):
        assert match_shiny_colors("櫻木真乃ちゃん可愛い") is True

    def test_song_title(self):
        assert match_shiny_colors("シャイノグラフィ最高") is True

    def test_shiny_colors_full(self):
        assert match_shiny_colors("シャイニーカラーズ") is True

    def test_abbreviation(self):
        assert match_shiny_colors("放クラ推し") is True

    def test_case_insensitive_english(self):
        assert match_shiny_colors("SONG FOR PRISMを聴いた") is True


class TestMatchShinyColorsRank2:
    """rank2キーワード（2つ以上でマッチ）のテスト"""

    def test_single_rank2_no_match(self):
        assert match_shiny_colors("櫻木さんと会った") is False

    def test_two_rank2_match(self):
        assert match_shiny_colors("櫻木と風野が共演") is True

    def test_surname_and_firstname(self):
        assert match_shiny_colors("真乃と灯織のコンビ") is True


class TestMatchShinyColorsNoMatch:
    """マッチしないケースのテスト"""

    def test_unrelated_text(self):
        assert match_shiny_colors("今日は天気がいい") is False

    def test_empty_string(self):
        assert match_shiny_colors("") is False

    def test_only_common_words(self):
        assert match_shiny_colors("東京で買い物をした") is False


class TestKeywordEscaping:
    """キーワードが正規表現として解釈されず文字どおりマッチすることのテスト"""

    def test_metachar_keyword_matches_literally(self):
        assert rank1_regex.match("w.i.n.g.")

    def test_dot_is_not_wildcard(self):
        # re.escape 導入前は「W.I.N.G.」の `.` が任意の1文字にマッチしていた
        assert rank1_regex.match("wxixnxgx") is None

    def test_no_manual_escape_in_keywords(self):
        # keyword.toml に手動エスケープの `\` を混入させない（文字として解釈されマッチしなくなる）
        assert not any("\\" in word for word in rank1 + rank2 + rank1_surface)


class TestSurfaceMatch:
    """表面形マッチ層（rank1_surface）のテスト。

    Sudachi の正規化形マッチでは誤検出する語（seeds → シーズ）を、
    書かれたままの表面形で判定する。
    """

    def test_surface_form_matches(self):
        assert match_shiny_colors("シーズの新曲が出た") is True

    def test_alias_surface_matches(self):
        assert match_shiny_colors("SHHisの新曲が出た") is True

    def test_normalized_collision_not_matched(self):
        # "seeds" の正規化形は「シーズ」だが、表面形が異なるため拾わない
        assert match_shiny_colors("seedsを植えた") is False
        assert match_shiny_colors("Seedsが発芽した") is False

    def test_similar_word_not_matched(self):
        assert match_shiny_colors("シーズンの変わり目") is False


class TestUserDictionary:
    """ユーザー辞書（user.csv）による正規化のテスト。

    conftest.py が scripts/build_user_dict.py で辞書を組み込むため、
    本番（Docker）と同じマッチング挙動をローカルでも検証できる。
    """

    def test_english_alias_normalized(self):
        # user.csv: shiny colors → シャイニーカラーズ（rank1）
        assert match_shiny_colors("shiny colorsのイベントに参加した") is True

    def test_multi_word_song_title(self):
        # user.csv がないと複数語の楽曲名は1トークンにならずマッチしない
        assert match_shiny_colors("dye the sky.を聴いた") is True

    def test_normalized_form_matches_rank1_entry(self):
        # かつて user.csv の正規化先が「borderline」（rank2）になっており、
        # rank1 登録済みでも単独でマッチしなかった（正規化先の書き誤り）
        assert match_shiny_colors("カウントダウンラブを聴いた") is True

    def test_borderline_alone_stays_rank2(self):
        # 上記修正で borderline 単独（rank2）の挙動は変えない
        assert match_shiny_colors("borderlineな気分") is False


class TestKeywordTomlValidation:
    """keyword.toml のバリデーション（CI で検証される）"""

    def test_no_duplicates(self):
        # マッチングは IGNORECASE のため小文字化して比較する
        for words in (rank1, rank2, rank1_surface):
            lowered = [word.lower() for word in words]
            assert len(lowered) == len(set(lowered))

    def test_no_overlap_between_ranks(self):
        lowered1 = {word.lower() for word in rank1}
        lowered2 = {word.lower() for word in rank2}
        lowered_surface = {word.lower() for word in rank1_surface}
        assert not lowered1 & lowered2
        assert not lowered1 & lowered_surface
        assert not lowered2 & lowered_surface
