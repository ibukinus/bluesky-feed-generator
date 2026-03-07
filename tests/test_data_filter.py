import datetime
from collections import defaultdict
from unittest.mock import MagicMock, patch

from atproto import models

from server.data_filter import is_archive_post, should_ignore_post, operations_callback
from server.database import db, Post


class TestIsArchivePost:
    def test_recent_post(self):
        record = MagicMock()
        record.created_at = datetime.datetime.now(datetime.UTC).isoformat()
        assert is_archive_post(record) is False

    def test_old_post(self):
        record = MagicMock()
        old_date = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
        record.created_at = old_date.isoformat()
        assert is_archive_post(record) is True

    def test_exactly_one_day_old(self):
        record = MagicMock()
        exactly_one_day = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1, seconds=1)
        record.created_at = exactly_one_day.isoformat()
        assert is_archive_post(record) is True


class TestShouldIgnorePost:
    def _make_post(self, created_at=None, reply=None):
        record = MagicMock()
        record.created_at = (created_at or datetime.datetime.now(datetime.UTC)).isoformat()
        record.reply = reply
        return {'record': record, 'uri': 'at://did:plc:test/app.bsky.feed.post/test'}

    def test_normal_post_not_ignored(self):
        assert should_ignore_post(self._make_post()) is False

    @patch('server.data_filter.config')
    def test_archived_post_ignored_when_enabled(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = True
        mock_config.IGNORE_REPLY_POSTS = False
        old_date = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
        assert should_ignore_post(self._make_post(created_at=old_date)) is True

    @patch('server.data_filter.config')
    def test_archived_post_not_ignored_when_disabled(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        old_date = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
        assert should_ignore_post(self._make_post(created_at=old_date)) is False

    @patch('server.data_filter.config')
    def test_reply_post_ignored_when_enabled(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = True
        reply = MagicMock()
        assert should_ignore_post(self._make_post(reply=reply)) is True

    @patch('server.data_filter.config')
    def test_reply_post_not_ignored_when_disabled(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        reply = MagicMock()
        assert should_ignore_post(self._make_post(reply=reply)) is False


class TestOperationsCallback:
    def setup_method(self):
        db.create_tables([Post], safe=True)
        Post.delete().execute()

    def _make_ops(self, created=None, deleted=None):
        ops = defaultdict(lambda: {'created': [], 'deleted': []})
        if created:
            ops[models.ids.AppBskyFeedPost]['created'] = created
        if deleted:
            ops[models.ids.AppBskyFeedPost]['deleted'] = deleted
        return ops

    def _make_created_post(self, uri='at://did:plc:test/app.bsky.feed.post/1', cid='cid1',
                           author='did:plc:author1', text='シャニマスの話', langs=None,
                           reply=None, embed=None):
        record = MagicMock()
        record.text = text
        record.langs = langs or ['ja']
        record.reply = reply
        record.embed = embed
        record.created_at = datetime.datetime.now(datetime.UTC).isoformat()
        return {'uri': uri, 'cid': cid, 'author': author, 'record': record}

    @patch('server.data_filter.config')
    def test_matching_post_created(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

        ops = self._make_ops(created=[self._make_created_post()])
        operations_callback(ops)

        assert Post.select().count() == 1
        post = Post.get()
        assert post.uri == 'at://did:plc:test/app.bsky.feed.post/1'
        assert post.cid == 'cid1'

    @patch('server.data_filter.config')
    def test_non_matching_post_not_created(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

        ops = self._make_ops(created=[self._make_created_post(text='今日の天気は晴れ')])
        operations_callback(ops)

        assert Post.select().count() == 0

    @patch('server.data_filter.config')
    def test_excluded_did_filtered(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = ['did:plc:excluded']
        mock_config.PRIORITY_DID_LIST = []

        ops = self._make_ops(created=[
            self._make_created_post(author='did:plc:excluded')
        ])
        operations_callback(ops)

        assert Post.select().count() == 0

    @patch('server.data_filter.config')
    def test_priority_did_always_included(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = ['did:plc:priority']

        ops = self._make_ops(created=[
            self._make_created_post(author='did:plc:priority', text='関係ない話題', langs=None)
        ])
        operations_callback(ops)

        assert Post.select().count() == 1

    @patch('server.data_filter.config')
    def test_non_japanese_post_filtered(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

        ops = self._make_ops(created=[
            self._make_created_post(text='シャニマス', langs=['en'])
        ])
        operations_callback(ops)

        assert Post.select().count() == 0

    @patch('server.data_filter.config')
    def test_no_langs_post_filtered(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

        post = self._make_created_post(text='シャニマス')
        post['record'].langs = None
        ops = self._make_ops(created=[post])
        operations_callback(ops)

        assert Post.select().count() == 0

    @patch('server.data_filter.config')
    def test_image_alt_text_matching(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

        embed = MagicMock()
        embed.py_type = 'app.bsky.embed.images'
        image = MagicMock()
        image.alt = 'シャニマスのスクショ'
        embed.images = [image]

        ops = self._make_ops(created=[
            self._make_created_post(text='画像です', embed=embed)
        ])
        operations_callback(ops)

        assert Post.select().count() == 1

    @patch('server.data_filter.config')
    def test_delete_post(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

        Post.create(uri='at://did:plc:test/app.bsky.feed.post/del1', cid='cid1')

        ops = self._make_ops(deleted=[{'uri': 'at://did:plc:test/app.bsky.feed.post/del1'}])
        operations_callback(ops)

        assert Post.select().count() == 0

    @patch('server.data_filter.config')
    def test_reply_post_stores_reply_info(self, mock_config):
        mock_config.IGNORE_ARCHIVED_POSTS = False
        mock_config.IGNORE_REPLY_POSTS = False
        mock_config.EXCLUDED_DID_LIST = []
        mock_config.PRIORITY_DID_LIST = []

        reply = MagicMock()
        reply.parent.uri = 'at://did:plc:test/app.bsky.feed.post/parent'
        reply.root.uri = 'at://did:plc:test/app.bsky.feed.post/root'

        ops = self._make_ops(created=[
            self._make_created_post(text='シャニマスの返信', reply=reply)
        ])
        operations_callback(ops)

        assert Post.select().count() == 1
        post = Post.get()
        assert post.reply_parent == 'at://did:plc:test/app.bsky.feed.post/parent'
        assert post.reply_root == 'at://did:plc:test/app.bsky.feed.post/root'
