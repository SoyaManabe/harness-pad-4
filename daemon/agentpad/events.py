"""エージェントの種類によらない、共通のイベント形式 (phase0/EVENTS.md)。

デーモンの中核 (registry.py) はこの形式だけを読む。エージェントごとの違いは
変換 (claude_hooks.py など) が吸収する。エージェントを足すときは変換を足せばよく、
状態の管理・表示・計測には手を入れずに済む。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from enum import Enum

from .protocol import SessionState

EVENT_VERSION = 1

# ホスト名の上限。表示には先頭の数文字しか出ないが、ログと計測には全体を残す。
HOST_MAX_CHARS = 32

# 共通形式での状態の名前。EMPTY はスロットの状態であってセッションの状態ではないので無い。
STATE_NAMES = {
    "idle": SessionState.IDLE,
    "busy": SessionState.BUSY,
    "waiting": SessionState.WAITING,
    "done": SessionState.DONE,
    "error": SessionState.ERROR,
}


class EventKind(Enum):
    STATE = "state"  # 状態が変わった
    END = "end"  # セッションが終わった
    ALIVE = "alive"  # 動いていることだけを伝える。状態は変えない


class RequestKind(Enum):
    SHELL = "shell"  # シェルのコマンド。デーモンが危険な操作の印を付ける対象
    TOOL = "tool"  # それ以外の操作（ファイルの編集、URL の取得など）
    MESSAGE = "message"  # 操作ではなく文面だけ


@dataclass(frozen=True)
class Request:
    """対応が必要なセッションが、何を求めているか。"""

    kind: RequestKind = RequestKind.MESSAGE
    # 求めている操作そのもの。SHELL ならコマンド、TOOL なら対象（パスなど）、MESSAGE なら文面
    text: str = ""
    # 操作の種類の名前（例: "Edit"）。SHELL と TOOL で使う
    tool: str = ""
    # エージェントが付けた短い説明。操作の代わりではなく、添えて見せる
    summary: str = ""


@dataclass(frozen=True)
class AgentEvent:
    session: str
    kind: EventKind
    state: SessionState | None = None
    # エージェントが動いているマシン。デーモンと同じマシンなら空
    host: str = ""
    # 変換の名前（例: "claude-code"）
    agent: str = ""
    cwd: str = ""
    # 人間が発言したことで起きたイベント。「直近に使ったセッション」の判定に使う
    prompt: bool = False
    request: Request | None = None


def local_host() -> str:
    return _short_host(socket.gethostname())


def normalize_host(host: object) -> str:
    """ホスト名を短い形にする。デーモン自身のマシンなら空文字列にする。

    同じフック設定を手元とリモートの両方で使えるように、手元のホスト名が
    届いても「手元」として扱う。
    """
    if not isinstance(host, str):
        return ""
    short = _short_host(host)
    if short.lower() == local_host().lower():
        return ""
    return short


def _short_host(host: str) -> str:
    return host.strip().split(".")[0][:HOST_MAX_CHARS]


def parse_event(data: dict, host: str = "") -> AgentEvent:
    """共通形式の JSON を読む。形式に合わなければ ValueError。

    host は、本文に host が無いときに使う値（接続元から分かっている場合）。
    """
    version = data.get("v")
    if version != EVENT_VERSION:
        raise ValueError(f"v は {EVENT_VERSION} である必要があります: {version!r}")

    session = data.get("session")
    if not isinstance(session, str) or not session:
        raise ValueError("session がありません")

    try:
        kind = EventKind(data.get("event"))
    except ValueError:
        raise ValueError(f"event が不正です: {data.get('event')!r}") from None

    state = None
    if kind is EventKind.STATE:
        state = STATE_NAMES.get(data.get("state"))
        if state is None:
            raise ValueError(f"state が不正です: {data.get('state')!r}")

    return AgentEvent(
        session=session,
        kind=kind,
        state=state,
        host=normalize_host(data.get("host") or host),
        agent=_text(data.get("agent")),
        cwd=_text(data.get("cwd")),
        prompt=data.get("prompt") is True,
        request=_parse_request(data.get("request")),
    )


def _parse_request(data: object) -> Request | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("request はオブジェクトで指定してください")
    try:
        kind = RequestKind(data.get("kind", RequestKind.MESSAGE.value))
    except ValueError:
        raise ValueError(f"request.kind が不正です: {data.get('kind')!r}") from None
    return Request(
        kind=kind,
        text=_text(data.get("text")),
        tool=_text(data.get("tool")),
        summary=_text(data.get("summary")),
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
