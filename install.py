"""harness-pad-4 のセットアップ（Windows）。何度実行してもよい。

    python install.py        （python が無ければ py install.py）

  1. Python の専用環境 (daemon\\.venv) を作り、依存を入れる
  2. Claude Code のスキル /pad を入れる
  3. このフォルダの場所に合わせたフック設定 (daemon\\hooks.windows.json) を書き出す
  4. デーモンを起動し、ログイン時に自動で起動するよう登録する

Claude Code のフック設定だけは自動では入れない。既存の設定を壊さないよう、
最後に表示する手順で手で統合する。

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
HOOK_SENDER = DAEMON_DIR / "hook_send.py"
HOOKS_EXAMPLE = DAEMON_DIR / "hooks.example.json"
HOOKS_OUT = DAEMON_DIR / "hooks.windows.json"
SKILL_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "skills" / "pad"

MIN_PYTHON = (3, 9)
# フックがこの秒数で終わらなければ Claude Code が打ち切る。送信側の時間切れ (2 秒) より長くする
HOOK_TIMEOUT_SEC = 5
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


def write_hooks() -> None:
    """hooks.example.json と同じイベントに、Windows 用の送り方を当てはめて書き出す。"""
    example = json.loads(HOOKS_EXAMPLE.read_text(encoding="utf-8"))
    handler = {
        "type": "command",
        # args があると、シェルを介さずにこのプログラムを直接起動する。
        # Git Bash と PowerShell のどちらが使われるかに左右されず、空白を含むパスでも動く
        "command": str(VENV_PYTHON),
        # ポートを変えて入れた場合 (AGENTPAD_PORT) は、送り先も合わせる
        "args": [str(HOOK_SENDER), *filter(None, [os.environ.get("AGENTPAD_PORT")])],
        "timeout": HOOK_TIMEOUT_SEC,
    }
    hooks = {event: [{"hooks": [handler]}] for event in example["hooks"]}
    comment = [
        "install.py が、このフォルダの場所に合わせて書き出した Claude Code のフック設定。",
        "hooks の中身を %USERPROFILE%\\.claude\\settings.json の hooks へ統合する。",
        "このフォルダを移動したら、install.py をやり直して統合し直す。",
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

    print(f"==> フック設定を書き出します ({HOOKS_OUT})")
    write_hooks()

    print("==> デーモンを起動し、自動起動に登録します")
    run(VENV_PYTHON, CONTROL, "install")

    print("==> パッドを確かめます")
    # 見つからなくても続ける。あとから繋げばデーモンが自動で接続する
    subprocess.run([str(VENV_PYTHON), str(CONTROL), "check"], check=False)

    print(
        f"""
セットアップが終わりました。残りは Claude Code のフック設定です。

  1. {HOOKS_OUT} の "hooks" の中身を、
     {Path.home() / ".claude" / "settings.json"} の "hooks" に統合する
  2. Claude Code で /hooks を開く（またはセッションを始め直す）

このフォルダを移動したときは、もう一度 install.py を実行し、フックも統合し直してください。"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
