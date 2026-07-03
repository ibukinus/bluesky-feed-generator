"""Sudachi ユーザー辞書（user.csv）をビルドして sudachipy に組み込む。

Docker ビルド・ローカル開発・pytest（conftest.py）が共通で使う唯一のビルド手順。
sudachipy の resources ディレクトリに user.dic と sudachi.json を配置することで、
`dictionary.Dictionary()` がデフォルト設定のままユーザー辞書を読み込む。
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_user_dict() -> None:
    import sudachidict_core
    import sudachipy

    system_dic = Path(sudachidict_core.__file__).parent / "resources" / "system.dic"
    resources_dir = Path(sudachipy.__file__).parent / "resources"
    user_dic = resources_dir / "user.dic"
    user_csv = REPO_ROOT / "user.csv"
    repo_json = REPO_ROOT / "sudachi.json"
    installed_json = resources_dir / "sudachi.json"

    up_to_date = (
        user_dic.exists()
        and user_dic.stat().st_mtime >= user_csv.stat().st_mtime
        and installed_json.exists()
        and installed_json.read_bytes() == repo_json.read_bytes()
    )
    if up_to_date:
        return

    # sudachipy CLI は実行中の Python と同じ環境のものを使う
    ubuild = Path(sys.executable).with_name("sudachipy")
    subprocess.run(
        [str(ubuild), "ubuild", "-o", str(user_dic), "-s", str(system_dic), str(user_csv)],
        check=True,
    )
    shutil.copy(repo_json, installed_json)
    print(f"Sudachi ユーザー辞書をビルドしました: {user_dic}")


if __name__ == "__main__":
    build_user_dict()
