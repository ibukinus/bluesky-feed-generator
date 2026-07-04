import pytest

from server.config import _get_bool_env_var, _parse_retention_days


class TestGetBoolEnvVar:
    def test_true_values(self):
        for val in ['1', 'true', 't', 'yes', 'y', 'True', 'TRUE', 'YES', 'Y', 'T']:
            assert _get_bool_env_var(val) is True

    def test_false_values(self):
        for val in ['0', 'false', 'f', 'no', 'n', '', 'random']:
            assert _get_bool_env_var(val) is False

    def test_none(self):
        assert _get_bool_env_var(None) is False

    def test_whitespace(self):
        assert _get_bool_env_var('  true  ') is True
        assert _get_bool_env_var('  1  ') is True


class TestParseRetentionDays:
    def test_valid_values(self):
        assert _parse_retention_days('30') == 30
        assert _parse_retention_days('0') == 0

    def test_negative_rejected(self):
        # 負値はしきい値が未来になり全投稿削除につながるため拒否する
        with pytest.raises(RuntimeError, match='0 or positive'):
            _parse_retention_days('-1')

    def test_non_integer_rejected(self):
        with pytest.raises(RuntimeError, match='integer'):
            _parse_retention_days('thirty')
