from flask import Flask, jsonify, request

from server import config
from server.algos import algos

# Firehose/Jetstream の購読は別プロセス（server/ingest.py）が担う。
# このモジュールは配信 API のみを提供する。
app = Flask(__name__)


@app.route('/')
def index():
    return 'ATProto Feed Generator powered by The AT Protocol SDK for Python (https://github.com/MarshalX/atproto).'


@app.route('/.well-known/did.json', methods=['GET'])
def did_json():
    if not config.SERVICE_DID.endswith(config.HOSTNAME):
        return '', 404

    return jsonify({
        '@context': ['https://www.w3.org/ns/did/v1'],
        'id': config.SERVICE_DID,
        'service': [
            {
                'id': '#bsky_fg',
                'type': 'BskyFeedGenerator',
                'serviceEndpoint': f'https://{config.HOSTNAME}'
            }
        ]
    })


@app.route('/xrpc/app.bsky.feed.describeFeedGenerator', methods=['GET'])
def describe_feed_generator():
    feeds = [{'uri': uri} for uri in algos.keys()]
    response = {
        'encoding': 'application/json',
        'body': {
            'did': config.SERVICE_DID,
            'feeds': feeds
        }
    }
    return jsonify(response)


@app.route('/xrpc/app.bsky.feed.getFeedSkeleton', methods=['GET'])
def get_feed_skeleton():
    feed = request.args.get('feed', default=None, type=str)
    algo = algos.get(feed)
    if not algo:
        return 'Unsupported algorithm', 400

    # Example of how to check auth if giving user-specific results:
    """
    from server.auth import AuthorizationError, validate_auth
    try:
        requester_did = validate_auth(request)
    except AuthorizationError:
        return 'Unauthorized', 401
    """

    try:
        cursor = request.args.get('cursor', default=None, type=str)
        limit = request.args.get('limit', default=20, type=int)
        # AT Protocol の仕様（1〜100）に合わせてクランプする
        limit = max(1, min(limit, 100))
        body = algo(cursor, limit)
    except ValueError:
        return 'Malformed cursor', 400

    return jsonify(body)
