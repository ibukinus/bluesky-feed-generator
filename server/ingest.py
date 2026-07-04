"""Jetstream から投稿イベントを購読し、フィルタリングして DB へ保存する常駐プロセス。

Flask（配信 API）とは別プロセスで動かす（compose の ingest サービス）。

使い方: python -m server.ingest
"""
import json
import signal
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from atproto import models
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

from server import config
from server.data_filter import operations_callback
from server.database import Post, SubscriptionState
from server.logger import logger

# SubscriptionState のキー。旧 Firehose のカーソル（シーケンス番号）とは
# 単位が異なる（Jetstream は time_us）ため、別キーで保存する。
SERVICE_NAME = 'jetstream'

CURSOR_SAVE_INTERVAL_US = 5_000_000  # 約5秒ごとにカーソルを永続化
RECONNECT_REWIND_US = 5_000_000  # 再接続時に5秒巻き戻して取りこぼしを防ぐ（重複は insert 時に排除）
RECONNECT_DELAY_SECONDS = 5
CLEANUP_INTERVAL_SECONDS = 3600  # 保持期限切れ投稿の削除間隔


def build_url(cursor: Optional[int]) -> str:
    params = {'wantedCollections': models.ids.AppBskyFeedPost}
    if cursor:
        params['cursor'] = str(max(cursor - RECONNECT_REWIND_US, 0))
    return f'{config.JETSTREAM_ENDPOINT}?{urllib.parse.urlencode(params)}'


def ops_from_event(event: dict) -> Optional[defaultdict]:
    """Jetstream の commit イベントを operations_callback の入力形式に変換する。

    対象外のイベント（commit 以外・投稿以外・update 等）は None を返す。
    """
    if event.get('kind') != 'commit':
        return None
    commit = event.get('commit', {})
    if commit.get('collection') != models.ids.AppBskyFeedPost:
        return None

    operation = commit.get('operation')
    uri = f"at://{event['did']}/{commit['collection']}/{commit['rkey']}"
    ops = defaultdict(lambda: {'created': [], 'deleted': []})

    if operation == 'create':
        record = models.get_or_create(commit.get('record'), strict=False)
        if record is None or not models.is_record_type(record, models.AppBskyFeedPost):
            logger.debug(f'投稿レコードとして解釈できないためスキップ: {uri}')
            return None
        ops[models.ids.AppBskyFeedPost]['created'].append({
            'record': record,
            'uri': uri,
            'cid': commit.get('cid'),
            'author': event['did'],
        })
    elif operation == 'delete':
        ops[models.ids.AppBskyFeedPost]['deleted'].append({'uri': uri})
    else:
        return None

    return ops


def cleanup_old_posts() -> int:
    """保持期限（FEEDGEN_POST_RETENTION_DAYS）を過ぎた投稿を削除する。"""
    if not config.POST_RETENTION_DAYS:
        return 0
    threshold = datetime.utcnow() - timedelta(days=config.POST_RETENTION_DAYS)
    deleted = Post.delete().where(Post.indexed_at < threshold).execute()
    if deleted:
        logger.info(f'保持期限切れの投稿を削除しました: {deleted}件')
    return deleted


def _load_cursor() -> Optional[int]:
    state = SubscriptionState.get_or_none(SubscriptionState.service == SERVICE_NAME)
    if state is None:
        SubscriptionState.create(service=SERVICE_NAME, cursor=0)
        return None
    return state.cursor or None


def should_reset_cursor(exc: Exception) -> bool:
    """接続失敗時にカーソルを破棄すべきか判定する。

    Jetstream は不正なカーソルをハンドシェイクの 400 Bad Request で拒否するため、
    その場合のみ破棄してライブテールから再開する。一時的な障害
    （DNS/TLS/切断/429 レート制限/5xx）ではカーソルを保持し、
    再生可能な範囲の取りこぼしを防ぐ。
    """
    return isinstance(exc, InvalidStatus) and exc.response.status_code == 400


def _save_cursor(time_us: int) -> None:
    SubscriptionState.update(cursor=time_us).where(
        SubscriptionState.service == SERVICE_NAME
    ).execute()


def run() -> None:
    cursor = _load_cursor()
    last_cleanup = 0.0

    while True:
        url = build_url(cursor)
        try:
            with connect(url) as ws:
                logger.info(f'Jetstream に接続しました: {config.JETSTREAM_ENDPOINT} (cursor={cursor})')
                last_saved_us = 0
                for message in ws:
                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError as e:
                        logger.error(f'イベントの JSON 解析に失敗しました: {e}')
                        continue

                    ops = ops_from_event(event)
                    if ops is not None:
                        try:
                            operations_callback(ops)
                        except Exception as e:
                            logger.error(f'投稿の処理に失敗しました: {e}')

                    time_us = event.get('time_us')
                    if time_us and time_us - last_saved_us >= CURSOR_SAVE_INTERVAL_US:
                        _save_cursor(time_us)
                        last_saved_us = time_us
                        cursor = time_us

                        if time.monotonic() - last_cleanup >= CLEANUP_INTERVAL_SECONDS or not last_cleanup:
                            cleanup_old_posts()
                            last_cleanup = time.monotonic()
        except (WebSocketException, OSError) as e:
            # ハンドシェイク拒否（古いカーソル等）も含めて捕捉し、常駐プロセスを止めない
            if cursor and should_reset_cursor(e):
                logger.warning(
                    f'ハンドシェイクが拒否されました: {e}。'
                    'カーソルを破棄してライブテールから再開します。'
                )
                cursor = None
            logger.error(
                f'Jetstream 接続に失敗しました: {e}。{RECONNECT_DELAY_SECONDS}秒後に再接続します。'
            )
            time.sleep(RECONNECT_DELAY_SECONDS)


def _shutdown_handler(*_):
    raise SystemExit(0)


def main() -> None:
    # カーソルは定期保存のため、強制終了しても再接続時の巻き戻しで復旧できる
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    run()


if __name__ == '__main__':
    main()
