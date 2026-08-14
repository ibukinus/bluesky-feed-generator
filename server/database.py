from datetime import datetime

import peewee

from server import config

# ingest（書き込み）と app（読み取り）の2プロセスが同じ DB を共有するため、
# WAL モードで並行アクセスを許可し、ロック競合時は待機する
db = peewee.SqliteDatabase(
    config.FEEDGEN_SQLITE_LOCATION,
    pragmas={'journal_mode': 'wal', 'busy_timeout': 5000},
)
db_version = 2

class BaseModel(peewee.Model):
    class Meta:
        database = db


class Post(BaseModel):
    uri = peewee.CharField(index=True)
    cid = peewee.CharField()
    reply_parent = peewee.CharField(null=True, default=None)
    reply_root = peewee.CharField(null=True, default=None)
    indexed_at = peewee.DateTimeField(default=datetime.utcnow)


class SubscriptionState(BaseModel):
    service = peewee.CharField(unique=True)
    cursor = peewee.BigIntegerField()


class IngestMeta(BaseModel):
    """ingest の起動をまたいで引き継ぐ文字列状態（購読ホスト等）"""
    key = peewee.CharField(unique=True)
    value = peewee.CharField()


if db.is_closed():
    db.connect()
    db.create_tables([Post, SubscriptionState, IngestMeta])
