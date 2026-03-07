import os

os.environ['HOSTNAME'] = 'test.example.com'
os.environ['SHINY_URI'] = 'at://did:plc:test/app.bsky.feed.generator/shiny-colors'
os.environ['FEEDGEN_SQLITE_LOCATION'] = ':memory:'
