"""Claude Code の設定ファイルに、デーモンへ状態を送るフックを入れる・入っているか調べる（Windows 用）。

install.py（入れる）と agentpadctl.py（調べる）の両方から使う。

既存の設定は消さない。触るのは、このパッドのフック（hook_send.py を呼ぶもの）だけ。
フォルダを移動してから入れ直した場合は、古い場所を指しているものを新しい場所に書き換える。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PureWindowsPath

DAEMON_DIR = Path(__file__).resolve().parent
HOOK_SENDER = DAEMON_DIR / "hook_send.py"
HOOKS_EXAMPLE = DAEMON_DIR / "hooks.example.json"
VENV_PYTHON = DAEMON_DIR / ".venv" / "Scripts" / "python.exe"

# hook_send.py の既定のポート。これと違うときだけ、フックの引数でポートを渡す
DEFAULT_PORT = "8787"
# フックがこの秒数で終わらなければ Claude Code が打ち切る。送信側の時間切れ (2 秒) より長くする
HOOK_TIMEOUT_SEC = 5
BACKUP_SUFFIX = ".bak"


class SettingsError(Exception):
    """設定ファイルを読めない、または想定した形をしていない。メッセージはそのまま利用者に見せる。"""


def settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def events() -> list[str]:
    """フックを入れるイベント。macOS 用の見本と同じものにそろえる。"""
    return list(json.loads(HOOKS_EXAMPLE.read_text(encoding="utf-8"))["hooks"])


def handler(port: str | None = None) -> dict:
    arguments = [str(HOOK_SENDER)]
    if port and port != DEFAULT_PORT:
        arguments.append(port)
    return {
        "type": "command",
        # args があると、シェルを介さずにこのプログラムを直接起動する。
        # Git Bash と PowerShell のどちらが使われるかに左右されず、空白を含むパスでも動く
        "command": str(VENV_PYTHON),
        "args": arguments,
        "timeout": HOOK_TIMEOUT_SEC,
    }


def is_ours(candidate: object) -> bool:
    """このパッドのフックかどうか。場所が違っても、hook_send.py を呼んでいればそうみなす。"""
    if not isinstance(candidate, dict):
        return False
    arguments = candidate.get("args")
    words = [candidate.get("command"), *(arguments if isinstance(arguments, list) else [])]
    return any(
        isinstance(word, str) and PureWindowsPath(word.strip('"')).name.lower() == HOOK_SENDER.name
        for word in words
    )


def load(path: Path) -> dict:
    """設定ファイルを読む。無ければ空。壊れていれば SettingsError。"""
    if not path.exists():
        return {}
    try:
        # メモ帳などが先頭に付ける印 (BOM) があっても読めるようにする
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path} を JSON として読めません: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} の中身がオブジェクトではありません")
    return data


def _groups(settings: dict, event: str) -> list:
    """イベントに登録されているフックの組の並び。想定した形でなければ SettingsError。"""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError('"hooks" がオブジェクトではありません')
    groups = hooks.get(event, [])
    if not isinstance(groups, list):
        raise SettingsError(f'"hooks" の "{event}" が配列ではありません')
    return groups


def _handlers(groups: list) -> list[tuple[list, int]]:
    """組の中の、このパッドのフックの場所。(それを含む配列, 位置) の並び。"""
    found = []
    for group in groups:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(inner, list):
            found.extend((inner, index) for index, item in enumerate(inner) if is_ours(item))
    return found


def missing(settings: dict, wanted: dict) -> list[str]:
    """wanted と同じフックが入っていないイベント。"""
    return [
        event
        for event in events()
        if not any(inner[index] == wanted for inner, index in _handlers(_groups(settings, event)))
    ]


def merge(settings: dict, wanted: dict) -> bool:
    """settings に wanted を入れる。書き換えたら True。

    すでに同じものがあれば何もしない。古い場所を指すものがあれば書き換える。
    どちらも無ければ、既存のフックの後ろに足す。
    """
    changed = False
    for event in events():
        groups = _groups(settings, event)
        ours = _handlers(groups)
        if not ours:
            settings.setdefault("hooks", {}).setdefault(event, groups).append({"hooks": [dict(wanted)]})
            changed = True
            continue
        for inner, index in ours:
            if inner[index] != wanted:
                inner[index] = dict(wanted)
                changed = True
    return changed


def install(port: str | None = None) -> str:
    """設定ファイルにフックを入れ、何をしたかを 1 行で返す。できなければ SettingsError。"""
    path = settings_path()
    settings = load(path)
    if not merge(settings, handler(port)):
        return f"フックは登録済みです ({path})"

    note = ""
    if path.exists():
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
        note = f"。元の内容は {backup.name} に残しました"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 途中で止まっても設定ファイルが半端にならないよう、別名で書いてから置き換える
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return f"フックを登録しました ({path}){note}"


def problem(port: str | None = None) -> str | None:
    """フックが正しく入っていなければ、その説明。入っていれば None。"""
    path = settings_path()
    try:
        absent = missing(load(path), handler(port))
    except SettingsError as exc:
        return f"フックを確かめられません: {exc}"
    if not absent:
        return None
    if len(absent) == len(events()):
        return f"フックが未登録です ({path})。Claude Code の状態がデーモンに届きません"
    return f"フックの一部が未登録です ({path}): {', '.join(absent)}"
