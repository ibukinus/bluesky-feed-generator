import datetime
from unittest.mock import patch, MagicMock

import pytest

from server.database import db, Post


@pytest.fixture
def client():
    with patch('server.data_stream.run'):
        from server.app import app
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c


class TestIndex:
    def test_index(self, client):
        response = client.get('/')
        assert response.status_code == 200
        assert b'ATProto Feed Generator' in response.data


class TestDidJson:
    def test_did_json_matching_hostname(self, client):
        from server import config
        config.SERVICE_DID = f'did:web:{config.HOSTNAME}'
        response = client.get('/.well-known/did.json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['id'] == config.SERVICE_DID
        assert data['@context'] == ['https://www.w3.org/ns/did/v1']
        assert len(data['service']) == 1
        assert data['service'][0]['type'] == 'BskyFeedGenerator'

    def test_did_json_non_matching_hostname(self, client):
        from server import config
        original = config.SERVICE_DID
        config.SERVICE_DID = 'did:web:other.example.com'
        response = client.get('/.well-known/did.json')
        assert response.status_code == 404
        config.SERVICE_DID = original


class TestDescribeFeedGenerator:
    def test_describe(self, client):
        response = client.get('/xrpc/app.bsky.feed.describeFeedGenerator')
        assert response.status_code == 200
        data = response.get_json()
        assert 'body' in data
        assert 'feeds' in data['body']
        assert 'did' in data['body']


class TestGetFeedSkeleton:
    def setup_method(self):
        db.create_tables([Post], safe=True)
        Post.delete().execute()

    def test_valid_feed(self, client):
        from server import config
        Post.create(uri='at://did:plc:test/post/1', cid='cid1')
        response = client.get(f'/xrpc/app.bsky.feed.getFeedSkeleton?feed={config.SHINY_URI}')
        assert response.status_code == 200
        data = response.get_json()
        assert 'feed' in data
        assert 'cursor' in data

    def test_unsupported_algo(self, client):
        response = client.get('/xrpc/app.bsky.feed.getFeedSkeleton?feed=at://invalid/uri')
        assert response.status_code == 400

    def test_with_limit(self, client):
        from server import config
        for i in range(5):
            Post.create(uri=f'at://did:plc:test/post/{i}', cid=f'cid{i}')
        response = client.get(
            f'/xrpc/app.bsky.feed.getFeedSkeleton?feed={config.SHINY_URI}&limit=2'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['feed']) == 2

    def test_malformed_cursor(self, client):
        from server import config
        response = client.get(
            f'/xrpc/app.bsky.feed.getFeedSkeleton?feed={config.SHINY_URI}&cursor=bad'
        )
        assert response.status_code == 400

    def test_overflow_cursor_returns_400(self, client):
        from server import config
        cursor = '9' * 25 + '::cid1'
        response = client.get(
            f'/xrpc/app.bsky.feed.getFeedSkeleton?feed={config.SHINY_URI}&cursor={cursor}'
        )
        assert response.status_code == 400

    def test_limit_clamped_to_100(self, client):
        from server import config
        for i in range(105):
            Post.create(uri=f'at://did:plc:test/post/{i}', cid=f'cid{i:03d}')
        response = client.get(
            f'/xrpc/app.bsky.feed.getFeedSkeleton?feed={config.SHINY_URI}&limit=100000'
        )
        assert response.status_code == 200
        assert len(response.get_json()['feed']) == 100

    def test_limit_clamped_to_minimum_1(self, client):
        from server import config
        for i in range(5):
            Post.create(uri=f'at://did:plc:test/post/{i}', cid=f'cid{i}')
        response = client.get(
            f'/xrpc/app.bsky.feed.getFeedSkeleton?feed={config.SHINY_URI}&limit=0'
        )
        assert response.status_code == 200
        assert len(response.get_json()['feed']) == 1
