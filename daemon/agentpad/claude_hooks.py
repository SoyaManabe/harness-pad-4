"""Claude Code のフックを、共通のイベント形式 (events.py) に直す。

フックの出典: https://code.claude.com/docs/en/hooks
"""

from __future__ import annotations

from .events import AgentEvent, EventKind, Request, RequestKind, normalize_host
from .protocol import SessionState

AGENT_NAME = "claude-code"

# このフックイベントを受けたらこの状態にする、という対応表。
EVENT_TO_STATE: dict[str, SessionState] = {
    "SessionStart": SessionState.IDLE,
    "UserPromptSubmit": SessionState.BUSY,
    "PreToolUse": SessionState.BUSY,
    "PostToolUse": SessionState.BUSY,
    "Notification": SessionState.WAITING,
    "PermissionRequest": SessionState.WAITING,
    "Elicitation": SessionState.WAITING,
    "Stop": SessionState.DONE,
    "StopFailure": SessionState.ERROR,
    "PostToolUseFailure": SessionState.BUSY,  # 失敗しても復帰を試みるので作業中のまま
}

# これを受けたらセッションが終わったとみなす
END_EVENTS = {"SessionEnd"}

# 人間が発言したことを示すイベント
PROMPT_EVENTS = {"UserPromptSubmit"}

# 許可要求の tool_input から、内容を最もよく表す値を探すときのキーの優先順。
TOOL_INPUT_KEYS = ("command", "file_path", "url", "pattern", "query", "description", "prompt")

# シェルのコマンドを実行するツール。入力はコマンドそのものとして渡す。
SHELL_TOOLS = {"Bash", "PowerShell"}

# パスの区切り。Windows のセッションからは \\ で届く
PATH_SEPARATORS = "/\\"


def from_hook(payload: dict, host: str = "") -> AgentEvent:
    """フックの JSON ペイロードを 1 件変換する。必須の項目が無ければ ValueError。

    host はフックを送ってきたマシン。ペイロードには含まれないので呼び出し側が渡す。
    """
    session = payload.get("session_id")
    name = payload.get("hook_event_name")
    if not isinstance(session, str) or not session or not isinstance(name, str) or not name:
        raise ValueError("session_id か hook_event_name がありません")

    common = {
        "session": session,
        "host": normalize_host(host),
        "agent": AGENT_NAME,
        "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else "",
    }

    if name in END_EVENTS:
        return AgentEvent(kind=EventKind.END, **common)

    state = EVENT_TO_STATE.get(name)
    if state is None:
        # 対応表にないイベントは状態を変えない。動いていることだけを伝える。
        return AgentEvent(kind=EventKind.ALIVE, **common)

    return AgentEvent(
        kind=EventKind.STATE,
        state=state,
        prompt=name in PROMPT_EVENTS,
        request=_request(payload),
        **common,
    )


def _request(payload: dict) -> Request | None:
    """何を求められているか。許可要求ならツールの入力から、それ以外はフックの文面から作る。"""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        message = next(
            (v for v in (payload.get("message"), payload.get("error")) if isinstance(v, str)),
            "",
        )
        return Request(text=message) if message else None

    tool_input = payload.get("tool_input")
    value = _summarize_tool_input(tool_input, payload.get("cwd"))
    if tool not in SHELL_TOOLS:
        return Request(kind=RequestKind.TOOL, tool=tool, text=value)

    description = tool_input.get("description") if isinstance(tool_input, dict) else None
    return Request(
        kind=RequestKind.SHELL,
        tool=tool,
        text=value,
        summary=description if isinstance(description, str) else "",
    )


def _summarize_tool_input(tool_input, cwd) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in TOOL_INPUT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            break
    else:
        value = next((v for v in tool_input.values() if isinstance(v, str) and v), "")

    # 作業ディレクトリ配下のパスは相対にする。先頭の共通部分で行を使い切らないため。
    if isinstance(cwd, str):
        root = cwd.rstrip(PATH_SEPARATORS)
        if root and value.startswith(root) and value[len(root) : len(root) + 1] in tuple(PATH_SEPARATORS):
            value = value[len(root) + 1 :]
    return value
