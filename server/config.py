import os

SERVICE_DID = os.environ.get('SERVICE_DID', None)
HOSTNAME = os.environ.get('HOSTNAME', None)

if HOSTNAME is None:
    raise RuntimeError('You should set "HOSTNAME" environment variable first.')

if SERVICE_DID is None:
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
