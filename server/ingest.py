"""Jetstream から投稿イベントを購読し、フィルタリングして DB へ保存する常駐プロセス。

Flask（配信 API）とは別プロセスで動かす（compose の ingest サービス）。

使い方: python -m server.ingest
"""
import json
import signal
import statistics
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
from server.database import IngestMeta, Post, SubscriptionState
from server.logger import logger

# SubscriptionState のキー。旧 Firehose のカーソル（シーケンス番号）とは
# 単位が異なる（Jetstream は time_us）ため、別キーで保存する。
SERVICE_NAME = 'jetstream'

CURSOR_SAVE_INTERVAL_US = 5_000_000  # 約5秒ごとにカーソルを永続化
RECONNECT_REWIND_US = 5_000_000  # 再接続時に5秒巻き戻して取りこぼしを防ぐ（重複は insert 時に排除）
RECONNECT_DELAY_SECONDS = 5
CLEANUP_INTERVAL_SECONDS = 3600  # 保持期限切れ投稿の削除間隔

# イベントが1件も届かないままこの秒数が過ぎたら停滞とみなして繋ぎ直す。
# 投稿ストリームは常時 60件/秒 以上流れているため、数十秒の無音は接続が死んだ兆候。
# これがないと、サーバが Pong だけ返して黙り込んだ場合に永久に気づけない。
RECV_TIMEOUT_SECONDS = 60
# websockets の受信バッファ（フレーム数）。既定の16は 60件/秒 のストリームでは
# 0.3秒分しかなく、一瞬の処理遅延で受信スレッドが socket.recv ごと停止して
# Pong を返せなくなり、自分で keepalive タイムアウトを起こす。
RECV_QUEUE_SIZE = 1024
# keepalive の応答待ち。既定の20秒はホスト側の瞬間的な停止で切れやすい。
PING_TIMEOUT_SECONDS = 60

# 配信遅延（イベントの time_us と投稿の createdAt の差）の監視。
# Jetstream ホストがネットワークから遅れると time_us には現在時刻が入ったまま
# 中身だけが古くなるため、カーソルの遅れでは検知できない（2026-08-14 の
# jetstream2.us-east の障害がこれ。docs/reports/2026-08-14-jetstream2-配信遅延.md）。
STALENESS_CHECK_INTERVAL_SECONDS = 300
STALENESS_WARN_SECONDS = 900
STALENESS_SAMPLE_SIZE = 500
STALENESS_MIN_SAMPLES = 30

# 自動フェイルオーバー。ホスト単位の障害から自力で復帰するための仕組み。
# 2026-08-20 の jetstream.us-east 停止では、5秒間隔の再接続を 55 時間続けても
# 復帰できなかった（docs/reports/2026-08-23-jetstream-us-east停止.md）。
# 連続でこの回数だけ接続に失敗したら次の候補ホストへ移る（5秒間隔なので約15秒）。
FAILOVER_AFTER_FAILURES = 3
# 配信遅延がこの秒数を超えたら次の候補ホストへ移る。警告だけ出す
# STALENESS_WARN_SECONDS より高くし、一時的な遅れで切り替えないようにする。
STALENESS_FAILOVER_SECONDS = 1800

# 購読ホストが前回起動時から変わっていたら、この時間だけカーソルを巻き戻す。
# 保存済みカーソルは前のホストが付けた time_us なので、そのホストが遅れていた
# 場合はカーソルが実時刻付近を指していても投稿は未収集で、巻き戻さないと
# 恒久的に読み飛ばす。再生分の重複は insert 時に排除される。
# これより長く戻す必要があるときは scripts/rewind_cursor.py を使う。
ENDPOINT_META_KEY = 'jetstream_endpoint'
# 直近の起動時の第一候補（JETSTREAM_ENDPOINT）。自動フェイルオーバーで移った先と、
# 人が設定を変えた結果とを区別するために記録する。
PRIMARY_META_KEY = 'jetstream_primary'
ENDPOINT_CHANGE_REWIND_US = 8 * 3600 * 1_000_000


def build_url(endpoint: str, cursor: Optional[int]) -> str:
    params = {'wantedCollections': models.ids.AppBskyFeedPost}
    if cursor:
        params['cursor'] = str(max(cursor - RECONNECT_REWIND_US, 0))
    return f'{endpoint}?{urllib.parse.urlencode(params)}'


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


def event_staleness_seconds(event: dict) -> Optional[float]:
    """イベントの time_us と投稿の createdAt の差（秒）を返す。

    Jetstream ホストがネットワークから遅れている場合、time_us にはホストが
    処理した時刻（＝ほぼ現在時刻）が入ったまま中身だけが古くなるため、
    カーソルの遅れでは検知できない。この差だけが遅延を映す。

    判定に使えないイベント（createdAt の欠落・解釈不能・タイムゾーン不明）は
    None を返す。
    """
    time_us = event.get('time_us')
    created_at = ((event.get('commit') or {}).get('record') or {}).get('createdAt')
    if not time_us or not isinstance(created_at, str):
        return None

    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    # タイムゾーンなしの値は基準が不明なため判定に使わない
    if created.tzinfo is None:
        return None

    return time_us / 1_000_000 - created.timestamp()


class EndpointRotator:
    """候補ホストを順に切り替える。

    末尾まで使い切ったら先頭へ戻る。全ホストが同時に落ちている場合でも
    再接続を続けられるようにするため、打ち止めにはしない。
    """

    def __init__(self, endpoints: list[str], start_at: Optional[str] = None) -> None:
        if not endpoints:
            raise ValueError('購読先（JETSTREAM_ENDPOINT）が1つも設定されていません。')
        self._endpoints = list(endpoints)
        self._index = self._endpoints.index(start_at) if start_at in self._endpoints else 0

    @property
    def current(self) -> str:
        return self._endpoints[self._index]

    @property
    def has_alternatives(self) -> bool:
        return len(self._endpoints) > 1

    def advance(self) -> str:
        """次の候補へ移り、その購読先を返す。"""
        self._index = (self._index + 1) % len(self._endpoints)
        return self.current


class StalenessMonitor:
    """Jetstream ホストの配信遅延を定期的に判定してログに出す。

    createdAt はクライアントの自己申告値で個々にはあてにならないため、
    一定件数の中央値で判定する。

    サンプルはホストごとの状態なので、購読先を切り替えたら作り直す。
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._samples: list[float] = []
        self._last_check = time.monotonic()
        self._warned = False

    def add(self, event: dict) -> bool:
        """サンプルを1件加える。切り替えるべきほど遅れていれば True を返す。"""
        value = event_staleness_seconds(event)
        if value is not None:
            self._samples.append(value)
            if len(self._samples) > STALENESS_SAMPLE_SIZE:
                del self._samples[:-STALENESS_SAMPLE_SIZE]

        if time.monotonic() - self._last_check >= STALENESS_CHECK_INTERVAL_SECONDS:
            return self._report()
        return False

    def _report(self) -> bool:
        self._last_check = time.monotonic()
        if len(self._samples) < STALENESS_MIN_SAMPLES:
            return False

        median = statistics.median(self._samples)
        self._samples.clear()

        if median >= STALENESS_WARN_SECONDS:
            logger.warning(
                f'Jetstream の配信が約 {median / 3600:.1f} 時間遅れています'
                f'（{self._endpoint}）。'
                'カーソルが追随していても収集内容は古いままです。'
            )
            self._warned = True
            return median >= STALENESS_FAILOVER_SECONDS

        if self._warned:
            logger.info(f'Jetstream の配信遅延が解消しました（中央値 {median:.0f}秒）。')
            self._warned = False
        return False


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


def cursor_lag_seconds(cursor: Optional[int]) -> Optional[float]:
    """カーソルが実時刻からどれだけ遅れているかを秒で返す（未保存なら None）。"""
    if not cursor:
        return None
    return time.time() - cursor / 1_000_000


def _load_endpoint() -> Optional[str]:
    meta = IngestMeta.get_or_none(IngestMeta.key == ENDPOINT_META_KEY)
    return meta.value if meta else None


def _save_endpoint(endpoint: str) -> None:
    IngestMeta.replace(key=ENDPOINT_META_KEY, value=endpoint).execute()


def _load_primary() -> Optional[str]:
    meta = IngestMeta.get_or_none(IngestMeta.key == PRIMARY_META_KEY)
    return meta.value if meta else None


def _save_primary(primary: str) -> None:
    IngestMeta.replace(key=PRIMARY_META_KEY, value=primary).execute()


def initial_endpoint(
    endpoints: list[str],
    saved_endpoint: Optional[str],
    saved_primary: Optional[str],
    primary: str,
) -> str:
    """起動時に最初に試す購読先を返す。

    自動フェイルオーバーで移った先は再起動後も引き継ぐ。第一候補へ戻すと、落ちた
    ままのホストへ毎回繋ぎに行き、切り替えのたびにカーソルを8時間巻き戻して再生し直す
    ことになるため。

    ただし第一候補（JETSTREAM_ENDPOINT）が前回と変わっていれば人が設定を変えた
    ということなので、記録を無視して第一候補から始める。
    """
    if saved_primary == primary and saved_endpoint in endpoints:
        return saved_endpoint
    return endpoints[0]


def cursor_for_endpoint(
    cursor: Optional[int], previous_endpoint: Optional[str], endpoint: str, now_us: int
) -> Optional[int]:
    """購読ホストが変わっていればカーソルを巻き戻して返す。

    前のホストが遅れて配信していた場合、カーソルは実時刻付近を指していても
    投稿は未収集なので、そのまま新ホストへ繋ぐとその分を恒久的に読み飛ばす。
    記録がない場合（この仕組みの導入前から動いている環境）も、既定値の変更などで
    ホストが変わった可能性があるため巻き戻す。

    ingest が長時間止まっていた場合は保存済みカーソルの方が古いので、
    カーソルを前進させない（＝取りこぼしを増やさない）よう小さい方を採る。
    """
    if cursor is None or previous_endpoint == endpoint:
        return cursor
    return min(cursor, now_us - ENDPOINT_CHANGE_REWIND_US)


def _apply_endpoint_change(cursor: Optional[int], endpoint: str) -> Optional[int]:
    """購読ホストの変更を検知し、必要ならカーソルを巻き戻して永続化する。"""
    previous = _load_endpoint()
    if previous == endpoint:
        return cursor

    rewound = cursor_for_endpoint(cursor, previous, endpoint, int(time.time() * 1_000_000))
    if rewound != cursor:
        logger.warning(
            f'購読ホストが変わりました（{previous or "記録なし"} → {endpoint}）。'
            '前のホストが遅れていた場合の取りこぼしを防ぐため、'
            f'カーソルを {_format_time_us(rewound)} まで巻き戻します。'
        )
        _save_cursor(rewound)
    _save_endpoint(endpoint)
    return rewound


def _format_time_us(time_us: int) -> str:
    return f'{datetime.utcfromtimestamp(time_us / 1_000_000):%Y-%m-%d %H:%M:%S} UTC'


def _failover(
    rotator: EndpointRotator, cursor: Optional[int], reason: str
) -> Optional[int]:
    """次の候補ホストへ切り替え、カーソルを巻き戻して返す。

    候補が1つしかなければ何もしない。

    切り替えの理由によらず巻き戻す。前のホストが遅れて配信していた場合、カーソルは
    実時刻付近を指していても投稿は未収集なので、そのまま新ホストへ繋ぐと恒久的に
    読み飛ばす。接続できずに切り替える場合も「落ちる直前まで遅れていた」可能性は
    排除できず（遅延判定は5分間隔なので、遅れ始めてすぐ落ちれば気づけない）、
    読み飛ばしより再生のコストを取る。長時間繋がらなかった場合は保存済みカーソルの
    方が古いため、`cursor_for_endpoint` の min により前進はしない。
    """
    if not rotator.has_alternatives:
        return cursor

    previous = rotator.current
    endpoint = rotator.advance()
    logger.warning(f'{reason}、購読先を切り替えます: {previous} → {endpoint}')
    return _apply_endpoint_change(cursor, endpoint)


def run() -> None:
    endpoints = config.JETSTREAM_ENDPOINTS
    primary = config.JETSTREAM_ENDPOINT
    rotator = EndpointRotator(
        endpoints,
        start_at=initial_endpoint(endpoints, _load_endpoint(), _load_primary(), primary),
    )
    # 購読先の記録を先に更新する。逆順にすると、2つの書き込みの間で落ちたときに
    # 「第一候補は新しいのに購読先は古い」状態が残り、次の起動で古い購読先を
    # 自動フェイルオーバー先と誤認して、変更した第一候補を無視し続けてしまう。
    cursor = _apply_endpoint_change(_load_cursor(), rotator.current)
    _save_primary(primary)
    last_cleanup = 0.0
    staleness = StalenessMonitor(rotator.current)
    failures = 0

    while True:
        url = build_url(rotator.current, cursor)
        try:
            with connect(
                url,
                max_queue=RECV_QUEUE_SIZE,
                ping_timeout=PING_TIMEOUT_SECONDS,
            ) as ws:
                logger.info(f'Jetstream に接続しました: {rotator.current} (cursor={cursor})')
                # 巻き戻し再生分の古い time_us を保存してカーソルが後退しないよう、
                # 保存済みカーソルより先に進んだイベントのみを保存対象にする
                last_saved_us = cursor or 0
                while True:
                    try:
                        message = ws.recv(timeout=RECV_TIMEOUT_SECONDS)
                    except TimeoutError:
                        logger.warning(
                            f'{RECV_TIMEOUT_SECONDS}秒間イベントが届きませんでした。再接続します。'
                        )
                        failures += 1
                        break

                    # ハンドシェイクが通っても黙り込むホストがあるため、
                    # 接続できた時点ではなく実際にイベントが届いた時点で回復とみなす
                    failures = 0

                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError as e:
                        logger.error(f'イベントの JSON 解析に失敗しました: {e}')
                        continue

                    # 切り替え先が無い設定では、繋ぎ直しても同じホストに戻るだけで
                    # 5分ごとに無駄な再接続を繰り返すことになるため、警告に留める
                    if staleness.add(event) and rotator.has_alternatives:
                        cursor = _failover(rotator, cursor, '配信が遅れているため')
                        staleness = StalenessMonitor(rotator.current)
                        break

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
            lag = cursor_lag_seconds(cursor)
            if cursor and should_reset_cursor(e):
                logger.warning(
                    f'ハンドシェイクが拒否されました: {e}。'
                    'カーソルを破棄してライブテールから再開します。'
                )
                cursor = None
            lag_text = f'（カーソルは実時刻より {lag:.0f}秒 前）' if lag is not None else ''
            logger.error(
                f'Jetstream 接続に失敗しました: {e}{lag_text}。'
                f'{RECONNECT_DELAY_SECONDS}秒後に再接続します。'
            )

            failures += 1
            time.sleep(RECONNECT_DELAY_SECONDS)

        # 接続失敗と「繋がったが無音」の両方をまとめて数え、続くようなら別ホストへ移る
        if failures >= FAILOVER_AFTER_FAILURES:
            cursor = _failover(
                rotator, cursor, f'{failures}回連続で購読に失敗したため'
            )
            staleness = StalenessMonitor(rotator.current)
            failures = 0


def _shutdown_handler(*_):
    raise SystemExit(0)


def main() -> None:
    # カーソルは定期保存のため、強制終了しても再接続時の巻き戻しで復旧できる
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    run()


if __name__ == '__main__':
    main()
