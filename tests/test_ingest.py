import datetime
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
    RECONNECT_REWIND_US,
    SERVICE_NAME,
    STALENESS_MIN_SAMPLES,
    StalenessMonitor,
    _apply_endpoint_change,
    build_url,
    cleanup_old_posts,
    cursor_for_endpoint,
    cursor_lag_seconds,
    event_staleness_seconds,
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
    def test_without_cursor(self):
        url = build_url(None)
        assert 'wantedCollections=app.bsky.feed.post' in url
        assert 'cursor' not in url

    def test_with_cursor_rewinds(self):
        url = build_url(10_000_000 + RECONNECT_REWIND_US)
        assert 'cursor=10000000' in url


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
    def _feed(self, monitor, hours_behind, count=STALENESS_MIN_SAMPLES):
        now = datetime.datetime.now(datetime.UTC)
        created = (now - datetime.timedelta(hours=hours_behind)).isoformat()
        for _ in range(count):
            monitor.add(make_event_at(created, int(now.timestamp() * 1_000_000)))

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_warns_when_host_is_behind(self, mock_logger):
        self._feed(StalenessMonitor(), hours_behind=5)
        assert mock_logger.warning.called
        assert '5.0 時間' in mock_logger.warning.call_args[0][0]

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_healthy_host_is_silent(self, mock_logger):
        self._feed(StalenessMonitor(), hours_behind=0)
        assert not mock_logger.warning.called

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_recovery_logged_once(self, mock_logger):
        monitor = StalenessMonitor()
        self._feed(monitor, hours_behind=5)
        self._feed(monitor, hours_behind=0)
        self._feed(monitor, hours_behind=0)
        assert mock_logger.info.call_count == 1
        assert '解消' in mock_logger.info.call_args[0][0]

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_too_few_samples_does_not_report(self, mock_logger):
        self._feed(StalenessMonitor(), hours_behind=5, count=STALENESS_MIN_SAMPLES - 1)
        assert not mock_logger.warning.called

    @patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 0)
    @patch('server.ingest.logger')
    def test_interval_not_elapsed_does_not_report(self, mock_logger):
        with patch('server.ingest.STALENESS_CHECK_INTERVAL_SECONDS', 3600):
            self._feed(StalenessMonitor(), hours_behind=5)
        assert not mock_logger.warning.called


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

    @patch('server.ingest.config')
    def test_rewinds_and_records_endpoint(self, mock_config):
        mock_config.JETSTREAM_ENDPOINT = 'wss://new.example/subscribe'
        now_us = int(time.time() * 1_000_000)
        SubscriptionState.create(service=SERVICE_NAME, cursor=now_us)

        rewound = _apply_endpoint_change(now_us)

        assert rewound == pytest.approx(now_us - ENDPOINT_CHANGE_REWIND_US, abs=5_000_000)
        # 巻き戻したカーソルは永続化される（起動直後に落ちても失われない）
        assert SubscriptionState.get(SubscriptionState.service == SERVICE_NAME).cursor == rewound
        assert IngestMeta.get(IngestMeta.key == ENDPOINT_META_KEY).value == 'wss://new.example/subscribe'

    @patch('server.ingest.config')
    def test_unchanged_endpoint_is_noop(self, mock_config):
        mock_config.JETSTREAM_ENDPOINT = 'wss://same.example/subscribe'
        now_us = int(time.time() * 1_000_000)
        SubscriptionState.create(service=SERVICE_NAME, cursor=now_us)

        first = _apply_endpoint_change(now_us)
        second = _apply_endpoint_change(first)

        assert second == first
        assert SubscriptionState.get(SubscriptionState.service == SERVICE_NAME).cursor == first


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
