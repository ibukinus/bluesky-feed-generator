"""Jetstream のカーソルを指定時間だけ巻き戻す運用スクリプト。

購読ホスト（`JETSTREAM_ENDPOINT`）を切り替えるときに使う。

保存済みカーソルは切り替え前のホストが付けた `time_us` である。ホストが
ネットワークから遅れていた場合、カーソルは実時刻付近を指していても投稿は
その分だけ未収集なので、同じカーソルのまま新ホストへ繋ぐと未収集分を恒久的に
読み飛ばす（2026-08-14 の障害がこれ。
docs/reports/2026-08-14-jetstream2-配信遅延.md を参照）。

ingest を止めてから実行し、`JETSTREAM_ENDPOINT` を変更して起動し直すこと。
巻き戻し分は再生されるが、登録済み URI は insert 時に排除される。

使い方:
    # compose 環境（.env が読み込まれる）
    docker compose stop ingest
    docker compose run --rm ingest python scripts/rewind_cursor.py --hours 7
    docker compose up -d ingest

    # ローカル
    uv run python scripts/rewind_cursor.py --hours 7
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Jetstream の再生可能範囲（Bluesky ホストのライブソケットの lookback）
REPLAY_WINDOW_HOURS = 36


def compute_target(cursor: int, hours: float, now_us: int) -> int:
    """巻き戻し先の time_us を返す。

    ingest が長時間止まっていた場合は保存済みカーソルの方が古いので、
    カーソルを前進させない（＝取りこぼしを増やさない）よう小さい方を採る。
    """
    return min(cursor, now_us - int(hours * 3600 * 1_000_000))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--hours', type=float, required=True,
        help=f'何時間前まで巻き戻すか（Jetstream の再生範囲は {REPLAY_WINDOW_HOURS} 時間）',
    )
    args = parser.parse_args()

    if args.hours <= 0:
        print('--hours は正の数を指定してください。', file=sys.stderr)
        return 1
    if args.hours > REPLAY_WINDOW_HOURS:
        print(
            f'--hours は {REPLAY_WINDOW_HOURS} 以下にしてください'
            f'（それより前は Jetstream が再生できません）。',
            file=sys.stderr,
        )
        return 1

    # import 時に DB へ接続するため、引数の検証後に読み込む
    from server.database import SubscriptionState
    from server.ingest import SERVICE_NAME

    state = SubscriptionState.get_or_none(SubscriptionState.service == SERVICE_NAME)
    if state is None or not state.cursor:
        print('カーソルが未保存です。ingest はライブテールから開始します。')
        return 0

    target = compute_target(state.cursor, args.hours, int(time.time() * 1_000_000))
    if target == state.cursor:
        print(
            f'カーソルはすでに {args.hours} 時間前より古いため変更しません'
            f'（{_format(state.cursor)}）。'
        )
        return 0

    SubscriptionState.update(cursor=target).where(
        SubscriptionState.service == SERVICE_NAME
    ).execute()
    print(f'カーソルを巻き戻しました: {_format(state.cursor)} → {_format(target)}')
    return 0


def _format(time_us: int) -> str:
    return f'{datetime.fromtimestamp(time_us / 1_000_000, tz=timezone.utc):%Y-%m-%d %H:%M:%S} UTC'


if __name__ == '__main__':
    raise SystemExit(main())
