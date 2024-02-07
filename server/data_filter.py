from atproto import models
from atproto_client.models.app.bsky.embed.images import Image

from server.logger import logger
from server.database import db, Post
from server.matcher import match_shiny_colors


def operations_callback(ops: dict) -> None:
    # Here we can filter, process, run ML classification, etc.
    # After our feed alg we can save posts into our DB
    # Also, we should process deleted posts to remove them from our DB and keep it in sync

    # for example, let's create our custom feed that will contain all posts that contains alf related text

    posts_to_create = []
    for created_post in ops['posts']['created']:
        record = created_post['record']

        # Post languageで日本語が設定されていない投稿を除外する
        langs = record['langs']
        if  langs is None or not 'ja' in langs:
            continue

        # 本文に収集対象のワードが含まれるか
        is_match = match_shiny_colors(record.text)

        # 画像のALTテキストに収集対象のワードが含まれるか
        if record['embed'] is not None and record['embed']['py_type'] == 'app.bsky.embed.images':
            images: list[Image] = record['embed']['images']
            for image in images:
                if match_shiny_colors(image.alt):
                    is_match = True
                    break

        if is_match:
            reply_parent = None
            if record.reply and record.reply.parent.uri:
                reply_parent = record.reply.parent.uri

            reply_root = None
            if record.reply and record.reply.root.uri:
                reply_root = record.reply.root.uri

            post_dict = {
                'uri': created_post['uri'],
                'cid': created_post['cid'],
                'reply_parent': reply_parent,
                'reply_root': reply_root,
            }
            posts_to_create.append(post_dict)

    posts_to_delete = [p['uri'] for p in ops['posts']['deleted']]
    if posts_to_delete:
        Post.delete().where(Post.uri.in_(posts_to_delete))

    if posts_to_create:
        with db.atomic():
            for post_dict in posts_to_create:
                Post.create(**post_dict)
