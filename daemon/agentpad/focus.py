"""セッションが動いている端末を前面に出す。

フックのペイロードには PID も tty も含まれないので、cwd を手がかりにする:

    cwd → エージェントのプロセス → その tty と、祖先にいる GUI アプリ → 前面へ

プロセスの調査は ps と lsof による読み取りだけで、対象プロセスには一切触れない。
前面に出す操作も端末アプリに対して行い、エージェントには何も送らない。

アプリごとにできることが違う:

    Terminal / iTerm2   tty が一致するタブを選んで前面へ
    VS Code 系          cwd かその祖先を開いているウィンドウを前面へ（タブまでは選べない）
    その他              アプリを前面へ出すだけ

    python3 -m agentpad.focus [cwd]    単体での動作確認
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# エージェント本体とみなす実行ファイル名
AGENT_COMMANDS = ("claude",)

# 外部コマンド 1 回あたりの上限。パッドの受信を長く止めないため。
COMMAND_TIMEOUT_SEC = 3.0

# フォルダを渡すと、それを開いているウィンドウを前面に出すアプリ。
# 開いているウィンドウが無いフォルダを渡すと新しいウィンドウが開いてしまうので、
# 先に同梱の CLI で開いているウィンドウを調べ、一致するフォルダだけを渡す。
WORKSPACE_APPS = (
    "Visual Studio Code.app",
    "Visual Studio Code - Insiders.app",
    "Cursor.app",
    "Windsurf.app",
)

# アプリのバンドル内で CLI を探す場所と、その名前の候補
WORKSPACE_CLI_DIR = "Contents/Resources/app/bin"
WORKSPACE_CLI_NAMES = ("code", "code-insiders", "cursor", "windsurf")

# `--status` は全プロセスを数えるので他のコマンドより遅い
STATUS_TIMEOUT_SEC = 10.0

# ウィンドウ名の既定の書式は「ファイル名 — フォルダ名」
TITLE_SEPARATOR = " — "

TERMINAL_SCRIPT = """
on run argv
    set wanted to item 1 of argv
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is wanted then
                    set selected tab of w to t
                    set index of w to 1
                    activate
                    return "ok"
                end if
            end repeat
        end repeat
    end tell
    return "not found"
end run
"""

ITERM_SCRIPT = """
on run argv
    set wanted to item 1 of argv
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if tty of s is wanted then
                        select w
                        tell t to select
                        tell s to select
                        activate
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "not found"
end run
"""

# tty でタブまで選べるアプリと、そのスクリプト
TTY_SCRIPTS = {
    "Terminal.app": TERMINAL_SCRIPT,
    "iTerm.app": ITERM_SCRIPT,
}


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    tty: str  # "ttys002" など。端末を持たなければ空
    comm: str


# ------------------------------------------------------------------ 調査


def _run(args: list[str], timeout: float = COMMAND_TIMEOUT_SEC) -> str | None:
    """外部コマンドを実行して標準出力を返す。失敗したら理由をログに出して None。"""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("%s を実行できませんでした: %s", args[0], exc)
        return None
    if done.returncode != 0:
        log.warning("%s が失敗しました (%d): %s", args[0], done.returncode, done.stderr.strip())
        return None
    return done.stdout


def parse_process_table(text: str) -> dict[int, Proc]:
    """`ps -axo pid=,ppid=,tty=,comm=` の出力を読む。"""
    table = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        tty = "" if parts[2].startswith("?") else parts[2]
        table[pid] = Proc(pid, ppid, tty, parts[3].strip())
    return table


def parse_cwds(text: str) -> dict[int, str]:
    """`lsof -a -d cwd -Fn -p ...` の出力を読む。"""
    cwds = {}
    pid = None
    for line in text.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


def find_agent(cwd: str, table: dict[int, Proc], cwds: dict[int, str]) -> Proc | None:
    """cwd で動いているエージェントのプロセスを選ぶ。

    フックの cwd はセッション中に変わりうるが、プロセスの cwd は起動時のまま。
    そのため完全一致が無ければ、cwd の祖先で動いているもののうち最も近いものを採る。
    """
    wanted = Path(cwd)
    best = None
    best_depth = -1
    ambiguous = False

    for pid, proc_cwd in sorted(cwds.items()):
        proc = table.get(pid)
        if proc is None:
            continue
        path = Path(proc_cwd)
        if path != wanted and path not in wanted.parents:
            continue
        depth = len(path.parts)
        if depth > best_depth:
            best, best_depth, ambiguous = proc, depth, False
        elif depth == best_depth:
            ambiguous = True

    if ambiguous:
        # 同じ場所で複数動いていると cwd では区別できない
        log.warning("%s で複数のエージェントが動いています。PID %d を選びました", cwd, best.pid)
    return best


def owning_app(proc: Proc, table: dict[int, Proc]) -> str | None:
    """プロセスの祖先をたどり、最初に見つかった .app のバンドルパスを返す。"""
    seen = set()
    current = proc
    while current is not None and current.pid not in seen:
        seen.add(current.pid)
        head, sep, _ = current.comm.partition(".app/")
        if sep:
            return head + ".app"
        current = table.get(current.ppid)
    return None


def parse_window_folders(status: str) -> set[str]:
    """`code --status` の出力から、開いている各ウィンドウのフォルダ名を拾う。

    該当行は `window [2] (WIRING.md — Projects)` の形をしている。
    """
    folders = set()
    for line in status.splitlines():
        line = line.strip()
        marker = line.find("window [")
        if marker < 0 or not line.endswith(")"):
            continue
        start = line.find("(", marker)
        if start < 0:
            continue
        title = line[start + 1 : -1]
        folders.add(title.rpartition(TITLE_SEPARATOR)[2].strip())
    return folders


def pick_workspace(cwd: str, folders: set[str]) -> str | None:
    """cwd とその祖先のうち、開いているウィンドウのフォルダ名に一致する最も近いもの。"""
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if candidate.name and candidate.name in folders:
            return str(candidate)
    return None


def _open_workspace_folder(app: str) -> set[str] | None:
    """アプリが開いているウィンドウのフォルダ名。調べられなければ None。"""
    bin_dir = Path(app) / WORKSPACE_CLI_DIR
    cli = next((bin_dir / n for n in WORKSPACE_CLI_NAMES if (bin_dir / n).exists()), None)
    if cli is None:
        log.warning("%s に CLI が見つかりません (%s)", Path(app).name, bin_dir)
        return None
    status = _run([str(cli), "--status"], timeout=STATUS_TIMEOUT_SEC)
    if status is None:
        return None
    return parse_window_folders(status)


# ------------------------------------------------------------------ 操作


def focus_session(cwd: str, label: str = "") -> bool:
    """cwd で動いているセッションの端末を前面に出す。できたら True。"""
    name = label or cwd

    if sys.platform != "darwin":
        log.warning("フォーカス移動は macOS でのみ対応しています")
        return False
    if not cwd:
        log.warning("%s の cwd が分からないため、フォーカスを移せません", name)
        return False

    ps_out = _run(["ps", "-axo", "pid=,ppid=,tty=,comm="])
    if ps_out is None:
        return False
    table = parse_process_table(ps_out)

    agents = [p.pid for p in table.values() if Path(p.comm).name in AGENT_COMMANDS]
    if not agents:
        log.warning("エージェントのプロセスが見つかりません (%s)", "/".join(AGENT_COMMANDS))
        return False

    lsof_out = _run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(map(str, agents))])
    if lsof_out is None:
        return False

    cwds = parse_cwds(lsof_out)
    agent = find_agent(cwd, table, cwds)
    if agent is None:
        log.warning("%s で動いているエージェントが見つかりません (cwd=%s)", name, cwd)
        return False

    app = owning_app(agent, table)
    if app is None:
        log.warning("%s (PID %d) を表示しているアプリを特定できません", name, agent.pid)
        return False

    app_name = Path(app).name
    script = TTY_SCRIPTS.get(app_name)

    if script and agent.tty:
        out = _run(["osascript", "-e", script, f"/dev/{agent.tty}"])
        if out is not None and out.strip() == "ok":
            log.info("%s のタブを前面に出しました (%s %s)", name, app_name, agent.tty)
            return True
        if out is not None:
            log.warning("%s に %s のタブが見つかりません。アプリだけ前面に出します", app_name, agent.tty)

    workspace = None
    if app_name in WORKSPACE_APPS:
        folders = _open_workspace_folder(app)
        if folders is not None:
            workspace = pick_workspace(cwds[agent.pid], folders)
            if workspace is None:
                log.warning(
                    "%s を開いている %s のウィンドウを特定できません (開いているもの: %s)。"
                    "アプリだけ前面に出します",
                    cwds[agent.pid], app_name, ", ".join(sorted(folders)) or "なし",
                )

    opened = _run(["open", "-a", app, workspace] if workspace else ["open", "-a", app])
    if opened is None:
        return False

    if workspace:
        log.info("%s のウィンドウを前面に出しました (%s: %s)", name, app_name, workspace)
    else:
        log.info("%s のアプリを前面に出しました (%s)。ウィンドウは選べていません", name, app_name)
    return True


class Focuser:
    """フォーカス移動を別スレッドで行う。

    パッドの受信スレッドから呼ばれるので、osascript の応答を待って
    次のキー入力を取りこぼさないようにする。
    """

    def __init__(self) -> None:
        self._busy = threading.Lock()

    def request(self, cwd: str, label: str = "") -> None:
        if not self._busy.acquire(blocking=False):
            log.info("前のフォーカス移動が終わっていないため、今回の分は無視します")
            return
        threading.Thread(
            target=self._work, args=(cwd, label), name="focus", daemon=True
        ).start()

    def _work(self, cwd: str, label: str) -> None:
        try:
            focus_session(cwd, label)
        except Exception:
            # 無言でスレッドが死ぬと原因が見えなくなる
            log.exception("フォーカス移動中に例外が発生しました")
        finally:
            self._busy.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else str(Path.cwd())
    sys.exit(0 if focus_session(target) else 1)
