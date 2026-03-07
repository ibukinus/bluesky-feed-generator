from server.config import _get_bool_env_var


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
