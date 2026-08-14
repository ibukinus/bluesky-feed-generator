from scripts.rewind_cursor import REPLAY_WINDOW_HOURS, compute_target

HOUR_US = 3600 * 1_000_000


class TestComputeTarget:
    """ホスト切り替え時のカーソル巻き戻し"""

    def test_rewinds_from_now(self):
        # 遅れていたホストのカーソルは実時刻付近を指しているので、実時刻から巻き戻す
        now = 100 * HOUR_US
        assert compute_target(cursor=now, hours=7, now_us=now) == now - 7 * HOUR_US

    def test_does_not_advance_older_cursor(self):
        # ingest が長時間止まっていた場合、カーソルを前進させると取りこぼしが増える
        now = 100 * HOUR_US
        old_cursor = now - 20 * HOUR_US
        assert compute_target(cursor=old_cursor, hours=7, now_us=now) == old_cursor

    def test_boundary_keeps_cursor(self):
        now = 100 * HOUR_US
        cursor = now - 7 * HOUR_US
        assert compute_target(cursor=cursor, hours=7, now_us=now) == cursor

    def test_replay_window_is_documented_limit(self):
        # Jetstream のライブソケットの lookback（36時間）を超える巻き戻しは再生できない
        assert REPLAY_WINDOW_HOURS == 36
