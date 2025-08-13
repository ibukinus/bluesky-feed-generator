from datetime import datetime
from typing import Optional

from server import config
from server.database import Post
from server.logger import logger

uri = config.SHINY_URI
CURSOR_EOF = 'eof'


def handler(cursor: Optional[str], limit: int) -> dict:
    posts = Post.select().order_by(Post.indexed_at.desc(), Post.cid.desc()).limit(limit)

    if cursor:
        if cursor == CURSOR_EOF:
            return {
                'cursor': CURSOR_EOF,
                'feed': []
            }
        cursor_parts = cursor.split('::')
        if len(cursor_parts) != 2:
            raise ValueError('Malformed cursor')

        try:
            indexed_at, cid = cursor_parts
            indexed_at = datetime.fromtimestamp(int(indexed_at) / 1000)
            posts = posts.where(((Post.indexed_at == indexed_at) & (Post.cid < cid)) | (Post.indexed_at < indexed_at))
        except (ValueError, OSError) as e:
            raise ValueError(f'Invalid cursor format: {e}') from e

    # クエリを実行してリストに変換（遅延評価を避ける）
    try:
        posts_list = list(posts)
    except Exception as e:
        logger.error(f'Database query failed: {e}')
        return {
            'cursor': CURSOR_EOF,
            'feed': []
        }

    feed = [{'post': post.uri} for post in posts_list]

    cursor = CURSOR_EOF
    if posts_list:
        last_post = posts_list[-1]
        cursor = f'{int(last_post.indexed_at.timestamp() * 1000)}::{last_post.cid}'

    return {
        'cursor': cursor,
        'feed': feed
    }
