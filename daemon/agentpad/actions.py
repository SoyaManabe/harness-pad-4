"""パッドから届いた操作イベントに対する動作。

セッションキーだけは端末を前面に出す (focus.py)。それ以外は受信内容をログに出すだけにしてある。
キーストロークの送出などは 0-A の後半で、
「どのイベントが実際に使われるか」を実機で見てから実装する
(docs/phase0-spec.md §9 の問い 2・4)。

先に自動化を作り込まないのは意図的である。使われないキーに動作を実装しても無駄になる。
"""

from __future__ import annotations

import logging
import threading
import time

from .protocol import EventFlag, PadEvent
from .registry import Registry, SlotError

log = logging.getLogger(__name__)

# セッションキーをこれ以上押し続けたら長押しとみなす。
# 短いと普通の押下が割り当てに化け、長いと待たされる。
HOLD_SEC = 0.8

SESSION_EVENTS = {
    PadEvent.SESSION_0: 0,
    PadEvent.SESSION_1: 1,
    PadEvent.SESSION_2: 2,
    PadEvent.SESSION_3: 3,
}


class ActionHandler:
    """イベントを受けて何をするかを決める。"""

    def __init__(self, registry: Registry, metrics=None, focuser=None) -> None:
        self.registry = registry
        self.metrics = metrics
        self.focuser = focuser
        self.ptt_held = False
        self._hold_lock = threading.Lock()
        # slot -> (押下時刻, 長押し判定のタイマー)。押されている間だけ入っている
        self._held: dict[int, tuple[float, threading.Timer]] = {}

    def handle(self, event: PadEvent, value: int, flags: int = 0) -> None:
        if event in SESSION_EVENTS:
            self._session_key(SESSION_EVENTS[event], value, flags)
        elif event is PadEvent.AUTO_TOGGLE:
            self._auto_toggle(value)
        elif event is PadEvent.PTT:
            self._ptt(value)
        elif event is PadEvent.ENCODER:
            log.info("[未実装] スクロール: %+d", value)
        elif event is PadEvent.STOP:
            if value:
                log.warning("[未実装] 緊急停止")
        elif event is PadEvent.APPROVE_FORCE:
            if value:
                # 破壊的操作の承認。ファームウェア側で二段確認済みのはず。
                log.warning("[未実装] 承認（破壊的操作・二段確認済み）")
        elif value:  # 残りは押下時のみ反応
            log.info("[未実装] %s", event.name)

    def _session_key(self, slot: int, value: int, flags: int) -> None:
        """セッションキーの押下・解放を、短押しと長押しに振り分ける。

            短押し  そのセッションへ移る
            長押し  直近に使ったセッションをこのキーに割り当てる

        解放を送らないファームウェアでは長押しを判定できないので、
        押下をそのまま短押しとして扱う。
        """
        if not flags & EventFlag.SENDS_RELEASE:
            if value:
                self._select_session(slot, time.monotonic())
            return

        with self._hold_lock:
            pending = self._held.pop(slot, None)
            if value:
                timer = threading.Timer(HOLD_SEC, self._hold_elapsed, args=(slot,))
                timer.daemon = True
                self._held[slot] = (time.monotonic(), timer)
                timer.start()

        if pending is None:
            return  # 解放: 長押しとして処理済み
        pressed_at, timer = pending
        timer.cancel()
        if not value:
            self._select_session(slot, pressed_at)

    def _hold_elapsed(self, slot: int) -> None:
        with self._hold_lock:
            if self._held.pop(slot, None) is None:
                return  # 判定の直前に離された。短押しとして処理される
        self._assign_recent(slot)

    def _assign_recent(self, slot: int) -> None:
        session = self.registry.recent_session()
        if session is None:
            log.info("S%d を長押ししましたが、割り当てるセッションがありません", slot + 1)
            return
        try:
            self.registry.assign(slot, session_id=session.session_id, host=session.host)
        except SlotError as exc:
            log.warning("S%d への割り当てに失敗しました: %s", slot + 1, exc)

    def _select_session(self, slot: int, pressed_at: float) -> None:
        # 4 キー版ではセッションキーを押すこと自体が「気づいた」合図になる。
        # 専用の ACK キーは要らない（docs/p1p2-poc-plan.md §3 の役目を兼ねる）。
        # 押されたキーと入力待ちのスロットを突き合わせれば P2 の正答判定もできる。
        if self.metrics is not None:
            self.metrics.key_pressed(slot, at=pressed_at)

        session = self.registry.session_for_key(slot)
        if session is None:
            log.info("S%d は空きスロットです", slot + 1)
            return

        self.registry.set_target(session.slot)
        log.info("宛先を S%d (%s) に切り替えました", session.slot + 1, session.name)
        if self.focuser is None:
            return
        if session.host:
            # cwd はリモートのパスなので、手元のプロセスを探す手がかりにならない。
            # 偶然同じパスの手元のセッションを前面に出してしまわないよう、何もしない。
            log.info("%s はリモートのセッションです。端末を前面に出す機能は未対応です", session.name)
            return
        self.focuser.request(session.cwd, session.label)

    def _auto_toggle(self, value: int) -> None:
        if not value:
            return
        enabled = self.registry.toggle_auto_accept()
        log.info("auto-accept: %s", "有効" if enabled else "無効")

    def _ptt(self, value: int) -> None:
        held = bool(value)
        if held == self.ptt_held:
            return
        self.ptt_held = held
        log.info("PTT: %s", "押下" if held else "解放")
