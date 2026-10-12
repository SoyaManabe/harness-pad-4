"""セッションが動いている端末を前面に出す（Windows）。入口は focus.py。

macOS 版と同じく cwd を手がかりにする:

    cwd → エージェントのプロセス → 祖先のうちウィンドウを持つもの → 前面へ

プロセスの調査は読み取りだけで、対象プロセスには一切触れない。前面に出す操作も
端末アプリのウィンドウに対して行い、エージェントには何も送らない。

できるのはウィンドウを選ぶところまで。タブは選べない (Windows Terminal、VS Code)。
同じアプリが複数のウィンドウを持つときは、題名にフォルダ名を含むものを選ぶ。

Windows は、操作中でないプログラムが勝手にウィンドウを前へ出すことを制限している。
断られた場合はタスクバーのボタンが点滅するだけになる。キー入力を偽装すれば
通せるが、入力を送らないという方針に反するので行わない。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

log = logging.getLogger(__name__)

# エージェント本体とみなす実行ファイル名（拡張子なし・小文字）
AGENT_NAMES = ("claude",)

# npm で入れたエージェントは node として動く。スクリプトの場所にこの語があればエージェントとみなす。
# "claude" だけでは、エージェントが起動した別の node（.claude の下の拡張など）まで拾う
SCRIPT_HOSTS = ("node",)
AGENT_COMMAND_MARKERS = ("claude-code",)

# 祖先をたどるのをここで打ち切る。エクスプローラはフォルダの窓を持っているので、
# ここまで来てから窓を探すと無関係なフォルダが前に出る
SHELL_NAMES = ("explorer",)

# Git Bash 風のパス (/c/Users/...) の先頭
MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")

# ShowWindow の引数: 最小化を解いて元の大きさに戻す
SW_RESTORE = 9
# GetWindow の引数: 所有者のウィンドウ
GW_OWNER = 4


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    name: str  # 拡張子なし・小文字
    started: float


@dataclass(frozen=True)
class Window:
    handle: int
    pid: int
    title: str


# ------------------------------------------------------------------ 判断（OS に触れない）


def to_windows_path(path: str) -> PureWindowsPath:
    """パスを比べられる形にする。Git Bash 風の /c/Users も受ける。"""
    return PureWindowsPath(MSYS_DRIVE.sub(lambda m: f"{m.group(1)}:/", path))


def program_name(executable: str) -> str:
    name = executable.lower()
    return name[:-4] if name.endswith(".exe") else name


def is_agent(name: str, command_line: list[str]) -> bool:
    if name in AGENT_NAMES:
        return True
    if name not in SCRIPT_HOSTS:
        return False
    # 先頭は node 自身。その後ろの、スクリプトの場所を見る
    return any(
        marker in argument.lower().replace("\\", "/").rsplit("/node_modules/", 1)[-1]
        for argument in command_line[1:3]
        for marker in AGENT_COMMAND_MARKERS
    )


def outermost(cwds: dict[int, str], table: dict[int, Proc]) -> dict[int, str]:
    """候補のうち、別の候補の子孫であるものを除く。

    エージェントが自分と同じ名前の子プロセスを持つことがある。同じセッションなので、
    別々に数えると「複数動いている」と誤って警告する。
    """
    return {
        pid: cwd
        for pid, cwd in cwds.items()
        if not any(p.pid in cwds for p in ancestors(pid, table)[1:])
    }


def find_agent(cwd: str, cwds: dict[int, str]) -> tuple[int, bool] | None:
    """cwd で動いているエージェントの PID と、候補が複数あったかどうか。

    フックの cwd はセッション中に変わりうるが、プロセスの cwd は起動時のまま。
    完全一致が無ければ、cwd の祖先で動いているもののうち最も近いものを採る。
    """
    wanted = to_windows_path(cwd)
    best = None
    best_depth = -1
    ambiguous = False
    for pid, proc_cwd in sorted(cwds.items()):
        path = to_windows_path(proc_cwd)
        if path != wanted and path not in wanted.parents:
            continue
        depth = len(path.parts)
        if depth > best_depth:
            best, best_depth, ambiguous = pid, depth, False
        elif depth == best_depth:
            ambiguous = True
    return None if best is None else (best, ambiguous)


def ancestors(pid: int, table: dict[int, Proc]) -> list[Proc]:
    """pid から親をたどった列。pid 自身を先頭に含む。エクスプローラの手前で止める。"""
    chain = []
    seen = set()
    current = table.get(pid)
    while current is not None and current.pid not in seen and current.name not in SHELL_NAMES:
        seen.add(current.pid)
        chain.append(current)
        parent = table.get(current.ppid)
        # Windows は親が終わっても番号を残し、その番号が別のプロセスに使い回される。
        # 子より後に始まった「親」は別人
        if parent is not None and parent.started > current.started:
            break
        current = parent
    return chain


def pick_window(chain: list[Proc], windows: list[Window], cwd: str) -> Window | None:
    """祖先のうち最も近い、ウィンドウを持つプロセスのウィンドウを選ぶ。

    windows は手前にあるものから順に並んでいること。複数あれば、題名に cwd か
    その祖先のフォルダ名を含むもの（近いフォルダを優先）、無ければ最も手前のもの。
    """
    for proc in chain:
        owned = [w for w in windows if w.pid == proc.pid]
        if not owned:
            continue
        if len(owned) > 1:
            path = to_windows_path(cwd)
            for folder in (path, *path.parents):
                if not folder.name:
                    continue
                for window in owned:
                    if folder.name.lower() in window.title.lower():
                        return window
        return owned[0]
    return None


# ------------------------------------------------------------------ 調査（読み取りだけ）


def _processes(psutil) -> tuple[dict[int, Proc], dict[int, str]]:
    """全プロセスの表と、エージェントとみなしたプロセスの cwd。"""
    table = {}
    cwds = {}
    for process in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        info = process.info
        name = program_name(info.get("name") or "")
        table[info["pid"]] = Proc(info["pid"], info.get("ppid") or 0, name, info.get("create_time") or 0.0)
        if name not in AGENT_NAMES and name not in SCRIPT_HOSTS:
            continue
        try:
            if is_agent(name, process.cmdline() if name in SCRIPT_HOSTS else []):
                cwds[info["pid"]] = process.cwd()
        except (psutil.Error, OSError):
            # 調べている間に終わった、または権限が無い。候補から外すだけ
            continue
    return table, cwds


def _windows(user32) -> list[Window]:
    """画面に出ている最上位のウィンドウ。手前にあるものから順。"""
    import ctypes
    from ctypes import wintypes

    found = []

    @user32.WNDENUMPROC
    def visit(handle, _):
        if not user32.IsWindowVisible(handle) or user32.GetWindow(handle, GW_OWNER):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length == 0:
            return True  # 題名の無いものは、利用者が見ているウィンドウではない
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        found.append(Window(handle, pid.value, buffer.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def _user32():
    """user32 の関数に型を付ける。付けないと 64 ビットでハンドルが切り詰められる。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.WNDENUMPROC = callback
    for name, result, arguments in (
        ("EnumWindows", wintypes.BOOL, (callback, wintypes.LPARAM)),
        ("IsWindowVisible", wintypes.BOOL, (wintypes.HWND,)),
        ("IsIconic", wintypes.BOOL, (wintypes.HWND,)),
        ("GetWindow", wintypes.HWND, (wintypes.HWND, wintypes.UINT)),
        ("GetWindowTextLengthW", ctypes.c_int, (wintypes.HWND,)),
        ("GetWindowTextW", ctypes.c_int, (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)),
        ("GetWindowThreadProcessId", wintypes.DWORD, (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))),
        ("GetForegroundWindow", wintypes.HWND, ()),
        ("SetForegroundWindow", wintypes.BOOL, (wintypes.HWND,)),
        ("BringWindowToTop", wintypes.BOOL, (wintypes.HWND,)),
        ("ShowWindow", wintypes.BOOL, (wintypes.HWND, ctypes.c_int)),
        ("AttachThreadInput", wintypes.BOOL, (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)),
    ):
        function = getattr(user32, name)
        function.restype = result
        function.argtypes = arguments
    return user32


# ------------------------------------------------------------------ 操作


def _raise(user32, handle: int) -> bool:
    """ウィンドウを前面に出す。Windows に断られたら False。"""
    import ctypes

    if user32.IsIconic(handle):
        user32.ShowWindow(handle, SW_RESTORE)

    user32.SetForegroundWindow(handle)
    if user32.GetForegroundWindow() == handle:
        return True

    # 断られた。いま前面にあるウィンドウの入力の流れに一時的に相乗りすると、
    # 「操作中のプログラム」として扱われて通ることが多い。キー入力は送らない。
    foreground = user32.GetForegroundWindow()
    if foreground:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        ours = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(foreground, None)
        if theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True):
            try:
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
            finally:
                user32.AttachThreadInput(ours, theirs, False)
    return user32.GetForegroundWindow() == handle


def focus_session(cwd: str, label: str = "") -> bool:
    """cwd で動いているセッションの端末を前面に出す。できたら True。"""
    name = label or cwd

    try:
        import psutil
    except ImportError:
        log.warning("psutil を読めないため、フォーカスを移せません。`pip install psutil` を確かめてください")
        return False

    table, cwds = _processes(psutil)
    cwds = outermost(cwds, table)
    log.debug("エージェントの候補: %s", cwds or "なし")
    if not cwds:
        log.warning("エージェントのプロセスが見つかりません (%s)", "/".join(AGENT_NAMES))
        return False

    found = find_agent(cwd, cwds)
    if found is None:
        log.warning("%s で動いているエージェントが見つかりません (cwd=%s)", name, cwd)
        return False
    pid, ambiguous = found
    if ambiguous:
        # 同じ場所で複数動いていると cwd では区別できない
        log.warning("%s で複数のエージェントが動いています。PID %d を選びました", cwd, pid)

    chain = ancestors(pid, table)
    log.debug("祖先: %s", " ← ".join(f"{p.name}({p.pid})" for p in chain))

    user32 = _user32()
    windows = _windows(user32)
    window = pick_window(chain, windows, cwds[pid])
    if window is None:
        log.warning(
            "%s (PID %d) を表示しているウィンドウを特定できません (祖先: %s)",
            name, pid, " ← ".join(p.name for p in chain),
        )
        return False

    owner = table[window.pid].name if window.pid in table else "?"
    if not _raise(user32, window.handle):
        log.warning(
            "%s のウィンドウ (%s: %s) を前面に出すことを Windows に断られました。"
            "タスクバーのボタンが点滅しているはずです",
            name, owner, window.title,
        )
        return False

    log.info("%s のウィンドウを前面に出しました (%s: %s)", name, owner, window.title)
    return True
