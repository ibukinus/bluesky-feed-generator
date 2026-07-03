import datetime

import pytest

from server.database import db, Post
from server.algos.shiny_colors import handler, CURSOR_EOF


class TestShinyColorsHandler:
    def setup_method(self):
        db.create_tables([Post], safe=True)
        Post.delete().execute()

    def test_empty_feed(self):
        result = handler(None, 20)
        assert result['cursor'] == CURSOR_EOF
        assert result['feed'] == []

    def test_returns_posts(self):
        Post.create(uri='at://did:plc:test/post/1', cid='cid1')
        Post.create(uri='at://did:plc:test/post/2', cid='cid2')

        result = handler(None, 20)
        assert len(result['feed']) == 2
        assert all('post' in item for item in result['feed'])

    def test_limit(self):
        for i in range(5):
            Post.create(uri=f'at://did:plc:test/post/{i}', cid=f'cid{i}')

        result = handler(None, 3)
        assert len(result['feed']) == 3

    def test_cursor_eof(self):
        result = handler(CURSOR_EOF, 20)
        assert result['cursor'] == CURSOR_EOF
        assert result['feed'] == []

    def test_cursor_pagination(self):
        posts = []
        base_time = datetime.datetime(2024, 1, 1, 12, 0, 0)
        for i in range(5):
            p = Post.create(
                uri=f'at://did:plc:test/post/{i}',
                cid=f'cid{i}',
                indexed_at=base_time + datetime.timedelta(seconds=i),
            )
            posts.append(p)

        first_page = handler(None, 3)
        assert len(first_page['feed']) == 3
        assert first_page['cursor'] != CURSOR_EOF

        second_page = handler(first_page['cursor'], 3)
        assert len(second_page['feed']) == 2

    def test_malformed_cursor_raises(self):
        with pytest.raises(ValueError, match='Malformed cursor'):
            handler('invalid_cursor', 20)

    def test_overflow_timestamp_cursor_raises_value_error(self):
        # timestamp が time_t の範囲を超えると OverflowError になるが、ValueError に変換される
        with pytest.raises(ValueError, match='Invalid cursor format'):
            handler('9999999999999999999999999::cid1', 20)

    def test_cursor_format(self):
        Post.create(uri='at://did:plc:test/post/1', cid='cid1')
        result = handler(None, 20)
        cursor = result['cursor']
        parts = cursor.split('::')
        assert len(parts) == 2
        int(parts[0])  # timestamp部分が数値であること

    def test_order_by_indexed_at_desc(self):
        base_time = datetime.datetime(2024, 1, 1, 12, 0, 0)
        Post.create(uri='at://did:plc:test/post/old', cid='cid1',
                    indexed_at=base_time)
        Post.create(uri='at://did:plc:test/post/new', cid='cid2',
                    indexed_at=base_time + datetime.timedelta(hours=1))

        result = handler(None, 20)
        assert result['feed'][0]['post'] == 'at://did:plc:test/post/new'
        assert result['feed'][1]['post'] == 'at://did:plc:test/post/old'
