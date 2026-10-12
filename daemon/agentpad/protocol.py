"""AgentPad Raw HID プロトコル v1 の定義。

phase0/PROTOCOL.md と一対一で対応する。ここを変えたらファームウェア側
(phase0/firmware/{poc4,agentpad}/keymaps/default/keymap.c) も必ず合わせること。
"""

from __future__ import annotations

from enum import IntEnum

PROTOCOL_VERSION = 0x01
REPORT_SIZE = 32

# QMK Raw HID の既定値
USAGE_PAGE = 0xFF60
USAGE = 0x61

MAX_SLOTS = 4
NO_SLOT = 0xFF

# TEXT メッセージ 1 通に載る文字数（32 バイト - ヘッダ 4 バイト）
TEXT_MAX_CHARS = REPORT_SIZE - 4
# パッドが保持できる表示行数の上限。ファームウェアの AP_TEXT_MAX_ROWS と一致させること。
TEXT_MAX_ROWS = 8


class MsgType(IntEnum):
    """メッセージ種別（レポート先頭バイト）。"""

    STATE = 0x01  # host -> pad
    TEXT = 0x02  # host -> pad
    HELLO_REQUEST = 0x03  # host -> pad
    EVENT = 0x81  # pad  -> host
    HELLO = 0x82  # pad  -> host


class SessionState(IntEnum):
    """セッションの状態。値は PROTOCOL.md の表と一致させること。"""

    EMPTY = 0
    IDLE = 1
    BUSY = 2
    WAITING = 3
    DONE = 4
    ERROR = 5


class Flag(IntEnum):
    """STATE メッセージのフラグビット。"""

    AUTO_ACCEPT = 1 << 0
    ANY_WAITING = 1 << 1
    DAEMON_ALIVE = 1 << 2


class TextFlag(IntEnum):
    """TEXT メッセージのフラグビット（オフセット 3）。"""

    INVERT = 1 << 0


class EventFlag(IntEnum):
    """EVENT メッセージのフラグビット（オフセット 4）。"""

    # このファームウェアはキーの解放も送る。
    # 立っていなければ押下しか届かないので、長押しは判定できない。
    SENDS_RELEASE = 1 << 0


class PadEvent(IntEnum):
    """パッドから届く操作イベント。"""

    SESSION_0 = 0x00
    SESSION_1 = 0x01
    SESSION_2 = 0x02
    SESSION_3 = 0x03

    APPROVE = 0x10
    APPROVE_FORCE = 0x11
    REJECT = 0x12
    PTT = 0x13
    AUTO_TOGGLE = 0x14

    STOP = 0x20

    HUNK_PREV = 0x30
    HUNK_NEXT = 0x31

    ENCODER = 0x40
    ENCODER_PUSH = 0x41


def build_state_report(
    states: list[SessionState],
    ages: list[int],
    flags: int,
    target_slot: int = NO_SLOT,
) -> bytes:
    """STATE メッセージを組み立てる。

    states / ages は MAX_SLOTS 個に切り詰め・ゼロ埋めされる。
    """
    report = bytearray(REPORT_SIZE)
    report[0] = MsgType.STATE
    report[1] = PROTOCOL_VERSION
    report[2] = sum(1 for s in states[:MAX_SLOTS] if s != SessionState.EMPTY)
    report[3] = flags & 0xFF

    for i in range(MAX_SLOTS):
        report[4 + i] = int(states[i]) if i < len(states) else SessionState.EMPTY
        # 経過秒は 255 で飽和させる（1 バイトのため）
        report[8 + i] = min(int(ages[i]), 255) if i < len(ages) else 0

    report[12] = target_slot & 0xFF
    return bytes(report)


def sanitize_text(text: str) -> str:
    """ディスプレイに出せる文字だけにする。

    パッドのフォントは ASCII の印字可能文字しか持たない。それ以外は '?' に置き換える。
    """
    return "".join(c if " " <= c <= "~" else "?" for c in text)


def build_text_report(row: int, text: str, invert: bool = False) -> bytes:
    """TEXT メッセージを組み立てる。1 通が表示 1 行に対応する。

    text は sanitize_text を通し、TEXT_MAX_CHARS 文字に切り詰める。
    空文字列ならその行を消す。
    """
    if not 0 <= row < TEXT_MAX_ROWS:
        raise ValueError(f"行番号は 0〜{TEXT_MAX_ROWS - 1} で指定してください: {row}")

    report = bytearray(REPORT_SIZE)
    report[0] = MsgType.TEXT
    report[1] = PROTOCOL_VERSION
    report[2] = row
    report[3] = TextFlag.INVERT if invert else 0
    body = sanitize_text(text)[:TEXT_MAX_CHARS].encode("ascii")
    report[4 : 4 + len(body)] = body
    return bytes(report)


def build_hello_request() -> bytes:
    """HELLO_REQUEST を組み立てる。パッドは HELLO を送り返す。"""
    report = bytearray(REPORT_SIZE)
    report[0] = MsgType.HELLO_REQUEST
    report[1] = PROTOCOL_VERSION
    return bytes(report)


def hello_display_size(data: bytes) -> tuple[int, int]:
    """HELLO が申告するディスプレイの (行数, 1 行の文字数)。無ければ (0, 0)。

    ディスプレイを持たないファームウェアは予約領域のゼロを返す。
    """
    if len(data) < 4 or data[0] != MsgType.HELLO:
        return 0, 0
    rows, cols = data[2], data[3]
    if rows == 0 or cols == 0:
        return 0, 0
    return min(rows, TEXT_MAX_ROWS), min(cols, TEXT_MAX_CHARS)


def pad_event_flags(data: bytes) -> int:
    """EVENT メッセージのフラグ。古いファームウェアは予約領域のゼロを返す。"""
    if len(data) < 5 or data[0] != MsgType.EVENT:
        return 0
    return data[4]


def parse_pad_report(data: bytes) -> tuple[MsgType, PadEvent | None, int]:
    """パッドから届いたレポートを (種別, イベント, 値) に分解する。

    未知のメッセージや短すぎるレポートでは例外を投げず、値を None にして返す。
    デーモンはファームウェアのバージョン差で落ちてはならない。
    """
    if len(data) < 4:
        raise ValueError(f"レポートが短すぎます: {len(data)} バイト")

    try:
        msg = MsgType(data[0])
    except ValueError as exc:
        raise ValueError(f"未知のメッセージ種別: 0x{data[0]:02X}") from exc

    if msg is MsgType.HELLO:
        return msg, None, 0

    if msg is not MsgType.EVENT:
        raise ValueError(f"パッドから届く想定のない種別です: {msg.name}")

    try:
        event = PadEvent(data[2])
    except ValueError:
        # 未知のイベントは黙って捨てる（新しいファームが古いデーモンに繋がった場合）
        return msg, None, 0

    value = data[3]
    if event is PadEvent.ENCODER:
        # エンコーダの増減は符号付き
        value = value - 256 if value > 127 else value

    return msg, event, value
