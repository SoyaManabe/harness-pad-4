"""AgentPad デーモン。

Claude Code のフックからセッション状態を受け取り、Raw HID でパッドへ送る。
パッドからの操作イベントを受け取って動作させる。

    python -m agentpad --vid 0xFEED --pid 0x0001

パッドが繋がっていなくても起動する。--no-hid を付ければ HID を使わず、
フックが届いているかどうかだけを確認できる。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from pathlib import Path

from .actions import ActionHandler
from .display import PadDisplay
from .focus import Focuser
from .hid_link import HidLink
from .hook_server import start_hook_server
from .protocol import (
    MsgType,
    build_hello_request,
    hello_display_size,
    pad_event_flags,
    parse_pad_report,
)
from .registry import Registry

try:
    from .metrics import Metrics
except ImportError:
    # 計測は実験用の機能で、配布物には含めない (release/build.sh)。無ければ計測しない。
    Metrics = None

log = logging.getLogger("agentpad")

# STATE の再送間隔。パッド側のハートビート判定より十分短くすること。
# ディスプレイの経過時間と本文の送りもこの周期で進む。
HEARTBEAT_INTERVAL_SEC = 1.0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    wake = threading.Event()
    aggregated = args.slots == 1
    metrics = Metrics(args.metrics, args.condition, aggregated=aggregated) if Metrics else None
    registry = Registry(on_change=wake.set, metrics=metrics, display_slots=args.slots)
    focuser = None if args.no_focus else Focuser()
    actions = ActionHandler(registry, metrics=metrics, focuser=focuser)
    display = PadDisplay()

    if aggregated:
        log.info(
            "LED 1 個の構成。全セッションのうち最も対応が必要な状態を表示します "
            "(優先順: WAITING > ERROR > DONE > BUSY > IDLE)"
        )
        if metrics is not None:
            log.info("この構成では P2（セッションの同定）は評価できません。P1 のみ検証します")

    link: HidLink | None = None
    if not args.no_hid:
        link = HidLink(
            vendor_id=args.vid,
            product_id=args.pid,
            on_report=lambda data: _on_pad_report(data, actions, display, wake),
            # 起動済みのパッドに後から繋いだ場合、HELLO はもう送られた後である。
            # ディスプレイの大きさを知るために送り直してもらう。
            on_connect=lambda: link.send(build_hello_request()),
        )
        link.start()
    else:
        log.info("--no-hid: HID を使わずフックの受信のみ確認します")

    server = start_hook_server(registry, args.host, args.port)

    stopping = threading.Event()

    def _shutdown(signum, _frame) -> None:
        log.info("シグナル %s を受信しました。終了します", signal.Signals(signum).name)
        stopping.set()
        wake.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("待機中。Ctrl-C で終了します")
    try:
        _run_loop(registry, link, display, wake, stopping)
    finally:
        server.shutdown()
        server.server_close()
        if link:
            link.stop()
        log.info("終了しました")

    return 0


def _run_loop(
    registry: Registry,
    link: HidLink | None,
    display: PadDisplay,
    wake: threading.Event,
    stopping: threading.Event,
) -> None:
    """状態が変わったとき、および一定間隔で STATE を送り続ける。

    定期送信はハートビートを兼ねる。これが途切れるとパッドは LED を消し、
    通常のキーボードとして振る舞う (phase0/PROTOCOL.md §3)。

    ディスプレイの文字 (TEXT) は変わった行だけを送る。経過時間の表示は
    この周期で進む。
    """
    last_description = ""

    while not stopping.is_set():
        wake.wait(timeout=HEARTBEAT_INTERVAL_SEC)
        wake.clear()

        if stopping.is_set():
            break

        registry.tick()

        description = registry.describe()
        if description != last_description:
            log.info("状態: %s", description)
            last_description = description

        if link is not None:
            link.send(registry.build_report())
            display.update(registry.snapshot(with_detail=True), link.send)


def _on_pad_report(
    data: bytes,
    actions: ActionHandler,
    display: PadDisplay,
    wake: threading.Event,
) -> None:
    try:
        msg, event, value = parse_pad_report(data)
    except ValueError as exc:
        log.warning("パッドからのレポートを解釈できませんでした: %s", exc)
        return

    if msg is MsgType.HELLO:
        # パッドが起動し直した。現在の状態を即座に送り返して同期させる。
        rows, cols = hello_display_size(data)
        display.configure(rows, cols)
        if rows:
            log.info(
                "パッドから HELLO を受信しました (ディスプレイ %d 行 × %d 文字)。状態を再送します",
                rows, cols,
            )
        else:
            log.info("パッドから HELLO を受信しました (ディスプレイなし)。状態を再送します")
        wake.set()
        return

    if event is None:
        return

    actions.handle(event, value, pad_event_flags(data))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentpad", description="AgentPad デーモン")
    parser.add_argument(
        "--vid",
        type=lambda s: int(s, 0),
        default=0xFEED,
        help="パッドのベンダ ID (既定: 0xFEED)",
    )
    parser.add_argument(
        "--pid",
        type=lambda s: int(s, 0),
        default=0x0001,
        help="パッドのプロダクト ID (既定: 0x0001)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="フック受信の待受アドレス")
    parser.add_argument("--port", type=int, default=8787, help="フック受信のポート")
    parser.add_argument("--no-hid", action="store_true", help="HID を使わずフック受信のみ行う")
    parser.add_argument(
        "--slots",
        type=int,
        default=4,
        choices=[1, 2, 3, 4],
        help="物理的に存在する LED の数。1 なら全セッションを 1 個に集約 (既定: 4)",
    )
    parser.add_argument(
        "--no-focus",
        action="store_true",
        help="セッションキーを押しても端末を前面に出さない",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="デバッグログを出す")
    if Metrics is not None:
        parser.add_argument(
            "--metrics",
            type=Path,
            default=None,
            help="計測ログの出力先 (JSONL)。指定しなければ計測しない",
        )
        parser.add_argument(
            "--condition",
            default="B",
            choices=["A", "B", "C"],
            help="実験条件。A = LED なし(基準) / B = LED あり / C = LED + ディスプレイ (既定: B)",
        )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
