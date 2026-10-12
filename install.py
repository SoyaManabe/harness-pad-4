"""harness-pad-4 のセットアップ（Windows）。何度実行してもよい。

    python install.py        （python が無ければ py install.py）

  1. Python の専用環境 (daemon\\.venv) を作り、依存を入れる
  2. Claude Code のスキル /pad を入れる
  3. Claude Code の設定 (settings.json) に、デーモンへ状態を送るフックを足す
  4. デーモンを起動し、ログイン時に自動で起動するよう登録する

フックは、既存の設定を消さずに足す。書き換える前の内容は settings.json.bak に残す。
設定ファイルを読めないときは触らず、手で統合する手順を表示する
（統合する中身は daemon\\hooks.windows.json に書き出してある）。

macOS では install.sh を使う。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAEMON_DIR = ROOT / "daemon"
VENV_DIR = DAEMON_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
CONTROL = DAEMON_DIR / "agentpadctl.py"
HOOKS_OUT = DAEMON_DIR / "hooks.windows.json"
SKILL_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "skills" / "pad"

MIN_PYTHON = (3, 9)
# スキルの中で、agentpadctl の呼び出し方に置き換える目印
SKILL_PLACEHOLDER = "__AGENTPADCTL__"


def die(message: str) -> None:
    sys.exit(f"install.py: {message}")


def run(*command: object) -> None:
    done = subprocess.run([str(c) for c in command], check=False)
    if done.returncode != 0:
        die(f"失敗しました ({done.returncode}): {' '.join(str(c) for c in command)}")


def shell_word(path: Path) -> str:
    """Git Bash と PowerShell のどちらでもそのまま通る形のパス。

    区切りは / にする。\\ は Git Bash でエスケープとして読まれて消える。
    """
    text = path.as_posix()
    return f'"{text}"' if " " in text else text


def write_skill() -> None:
    call = f"{shell_word(VENV_PYTHON)} {shell_word(CONTROL)}"
    text = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    (SKILL_DIR / "SKILL.md").write_text(text.replace(SKILL_PLACEHOLDER, call), encoding="utf-8")


def write_hooks(hook_config) -> None:
    """統合するフックの中身を、ファイルにも書き出しておく。手で統合するときと、
    1 つのプロジェクトだけで有効にしたいときに使う。"""
    # ポートを変えて入れた場合 (AGENTPAD_PORT) は、送り先も合わせる
    handler = hook_config.handler(os.environ.get("AGENTPAD_PORT"))
    hooks = {event: [{"hooks": [handler]}] for event in hook_config.events()}
    comment = [
        "install.py が、このフォルダの場所に合わせて書き出した Claude Code のフック設定。",
        "install.py は同じものを %USERPROFILE%\\.claude\\settings.json に自動で足す。",
        "それができなかったときは、hooks の中身を settings.json の hooks へ手で統合する。",
    ]
    HOOKS_OUT.write_text(
        json.dumps({"_comment": comment, "hooks": hooks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if sys.platform != "win32":
        die("これは Windows 用です。macOS では ./install.sh を使ってください")
    if sys.version_info < MIN_PYTHON:
        die(f"Python {'.'.join(map(str, MIN_PYTHON))} 以上が必要です（いまは {platform.python_version()}）")
    if platform.machine().upper() not in ("AMD64", "X86"):
        print(
            f"注意: この CPU ({platform.machine()}) 向けの hidapi は配布されていない可能性があります。\n"
            "      依存の導入に失敗したら、x64 版の Python で試してください。"
        )

    if VENV_PYTHON.exists():
        # やり直しのとき。動いているデーモンが環境のファイルを掴んでいると、作り直しに失敗する
        subprocess.run([str(VENV_PYTHON), str(CONTROL), "stop"], check=False)

    print(f"==> Python の環境を用意します ({VENV_DIR})")
    run(sys.executable, "-m", "venv", VENV_DIR)
    run(VENV_PYTHON, "-m", "pip", "install", "--quiet", "--upgrade", "pip")
    run(VENV_PYTHON, "-m", "pip", "install", "--quiet", "-r", DAEMON_DIR / "requirements.txt")

    print(f"==> スキル /pad を入れます ({SKILL_DIR})")
    write_skill()

    print("==> Claude Code にフックを登録します")
    sys.path.insert(0, str(DAEMON_DIR))
    import hook_config

    write_hooks(hook_config)
    try:
        print("    " + hook_config.install(os.environ.get("AGENTPAD_PORT")))
        hooks_merged = True
    except hook_config.SettingsError as exc:
        # 読めない設定ファイルを書き換えると、ほかの設定まで壊しかねない。触らずに続ける
        print(f"    登録できませんでした: {exc}")
        hooks_merged = False

    print("==> デーモンを起動し、自動起動に登録します")
    run(VENV_PYTHON, CONTROL, "install")

    print("==> 確かめます")
    # パッドが見つからなくても続ける。あとから繋げばデーモンが自動で接続する
    subprocess.run([str(VENV_PYTHON), str(CONTROL), "check"], check=False)

    if hooks_merged:
        print(
            """
セットアップが終わりました。

Claude Code で /hooks を開くか、セッションを始め直すと、フックが読み込まれます。
このフォルダを移動したときは、もう一度 install.py を実行してください。"""
        )
        return 0

    print(
        f"""
*** フックが未登録です。このままでは、Claude Code の状態がパッドに出ません。 ***

設定ファイルを読めなかったので、自動では登録していません。手で統合してください。

  1. {HOOKS_OUT} の "hooks" の中身を、
     {hook_config.settings_path()} の "hooks" に統合する
  2. Claude Code で /hooks を開く（またはセッションを始め直す）
  3. daemon\\agentpadctl check で、登録できたかを確かめる"""
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
