import datetime
import json
import time
from unittest.mock import patch

import pytest
from atproto import models

from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from server.database import db, IngestMeta, Post, SubscriptionState
from server.data_filter import operations_callback
from server.ingest import (
    ENDPOINT_CHANGE_REWIND_US,
    ENDPOINT_META_KEY,
    FAILOVER_AFTER_FAILURES,
    PRIMARY_META_KEY,
    RECONNECT_REWIND_US,
    SERVICE_NAME,
    STALENESS_MIN_SAMPLES,
    EndpointRotator,
    StalenessMonitor,
    _apply_endpoint_change,
    _failover,
    build_url,
    cleanup_old_posts,
    cursor_for_endpoint,
    cursor_lag_seconds,
    event_staleness_seconds,
    initial_endpoint,
    ops_from_event,
    run,
    should_reset_cursor,
)


def make_create_event(text='シャニマスの話', langs=None, did='did:plc:author1',
                      rkey='3laaabbbccc2d', extra_record=None):
    """実際の Jetstream commit イベント（JSON デコード後）を模したテストデータ"""
    record = {
        '$type': 'app.bsky.feed.post',
        'createdAt': datetime.datetime.now(datetime.UTC).isoformat(),
        'text': text,
        'langs': langs if langs is not None else ['ja'],
    }
    if extra_record:
        record.update(extra_record)
    return {
        'did': did,
        'time_us': 1725911162329308,
        'kind': 'commit',
        'commit': {
            'rev': '3l3qo2vutsw2b',
            'operation': 'create',
            'collection': 'app.bsky.feed.post',
            'rkey': rkey,
            'record': record,
            'cid': 'bafyreidwytest1',
        },
    }


class TestOpsFromEvent:
    def test_create_event(self):
        ops = ops_from_event(make_create_event())
        created = ops[models.ids.AppBskyFeedPost]['created']
        assert len(created) == 1
        assert created[0]['uri'] == 'at://did:plc:author1/app.bsky.feed.post/3laaabbbccc2d'
        assert created[0]['cid'] == 'bafyreidwytest1'
        assert created[0]['author'] == 'did:plc:author1'
        assert created[0]['record'].text == 'シャニマスの話'
        assert created[0]['record'].langs == ['ja']

    def test_create_event_with_image_embed(self):
        # Jetstream の JSON では blob 参照は {"$link": ...} 形式で届く
        embed = {
            '$type': 'app.bsky.embed.images',
            'images': [{
                'alt': 'シャニマスのスクショ',
                'image': {
                    '$type': 'blob',
                    'ref': {'$link': 'bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku'},
                    'mimeType': 'image/jpeg',
                    'size': 100000,
                },
            }],
        }
        ops = ops_from_event(make_create_event(extra_record={'embed': embed}))
        record = ops[models.ids.AppBskyFeedPost]['created'][0]['record']
        assert record.embed.py_type == 'app.bsky.embed.images'
        assert record.embed.images[0].alt == 'シャニマスのスクショ'

    def test_create_event_with_reply(self):
        reply = {
            'parent': {'cid': 'bafyreiparent', 'uri': 'at://did:plc:x/app.bsky.feed.post/parent'},
            'root': {'cid': 'bafyreiroot', 'uri': 'at://did:plc:x/app.bsky.feed.post/root'},
        }
        ops = ops_from_event(make_create_event(extra_record={'reply': reply}))
        record = ops[models.ids.AppBskyFeedPost]['created'][0]['record']
        assert record.reply.parent.uri == 'at://did:plc:x/app.bsky.feed.post/parent'
        assert record.reply.root.uri == 'at://did:plc:x/app.bsky.feed.post/root'

    def test_delete_event(self):
        event = {
            'did': 'did:plc:author1',
            'time_us': 1725911162329308,
            'kind': 'commit',
            'commit': {
                'rev': '3l3qo2vutsw2b',
                'operation': 'delete',
                'collection': 'app.bsky.feed.post',
                'rkey': '3laaabbbccc2d',
            },
        }
        ops = ops_from_event(event)
        deleted = ops[models.ids.AppBskyFeedPost]['deleted']
        assert deleted == [{'uri': 'at://did:plc:author1/app.bsky.feed.post/3laaabbbccc2d'}]

    def test_identity_event_ignored(self):
        event = {'did': 'did:plc:author1', 'time_us': 1, 'kind': 'identity',
                 'identity': {'did': 'did:plc:author1', 'handle': 'test.bsky.social'}}
        assert ops_from_event(event) is None

    def test_other_collection_ignored(self):
        event = make_create_event()
        event['commit']['collection'] = 'app.bsky.feed.like'
        assert ops_from_event(event) is None

    def test_update_operation_ignored(self):
        event = make_create_event()
        event['commit']['operation'] = 'update'
        assert ops_from_event(event) is None


class TestBuildUrl:
    ENDPOINT = 'wss://jetstream.example/subscribe'

    def test_without_cursor(self):
        url = build_url(self.ENDPOINT, None)
        assert url.startswith(f'{self.ENDPOINT}?')
        assert 'wantedCollections=app.bsky.feed.post' in url
        assert 'cursor' not in url

    def test_with_cursor_rewinds(self):
        url = build_url(self.ENDPOINT, 10_000_000 + RECONNECT_REWIND_US)
        assert 'cursor=10000000' in url

    def test_uses_given_endpoint(self):
        # フェイルオーバー後は切り替え先の URL を組む
        url = build_url('wss://other.example/subscribe', None)
        assert url.startswith('wss://other.example/subscribe?')


class TestShouldResetCursor:
    """カーソル破棄はカーソル起因のハンドシェイク拒否（4xx）に限る"""

    def _invalid_status(self, status_code):
        return InvalidStatus(Response(status_code, 'reason', Headers()))

    def test_handshake_400_resets(self):
        assert should_reset_cursor(self._invalid_status(400)) is True

    def test_rate_limit_keeps_cursor(self):
        # 429 はカーソル起因ではないため保持する
        assert should_reset_cursor(self._invalid_status(429)) is False

    def test_handshake_5xx_keeps_cursor(self):
        # サーバー側の一時障害ではカーソルを保持する
        assert should_reset_cursor(self._invalid_status(503)) is False

    def test_network_error_keeps_cursor(self):
        # DNS/TCP レベルの一時障害ではカーソルを保持する
        assert should_reset_cursor(OSError('dns failure')) is False


def make_event_at(created_at: str, time_us: int) -> dict:
    """createdAt と time_us を指定した commit イベントを作る"""
    event = make_create_event()
    event['time_us'] = time_us
    event['commit']['record']['createdAt'] = created_at
    return event


class TestEventStalenessSeconds:
    """ホストの配信遅延は time_us と createdAt の差にだけ現れる"""

    def test_live_event_is_near_zero(self):
        now = datetime.datetime.now(datetime.UTC)
        event = make_event_at(now.isoformat(), int(now.timestamp() * 1_000_000))
        assert event_staleness_seconds(event) == pytest.approx(0, abs=1)

    def test_lagging_host_detected(self):
        # ホストが5時間遅れて配信している状態（time_us には現在時刻が入る）
        now = datetime.datetime.now(datetime.UTC)
        created = now - datetime.timedelta(hours=5)
        event = make_event_at(created.isoformat(), int(now.timestamp() * 1_000_000))
        assert event_staleness_seconds(event) == pytest.approx(5 * 3600, abs=1)

    def test_parses_z_suffix_and_extra_precision(self):
        # 実際に流れてくる 'Z' 表記や7桁以上の秒未満も解釈できる
        event = make_event_at('2026-08-13T21:14:11.97083400Z', 1786675904507992)
        assert event_staleness_seconds(event) == pytest.approx(20252.5, abs=1)  # 約5.6時間

    def test_missing_created_at(self):
        event = make_create_event()
        del event['commit']['record']['createdAt']
        assert event_staleness_seconds(event) is None

    def test_delete_event_has_no_record(self):
        event = {
            'did': 'did:plc:author1',
            'time_us': 1725911162329308,
            'kind': 'commit',
            'commit': {
                'operation': 'delete',
                'collection': 'app.bsky.feed.post',
                'rkey': '3laaabbbccc2d',
            },
        }
        assert event_staleness_seconds(event) is None

    def test_naive_created_at_ignored(self):
        # タイムゾーンなしは基準が不明なため判定に使わない
        assert event_staleness_seconds(make_event_at('2026-08-13 21:14:11', 1786675904507992)) is None

    def test_malformed_created_at_ignored(self):
        assert event_staleness_seconds(make_event_at('昨日', 1786675904507992)) is None


class TestStalenessMonitor:
    ENDPOINT = 'wss://jetstream.example/subscribe'

    def _feed(self, monitor, hours_behind, count=STALENESS_MIN_SAMPLES):
        """サンプルを流し込み、フェイルオーバーすべきと判定されたかを返す"""
        now = datetime.datetime.now(datetime.UTC)
        created = (now - datetime.timedelta(hours=hours_behind)).isoformat()
        should_failover = False
        for _ in range(count):
            if monitor.add(make_event_at(created, int(now.timestamp() * 1_000_000))):
                should_failover = True
        return should_failover

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_warns_when_host_is_behind(self, mock_logger):
        self._feed(StalenessMonitor(self.ENDPOINT), hours_behind=5)
        assert mock_logger.warning.called
        assert '5.0 時間' in mock_logger.warning.call_args[0][0]
        # 遅れているホスト名がログから分かること
        assert self.ENDPOINT in mock_logger.warning.call_args[0][0]

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_healthy_host_is_silent(self, mock_logger):
        self._feed(StalenessMonitor(self.ENDPOINT), hours_behind=0)
        assert not mock_logger.warning.called

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_recovery_logged_once(self, mock_logger):
        monitor = StalenessMonitor(self.ENDPOINT)
        self._feed(monitor, hours_behind=5)
        self._feed(monitor, hours_behind=0)
        self._feed(monitor, hours_behind=0)
        assert mock_logger.info.call_count == 1
        assert '解消' in mock_logger.info.call_args[0][0]

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_too_few_samples_does_not_report(self, mock_logger):
        self._feed(
            StalenessMonitor(self.ENDPOINT),
            hours_behind=5,
            count=STALENESS_MIN_SAMPLES - 1,
        )
        assert not mock_logger.warning.called

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_interval_not_elapsed_does_not_report(self, mock_logger):
        with patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 3600):
            self._feed(StalenessMonitor(self.ENDPOINT), hours_behind=5)
        assert not mock_logger.warning.called

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_severe_delay_requests_failover(self, _mock_logger):
        # 1時間の遅れは STALENESS_FAILOVER_SECONDS（30分）を超える
        assert self._feed(StalenessMonitor(self.ENDPOINT), hours_behind=1)

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_mild_delay_warns_without_failover(self, mock_logger):
        # 20分の遅れは警告のみ。一時的な遅れでホストを切り替えない
        assert not self._feed(StalenessMonitor(self.ENDPOINT), hours_behind=20 / 60)
        assert mock_logger.warning.called

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_healthy_host_does_not_request_failover(self, _mock_logger):
        assert not self._feed(StalenessMonitor(self.ENDPOINT), hours_behind=0)


class TestCursorLagSeconds:
    def test_none_when_cursor_unset(self):
        assert cursor_lag_seconds(None) is None
        assert cursor_lag_seconds(0) is None

    def test_lag_measured_against_wall_clock(self):
        assert cursor_lag_seconds(int((time.time() - 120) * 1_000_000)) == pytest.approx(120, abs=5)


HOUR_US = 3600 * 1_000_000


class TestCursorForEndpoint:
    """購読ホストが変わったときのカーソル巻き戻し"""

    NOW = 100 * HOUR_US

    def test_same_endpoint_keeps_cursor(self):
        assert cursor_for_endpoint(self.NOW, 'wss://a/subscribe', 'wss://a/subscribe', self.NOW) == self.NOW

    def test_changed_endpoint_rewinds(self):
        # 前のホストが遅れていた場合、カーソルは実時刻付近でも投稿は未収集
        target = cursor_for_endpoint(self.NOW, 'wss://a/subscribe', 'wss://b/subscribe', self.NOW)
        assert target == self.NOW - ENDPOINT_CHANGE_REWIND_US

    def test_missing_record_rewinds(self):
        # 記録がない環境（この仕組みの導入前）も既定値変更でホストが変わり得る
        target = cursor_for_endpoint(self.NOW, None, 'wss://b/subscribe', self.NOW)
        assert target == self.NOW - ENDPOINT_CHANGE_REWIND_US

    def test_no_cursor_stays_none(self):
        assert cursor_for_endpoint(None, None, 'wss://b/subscribe', self.NOW) is None

    def test_older_cursor_not_advanced(self):
        # ingest が長時間止まっていた場合にカーソルを前進させない
        old = self.NOW - 30 * HOUR_US
        assert cursor_for_endpoint(old, 'wss://a/subscribe', 'wss://b/subscribe', self.NOW) == old


class TestApplyEndpointChange:
    def setup_method(self):
        db.create_tables([SubscriptionState, IngestMeta], safe=True)
        SubscriptionState.delete().execute()
        IngestMeta.delete().execute()

    def test_rewinds_and_records_endpoint(self):
        now_us = int(time.time() * 1_000_000)
        SubscriptionState.create(service=SERVICE_NAME, cursor=now_us)

        rewound = _apply_endpoint_change(now_us, 'wss://new.example/subscribe')

        assert rewound == pytest.approx(now_us - ENDPOINT_CHANGE_REWIND_US, abs=5_000_000)
        # 巻き戻したカーソルは永続化される（起動直後に落ちても失われない）
        assert SubscriptionState.get(SubscriptionState.service == SERVICE_NAME).cursor == rewound
        assert IngestMeta.get(IngestMeta.key == ENDPOINT_META_KEY).value == 'wss://new.example/subscribe'

    def test_unchanged_endpoint_is_noop(self):
        now_us = int(time.time() * 1_000_000)
        SubscriptionState.create(service=SERVICE_NAME, cursor=now_us)

        first = _apply_endpoint_change(now_us, 'wss://same.example/subscribe')
        second = _apply_endpoint_change(first, 'wss://same.example/subscribe')

        assert second == first
        assert SubscriptionState.get(SubscriptionState.service == SERVICE_NAME).cursor == first


class TestEndpointRotator:
    ENDPOINTS = [
        'wss://first.example/subscribe',
        'wss://second.example/subscribe',
        'wss://third.example/subscribe',
    ]

    def test_starts_at_first_candidate(self):
        assert EndpointRotator(self.ENDPOINTS).current == self.ENDPOINTS[0]

    def test_advances_in_order(self):
        rotator = EndpointRotator(self.ENDPOINTS)
        assert rotator.advance() == self.ENDPOINTS[1]
        assert rotator.advance() == self.ENDPOINTS[2]

    def test_wraps_around(self):
        """全ホストが落ちていても再接続を続けられるよう打ち止めにしない"""
        rotator = EndpointRotator(self.ENDPOINTS)
        for _ in range(len(self.ENDPOINTS)):
            rotator.advance()
        assert rotator.current == self.ENDPOINTS[0]

    def test_single_endpoint_has_no_alternatives(self):
        rotator = EndpointRotator([self.ENDPOINTS[0]])
        assert not rotator.has_alternatives
        assert rotator.advance() == self.ENDPOINTS[0]

    def test_multiple_endpoints_have_alternatives(self):
        assert EndpointRotator(self.ENDPOINTS).has_alternatives

    def test_empty_endpoints_rejected(self):
        with pytest.raises(ValueError):
            EndpointRotator([])

    def test_starts_at_given_endpoint(self):
        rotator = EndpointRotator(self.ENDPOINTS, start_at=self.ENDPOINTS[1])
        assert rotator.current == self.ENDPOINTS[1]
        assert rotator.advance() == self.ENDPOINTS[2]

    def test_unknown_start_falls_back_to_first(self):
        # 候補から外された購読先が記録されていても止まらない
        rotator = EndpointRotator(self.ENDPOINTS, start_at='wss://gone.example/subscribe')
        assert rotator.current == self.ENDPOINTS[0]


class TestInitialEndpoint:
    ENDPOINTS = ['wss://first.example/subscribe', 'wss://second.example/subscribe']

    def test_resumes_automatic_failover_target(self):
        """自動で移った先は再起動後も引き継ぐ（毎回8時間巻き戻さないため）"""
        assert initial_endpoint(
            self.ENDPOINTS, self.ENDPOINTS[1], self.ENDPOINTS[0], self.ENDPOINTS[0]
        ) == self.ENDPOINTS[1]

    def test_manual_primary_change_wins(self):
        """第一候補を変えたら人の意図を優先して先頭から始める"""
        assert initial_endpoint(
            self.ENDPOINTS, self.ENDPOINTS[1], 'wss://old-primary.example/subscribe',
            self.ENDPOINTS[0],
        ) == self.ENDPOINTS[0]

    def test_no_record_starts_at_first(self):
        assert initial_endpoint(self.ENDPOINTS, None, None, self.ENDPOINTS[0]) == self.ENDPOINTS[0]

    def test_saved_endpoint_no_longer_a_candidate(self):
        assert initial_endpoint(
            self.ENDPOINTS, 'wss://gone.example/subscribe', self.ENDPOINTS[0], self.ENDPOINTS[0]
        ) == self.ENDPOINTS[0]


class TestFailover:
    ENDPOINTS = ['wss://first.example/subscribe', 'wss://second.example/subscribe']

    def setup_method(self):
        db.create_tables([SubscriptionState, IngestMeta], safe=True)
        SubscriptionState.delete().execute()
        IngestMeta.delete().execute()

    @patch('server.ingest.logger')
    def test_switch_rewinds_cursor(self, _mock_logger):
        """前のホストが遅れていた可能性があるため、理由によらず巻き戻す"""
        rotator = EndpointRotator(self.ENDPOINTS)
        now_us = int(time.time() * 1_000_000)
        SubscriptionState.create(service=SERVICE_NAME, cursor=now_us)

        cursor = _failover(rotator, now_us, '3回連続で購読に失敗したため')

        assert cursor == pytest.approx(now_us - ENDPOINT_CHANGE_REWIND_US, abs=5_000_000)
        assert rotator.current == self.ENDPOINTS[1]
        assert SubscriptionState.get(SubscriptionState.service == SERVICE_NAME).cursor == cursor
        assert IngestMeta.get(IngestMeta.key == ENDPOINT_META_KEY).value == self.ENDPOINTS[1]

    @patch('server.ingest.logger')
    def test_old_cursor_not_advanced(self, _mock_logger):
        """長く繋がらなかった後の切り替えでカーソルを前進させない"""
        rotator = EndpointRotator(self.ENDPOINTS)
        stale_us = int(time.time() * 1_000_000) - 55 * 3600 * 1_000_000
        SubscriptionState.create(service=SERVICE_NAME, cursor=stale_us)

        assert _failover(rotator, stale_us, '3回連続で購読に失敗したため') == stale_us

    @patch('server.ingest.logger')
    def test_single_endpoint_does_not_switch(self, mock_logger):
        rotator = EndpointRotator([self.ENDPOINTS[0]])
        now_us = int(time.time() * 1_000_000)

        cursor = _failover(rotator, now_us, '3回連続で購読に失敗したため')

        assert cursor == now_us
        assert rotator.current == self.ENDPOINTS[0]
        assert not mock_logger.warning.called
        # 切り替えていないので記録も残らない
        assert IngestMeta.get_or_none(IngestMeta.key == ENDPOINT_META_KEY) is None


class TestRunReconnectsOnSilence:
    """サーバが黙り込んだ場合に受信タイムアウトで繋ぎ直す"""

    class _SilentConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def recv(self, timeout=None):
            raise TimeoutError

    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_recv_timeout_triggers_reconnect(self, mock_connect, _mock_load_cursor):
        # 2回目の接続で SystemExit を送って再接続ループを抜ける
        mock_connect.side_effect = [self._SilentConnection(), SystemExit]
        with pytest.raises(SystemExit):
            run()
        assert mock_connect.call_count == 2


@patch('server.ingest.RECONNECT_DELAY_SECONDS', 0)
class TestRunFailover:
    """繋がらない・黙り込むホストから自動で別ホストへ移る"""

    ENDPOINTS = ['wss://first.example/subscribe', 'wss://second.example/subscribe']

    class _SilentConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def recv(self, timeout=None):
            raise TimeoutError

    class _OneEventConnection:
        """イベントを1件返してから黙り込む"""

        def __init__(self):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def recv(self, timeout=None):
            if self._sent:
                raise TimeoutError
            self._sent = True
            # フィード対象外のテキストにして DB への書き込みを避ける
            return json.dumps(make_create_event(text='今日はいい天気'))

    def setup_method(self):
        db.create_tables([SubscriptionState, IngestMeta], safe=True)
        SubscriptionState.delete().execute()
        IngestMeta.delete().execute()

    def _endpoints_used(self, mock_connect):
        return [call.args[0].split('?')[0] for call in mock_connect.call_args_list]

    def _configure(self, mock_config, endpoints):
        mock_config.JETSTREAM_ENDPOINTS = endpoints
        mock_config.JETSTREAM_ENDPOINT = endpoints[0]
        # イベント受信で走る cleanup_old_posts が MagicMock を掴まないよう実値を入れる
        mock_config.POST_RETENTION_DAYS = 0

    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_switches_after_consecutive_failures(self, mock_connect, _cursor, mock_config):
        self._configure(mock_config, self.ENDPOINTS)
        failure = OSError('接続できません')
        # 閾値ちょうどで失敗させ、次の接続先を確かめてから抜ける
        mock_connect.side_effect = [failure] * FAILOVER_AFTER_FAILURES + [SystemExit]

        with pytest.raises(SystemExit):
            run()

        used = self._endpoints_used(mock_connect)
        assert used[:FAILOVER_AFTER_FAILURES] == [self.ENDPOINTS[0]] * FAILOVER_AFTER_FAILURES
        assert used[FAILOVER_AFTER_FAILURES] == self.ENDPOINTS[1]

    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_switches_after_consecutive_silence(self, mock_connect, _cursor, mock_config):
        """ハンドシェイクは通るのにイベントを流さないホストからも移る"""
        self._configure(mock_config, self.ENDPOINTS)
        mock_connect.side_effect = (
            [self._SilentConnection() for _ in range(FAILOVER_AFTER_FAILURES)] + [SystemExit]
        )

        with pytest.raises(SystemExit):
            run()

        assert self._endpoints_used(mock_connect)[FAILOVER_AFTER_FAILURES] == self.ENDPOINTS[1]

    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_received_event_resets_failures(self, mock_connect, _cursor, mock_config):
        """一度でもイベントが届いたら数え直す（散発的な切断で切り替えない）"""
        self._configure(mock_config, self.ENDPOINTS)
        failure = OSError('接続できません')
        mock_connect.side_effect = [
            failure,                     # 1回目
            failure,                     # 2回目
            self._OneEventConnection(),  # イベントが届いて数え直し（その後の無音で1回目）
            failure,                     # 2回目
            SystemExit,
        ]

        with pytest.raises(SystemExit):
            run()

        # 数え直しが無ければ4回目で閾値に達して切り替わっていた
        assert self._endpoints_used(mock_connect) == [self.ENDPOINTS[0]] * 5

    class _StaleConnection:
        """大きく遅れたイベントを流し続ける（判定に要る件数を返してから抜ける）"""

        def __init__(self, count):
            self._remaining = count

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def recv(self, timeout=None):
            if self._remaining <= 0:
                raise SystemExit
            self._remaining -= 1
            now = datetime.datetime.now(datetime.UTC)
            created = (now - datetime.timedelta(hours=5)).isoformat()
            event = make_event_at(created, int(now.timestamp() * 1_000_000))
            event['commit']['record']['text'] = '今日はいい天気'
            return json.dumps(event)

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_staleness_switches_endpoint(self, mock_connect, _cursor, mock_config):
        self._configure(mock_config, self.ENDPOINTS)
        mock_connect.side_effect = [
            self._StaleConnection(STALENESS_MIN_SAMPLES), SystemExit,
        ]

        with pytest.raises(SystemExit):
            run()

        assert self._endpoints_used(mock_connect) == [self.ENDPOINTS[0], self.ENDPOINTS[1]]

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_staleness_without_alternatives_stays_connected(
        self, mock_connect, _cursor, mock_config
    ):
        """切り替え先が無ければ、遅れていても繋ぎ直さない（無駄な再接続を避ける）"""
        self._configure(mock_config, [self.ENDPOINTS[0]])
        # 判定に要る件数の倍を流しても、接続は張り替えられない
        mock_connect.side_effect = [self._StaleConnection(STALENESS_MIN_SAMPLES * 2)]

        with pytest.raises(SystemExit):
            run()

        assert mock_connect.call_count == 1

    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_records_primary_on_start(self, mock_connect, _cursor, mock_config):
        """次回起動時に手動変更と自動切り替えを見分けられるよう第一候補を残す"""
        self._configure(mock_config, self.ENDPOINTS)
        mock_connect.side_effect = [SystemExit]

        with pytest.raises(SystemExit):
            run()

        assert IngestMeta.get(IngestMeta.key == PRIMARY_META_KEY).value == self.ENDPOINTS[0]

    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_resumes_from_failover_target(self, mock_connect, _cursor, mock_config):
        """前回自動で移った先から始める（落ちたホストへ繋ぎ直さない）"""
        self._configure(mock_config, self.ENDPOINTS)
        IngestMeta.replace(key=ENDPOINT_META_KEY, value=self.ENDPOINTS[1]).execute()
        IngestMeta.replace(key=PRIMARY_META_KEY, value=self.ENDPOINTS[0]).execute()
        mock_connect.side_effect = [SystemExit]

        with pytest.raises(SystemExit):
            run()

        assert self._endpoints_used(mock_connect) == [self.ENDPOINTS[1]]

    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_interrupted_start_still_honours_primary(self, mock_connect, _cursor, mock_config):
        """起動途中で落ちて記録が片方だけ残っても、設定した第一候補を無視しない"""
        self._configure(mock_config, self.ENDPOINTS)
        # 購読先だけ新しい第一候補に更新され、第一候補の記録が古いまま残った状態
        IngestMeta.replace(key=ENDPOINT_META_KEY, value=self.ENDPOINTS[0]).execute()
        IngestMeta.replace(key=PRIMARY_META_KEY, value='wss://old-primary.example/subscribe').execute()
        mock_connect.side_effect = [SystemExit]

        with pytest.raises(SystemExit):
            run()

        assert self._endpoints_used(mock_connect) == [self.ENDPOINTS[0]]

    @patch('server.ingest.config')
    @patch('server.ingest._load_cursor', return_value=None)
    @patch('server.ingest.connect')
    def test_single_endpoint_keeps_retrying(self, mock_connect, _cursor, mock_config):
        """候補が1つしかない設定では切り替えず再接続を続ける"""
        self._configure(mock_config, [self.ENDPOINTS[0]])
        failure = OSError('接続できません')
        mock_connect.side_effect = [failure] * (FAILOVER_AFTER_FAILURES + 1) + [SystemExit]

        with pytest.raises(SystemExit):
            run()

        assert set(self._endpoints_used(mock_connect)) == {self.ENDPOINTS[0]}


class TestEventToDatabase:
    """Jetstream イベント → operations_callback → DB 保存の結合テスト"""

    def setup_method(self):
        db.create_tables([Post], safe=True)
        Post.delete().execute()

    def _configure(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

    @patch('server.data_filter.config')
    def test_matching_post_stored(self, mock_config):
        self._configure(mock_config)
        operations_callback(ops_from_event(make_create_event()))
        assert Post.select().count() == 1
        assert Post.get().uri == 'at://did:plc:author1/app.bsky.feed.post/3laaabbbccc2d'

    @patch('server.data_filter.config')
    def test_replayed_event_not_duplicated(self, mock_config):
        # カーソル巻き戻し再接続で同じイベントが再送されても重複登録しない
        self._configure(mock_config)
        event = make_create_event()
        operations_callback(ops_from_event(event))
        operations_callback(ops_from_event(event))
        assert Post.select().count() == 1

    @patch('server.data_filter.config')
    def test_delete_event_removes_post(self, mock_config):
        self._configure(mock_config)
        operations_callback(ops_from_event(make_create_event()))
        event = make_create_event()
        event['commit']['operation'] = 'delete'
        del event['commit']['record']
        operations_callback(ops_from_event(event))
        assert Post.select().count() == 0


class TestCleanupOldPosts:
    def setup_method(self):
        db.create_tables([Post], safe=True)
        Post.delete().execute()

    @patch('server.ingest.config')
    def test_deletes_posts_past_retention(self, mock_config):
        mock_config.POST_RETENTION_DAYS = 30
        old = datetime.datetime.utcnow() - datetime.timedelta(days=31)
        Post.create(uri='at://old', cid='c1', indexed_at=old)
        Post.create(uri='at://new', cid='c2')

        assert cleanup_old_posts() == 1
        assert Post.select().count() == 1
        assert Post.get().uri == 'at://new'

    @patch('server.ingest.config')
    def test_disabled_when_zero(self, mock_config):
        mock_config.POST_RETENTION_DAYS = 0
        old = datetime.datetime.utcnow() - datetime.timedelta(days=365)
        Post.create(uri='at://old', cid='c1', indexed_at=old)

        assert cleanup_old_posts() == 0
        assert Post.select().count() == 1
