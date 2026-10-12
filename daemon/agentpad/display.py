"""パッドのディスプレイに出す内容を決める。

LED は「対応が必要だ」と気づかせる。ディスプレイはその次の
「何を求められているか」を、画面へ目を移す前に読ませるためにある
(docs/ai-agent-keyboard-report.md P3・P6)。

何をどの行に出すかはすべてここで決め、パッドは届いた行をそのまま描く。
表示を変えたくなってもファームウェアを書き直さずに済む。

    4 行のディスプレイ          8 行のディスプレイ
    ┌─────────────────────┐    ┌─────────────────────┐
    │S2 WAIT 0:42    ! 1/2│    │1 BUSY api-server    │
    │KeyBoard_2026        │    │2 WAIT!KeyBoard_2026 │
    │Force push           │    │3 WAIT docs          │
    │$ git push --force or│    │                     │
    └─────────────────────┘    │S2 WAIT 0:42    ! 1/2│
      対応が必要なときだけ。    │Force push           │
      無ければスロットの一覧    │$ git push --force or│
                               │igin main            │
                               └─────────────────────┘

詳細の本文は、エージェントが付けた説明と、求めている操作そのものの順に並べる。
入りきらないときは 1 行ずつ上へ送り、末尾まで出たら先頭に戻る。

対応が必要なセッションが複数あれば、詳細を順番に出す（見出しの右端に 2/3 のように出る）。
取り返しのつかない操作らしいものには ! を付ける。

リモートのセッションは、名前の前にホスト名を付ける（一覧では devbo:api-serv のように
先頭だけ、詳細では行に入るかぎり全部）。同じ操作でも、どのマシンで求められているかで
重みが変わるため。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .protocol import MAX_SLOTS, SessionState, build_text_report, sanitize_text

# 対応が必要な状態。この順に優先して詳細を出す。
ATTENTION_STATES = (SessionState.WAITING.name, SessionState.ERROR.name)

# 一覧に出さない状態。空きスロットだけ。
# 待機中のセッションは出す。どのキーにどのセッションが居るかを、何も起きていない
# あいだも確かめられるようにするため。そのぶん同じ文字が出続けるので、焼き付きは進みやすい。
QUIET_STATES = (SessionState.EMPTY.name,)

STATE_ABBREVIATIONS = {
    SessionState.IDLE.name: "IDLE",
    SessionState.BUSY.name: "BUSY",
    SessionState.WAITING.name: "WAIT",
    SessionState.DONE.name: "DONE",
    SessionState.ERROR.name: "ERR",
}

# 一覧と詳細を同時に出せる最小の行数
ROWS_FOR_LIST_AND_DETAIL = MAX_SLOTS + 2

# 本文が入りきらないとき、何秒ごとに 1 行送るか。
# デーモンの送信周期 (__main__.py の HEARTBEAT_INTERVAL_SEC) より短くしても速くはならない。
SCROLL_STEP_SEC = 2

# 先頭と末尾で止まっている長さ（SCROLL_STEP_SEC の何回分か）。
# 先頭は読み始める場所なので、送り始める前に間を置く。
SCROLL_HOLD_STEPS = 2

# 対応が必要なセッションが複数あるとき、1 つを最低何秒出してから次へ移るか。
ROTATE_MIN_SEC = 6

# 一覧の 1 行に、ホスト名を何文字まで出すか。残りは作業フォルダ名に使う。
LIST_HOST_CHARS = 5

# 取り返しのつかない操作らしいことを示す印 (registry.py の DANGER_PATTERNS)
DANGER_MARK = "!"


@dataclass(frozen=True)
class Line:
    text: str = ""
    invert: bool = False


def compose(slots: list[dict], rows: int, cols: int, elapsed_sec: int = 0) -> list[Line]:
    """Registry.snapshot(with_detail=True) から、表示する行を rows 個作る。

    elapsed_sec は、対応が必要なセッションの顔ぶれが今の形になってからの秒数。
    詳細を出すセッションの交代と、本文の送りはこれだけで決まる。
    """
    if rows <= 0 or cols <= 0:
        return []

    attention = _attention(slots)
    with_list = rows >= ROWS_FOR_LIST_AND_DETAIL
    detail_rows = rows - MAX_SLOTS if with_list else rows

    lines = _list_lines(slots, cols) if with_list or not attention else []
    if attention:
        lines += _detail_lines(attention, detail_rows, cols, elapsed_sec, with_label=not with_list)

    lines = lines[:rows]
    return lines + [Line()] * (rows - len(lines))


def _attention(slots: list[dict]) -> list[dict]:
    """対応が必要なスロット。入力待ちを先に、同じ状態なら長く待たせている順。"""
    return [
        slot
        for state in ATTENTION_STATES
        for slot in sorted(slots, key=lambda s: -s["age_sec"])
        if slot["state"] == state
    ]


def _list_lines(slots: list[dict], cols: int) -> list[Line]:
    """スロット 1 個につき 1 行。行の位置がキーの並びと一致する。"""
    lines = []
    for slot in slots[:MAX_SLOTS]:
        if slot["state"] in QUIET_STATES:
            lines.append(Line())
            continue
        state = STATE_ABBREVIATIONS.get(slot["state"], slot["state"][:4])
        mark = DANGER_MARK if slot.get("danger") else " "
        name = _name(slot, LIST_HOST_CHARS)
        lines.append(Line(_fit(f"{slot['slot']} {state:<4}{mark}{name}", cols)))
    return lines


def _detail_lines(
    attention: list[dict], rows: int, cols: int, elapsed_sec: int, with_label: bool
) -> list[Line]:
    """対応が必要なセッションの詳細。複数あれば順番に 1 つずつ出す。"""
    if rows <= 0:
        return []

    show_label = with_label and rows >= 3
    body_rows = rows - (2 if show_label else 1)

    # 1 つあたりの持ち時間は、本文を末尾まで送り終える長さにする。
    # 短い本文でも ROTATE_MIN_SEC は出しておく。
    bodies = [_body(slot, cols) for slot in attention]
    cycles = [_scroll_offsets(len(body), body_rows) for body in bodies]
    dwells = [max(ROTATE_MIN_SEC, len(cycle) * SCROLL_STEP_SEC) for cycle in cycles]

    index, local_sec = 0, elapsed_sec % sum(dwells)
    while local_sec >= dwells[index]:
        local_sec -= dwells[index]
        index += 1
    focus, body, cycle = attention[index], bodies[index], cycles[index]

    state = STATE_ABBREVIATIONS.get(focus["state"], focus["state"][:4])
    left = f"S{focus['slot']} {state} {_elapsed(focus['age_sec'])}"
    marks = [DANGER_MARK] if focus.get("danger") else []
    if len(attention) > 1:
        marks.append(f"{index + 1}/{len(attention)}")
    header = left + " ".join(marks).rjust(max(cols - len(left), 0))

    lines = [Line(_fit(header, cols).ljust(cols), invert=True)]
    if show_label:
        lines.append(Line(_fit(_name(focus, cols), cols)))
    offset = cycle[(local_sec // SCROLL_STEP_SEC) % len(cycle)]
    return lines + [Line(text) for text in body[offset : offset + max(body_rows, 0)]]


def _body(slot: dict, cols: int) -> list[str]:
    """詳細の本文。エージェントの説明を先に、操作そのものをその後に続ける。

    説明だけを見せることはしない。読まずに承認する癖を助長しないため
    (docs/ai-agent-keyboard-report.md 原則 02)。
    """
    return _wrap(slot.get("summary", ""), cols) + _wrap(slot.get("detail", ""), cols)


def _name(slot: dict, host_chars: int) -> str:
    """セッションの表示名。リモートならホスト名の先頭 host_chars 文字を前に付ける。"""
    host = slot.get("host", "")
    return f"{host[:host_chars]}:{slot['label']}" if host else slot["label"]


def _elapsed(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fit(text: str, cols: int) -> str:
    return sanitize_text(text)[:cols]


def _wrap(text: str, cols: int) -> list[str]:
    """cols 文字ごとに折り返す。コマンドは空白で切ると読みにくいので位置で切る。"""
    text = sanitize_text(text)
    return [text[i : i + cols] for i in range(0, len(text), cols)]


def _scroll_offsets(line_count: int, rows: int) -> list[int]:
    """本文を送るときの、先頭行の位置の並び。SCROLL_STEP_SEC ごとに 1 つ進む。

    入りきるなら [0] だけ。入りきらなければ 1 行ずつ送り、先頭と末尾で少し止まる。
    """
    overflow = line_count - rows
    if rows <= 0 or overflow <= 0:
        return [0]
    return [0] * SCROLL_HOLD_STEPS + list(range(1, overflow)) + [overflow] * SCROLL_HOLD_STEPS


class PadDisplay:
    """パッドのディスプレイの大きさと、送信済みの内容を覚えておく。

    大きさはパッドが HELLO で申告する。申告があるまでは何も送らない。
    変わった行だけを送るので、表示が同じあいだ通信は発生しない。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows = 0
        self._cols = 0
        self._sent: dict[int, Line] = {}
        # 対応が必要なセッションの顔ぶれと、それが今の形になった時刻。
        # 顔ぶれが変わったら、交代と本文の送りを最初からやり直す。
        self._attention_key: tuple = ()
        self._attention_since = 0.0

    def configure(self, rows: int, cols: int) -> None:
        """パッドが起動し直した。画面は空なので、次の update で全行を送り直す。"""
        with self._lock:
            self._rows, self._cols = rows, cols
            self._sent = {}

    @property
    def size(self) -> tuple[int, int]:
        with self._lock:
            return self._rows, self._cols

    def update(self, slots: list[dict], send) -> None:
        """表示を最新にする。send(report) が False を返した行は次回また送る。"""
        with self._lock:
            key = tuple(
                (s["slot"], s["state"], s.get("summary"), s.get("detail"))
                for s in _attention(slots)
            )
            now = time.monotonic()
            if key != self._attention_key:
                self._attention_key, self._attention_since = key, now
            elapsed = int(now - self._attention_since)

            for row, line in enumerate(compose(slots, self._rows, self._cols, elapsed)):
                if self._sent.get(row, Line()) == line:
                    continue
                if send(build_text_report(row, line.text, line.invert)):
                    self._sent[row] = line
