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

FEEDGEN_SQLITE_LOCATION = os.environ.get('FEEDGEN_SQLITE_LOCATION')

EXCLUDED_DID = os.environ.get('EXCLUDED_DID', '')
EXCLUDED_DID_LIST = EXCLUDED_DID.split(';')

PRIORITY_DID = os.environ.get('PRIORITY_DID', '')
PRIORITY_DID_LIST = PRIORITY_DID.split(';')

def _get_bool_env_var(value: str) -> bool:
    if value is None:
        return False

    normalized_value = value.strip().lower()
    if normalized_value in {'1', 'true', 't', 'yes', 'y'}:
        return True

    return False


IGNORE_ARCHIVED_POSTS = _get_bool_env_var(os.environ.get('IGNORE_ARCHIVED_POSTS'))
IGNORE_REPLY_POSTS = _get_bool_env_var(os.environ.get('IGNORE_REPLY_POSTS'))
