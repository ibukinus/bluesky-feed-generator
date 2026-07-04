import datetime
from unittest.mock import patch

from atproto import models

from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from server.database import db, Post
from server.data_filter import operations_callback
from server.ingest import (
    RECONNECT_REWIND_US,
    build_url,
    cleanup_old_posts,
    ops_from_event,
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
