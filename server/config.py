import os
import logging

from dotenv import load_dotenv

from server.logger import logger

load_dotenv()

SERVICE_DID = os.environ.get('SERVICE_DID')
HOSTNAME = os.environ.get('HOSTNAME')
FLASK_RUN_FROM_CLI = os.environ.get('FLASK_RUN_FROM_CLI')

if FLASK_RUN_FROM_CLI:
    logger.setLevel(logging.DEBUG)

if not HOSTNAME:
    raise RuntimeError('You should set "HOSTNAME" environment variable first.')

if not SERVICE_DID:
    SERVICE_DID = f'did:web:{HOSTNAME}'


SHINY_URI = os.environ.get('SHINY_URI')
if SHINY_URI is None:
    raise RuntimeError('Publish your feed first (run publish_feed.py) to obtain Feed URI. '
                       'Set this URI to "SHINY_URI" environment variable.')

FEEDGEN_SQLITE_LOCATION = os.environ.get('FEEDGEN_SQLITE_LOCATION', 'feed.db')

# v2 ホスト。v1 ワイヤ（/subscribe）も提供する。
# レガシーの jetstream1/2.us-east は凍結済みで、2026-08-14 に jetstream2.us-east が
# 約5時間遅れで配信する障害を起こしたため既定値から外した。
JETSTREAM_ENDPOINT = os.environ.get(
    'JETSTREAM_ENDPOINT', 'wss://jetstream.us-east.bsky.network/subscribe'
)

def _parse_retention_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        raise RuntimeError(
            f'"FEEDGEN_POST_RETENTION_DAYS" must be an integer, got: {value!r}'
        )
    # 負値は削除しきい値が未来になり全投稿が削除されてしまうため拒否する
    if days < 0:
        raise RuntimeError(
            f'"FEEDGEN_POST_RETENTION_DAYS" must be 0 or positive, got: {value!r}'
        )
    return days


# 収集した投稿の保持日数。0 で無期限。
POST_RETENTION_DAYS = _parse_retention_days(
    os.environ.get('FEEDGEN_POST_RETENTION_DAYS', '30')
)

EXCLUDED_DID = os.environ.get('EXCLUDED_DID', '')
EXCLUDED_DID_LIST = [did for did in EXCLUDED_DID.split(';') if did.strip()]

PRIORITY_DID = os.environ.get('PRIORITY_DID', '')
PRIORITY_DID_LIST = [did for did in PRIORITY_DID.split(';') if did.strip()]

def _get_bool_env_var(value: str) -> bool:
    if value is None:
        return False

    normalized_value = value.strip().lower()
    if normalized_value in {'1', 'true', 't', 'yes', 'y'}:
        return True

    return False


IGNORE_ARCHIVED_POSTS = _get_bool_env_var(os.environ.get('IGNORE_ARCHIVED_POSTS'))
IGNORE_REPLY_POSTS = _get_bool_env_var(os.environ.get('IGNORE_REPLY_POSTS'))
