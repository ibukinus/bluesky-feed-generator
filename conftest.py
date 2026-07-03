import os
import sys
from pathlib import Path

os.environ['HOSTNAME'] = 'test.example.com'
os.environ['SHINY_URI'] = 'at://did:plc:test/app.bsky.feed.generator/shiny-colors'
os.environ['FEEDGEN_SQLITE_LOCATION'] = ':memory:'

# 本番（Docker）と同じユーザー辞書でマッチングをテストするため、テスト収集前にビルドする
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.build_user_dict import build_user_dict

build_user_dict()
