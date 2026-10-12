"""パッドとの Raw HID 接続。

デバイスが繋がっていなくてもデーモンは動き続ける。再接続は自動で試みる。
パッド側も STATE が途切れたら通常のキーボードとして振る舞うため、
どちらが落ちても相手を巻き込まない (phase0/PROTOCOL.md §3)。
"""

from __future__ import annotations

import logging
import threading
import time

from .protocol import REPORT_SIZE, USAGE, USAGE_PAGE

log = logging.getLogger(__name__)

RECONNECT_INTERVAL_SEC = 2.0
READ_TIMEOUT_MS = 500


class HidLink:
    """パッドへの接続を保持し、送受信する。

    on_report(data: bytes) と on_connect() は受信スレッドから呼ばれる。
    """

    def __init__(self, vendor_id: int, product_id: int, on_report=None, on_connect=None) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self._on_report = on_report
        self._on_connect = on_connect

        self._device = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_connected = False
        self._warned_missing = False

    # ------------------------------------------------------------- ライフサイクル

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hid-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._close()

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._device is not None

    # ------------------------------------------------------------------- 送信

    def send(self, report: bytes) -> bool:
        """32 バイトのレポートを送る。未接続なら False を返すだけで例外は投げない。"""
        if len(report) != REPORT_SIZE:
            raise ValueError(f"レポート長は {REPORT_SIZE} バイトである必要があります: {len(report)}")

        with self._lock:
            if self._device is None:
                return False
            try:
                # hidapi は先頭にレポート ID を要求する。QMK は 0 を使う。
                self._device.write(b"\x00" + report)
                return True
            except OSError as exc:
                log.warning("送信に失敗しました。切断とみなします: %s", exc)
                self._close_locked()
                return False

    # ------------------------------------------------------------------- 受信

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.connected:
                if not self._try_connect():
                    self._stop.wait(RECONNECT_INTERVAL_SEC)
                    continue

            try:
                with self._lock:
                    device = self._device
                if device is None:
                    continue
                data = device.read(REPORT_SIZE, timeout=READ_TIMEOUT_MS)
            except OSError as exc:
                log.warning("受信に失敗しました。切断とみなします: %s", exc)
                self._close()
                continue
            except Exception:
                # OSError 以外（ライブラリの API 差異など）でスレッドを死なせない。
                # 死ぬと接続済みのまま無反応になり、原因が分からなくなる。
                log.exception("受信で予期しない例外が発生しました。切断して再接続します")
                self._close()
                self._stop.wait(RECONNECT_INTERVAL_SEC)
                continue

            if data and self._on_report:
                try:
                    self._on_report(bytes(data))
                except Exception:
                    # ハンドラの例外で受信スレッドを殺さない
                    log.exception("受信ハンドラで例外が発生しました")

    # ----------------------------------------------------------------- 接続管理

    def _try_connect(self) -> bool:
        try:
            import hid
        except ImportError:
            log.error(
                "Python の hid パッケージか hidapi ライブラリを読めません。"
                "`pip install hid` と `brew install hidapi` を確かめてください"
            )
            self._stop.set()
            return False

        path = self._find_interface(hid)
        if path is None:
            if self._was_connected or not self._warned_missing:
                log.info(
                    "パッド (VID=0x%04X PID=0x%04X の Raw HID) が見つかりません。"
                    "接続を待機します",
                    self.vendor_id,
                    self.product_id,
                )
                self._was_connected = False
                self._warned_missing = True
            return False

        try:
            device = hid.Device(path=path)
        except OSError as exc:
            log.warning("パッドを開けませんでした: %s", exc)
            return False

        with self._lock:
            self._device = device
        self._was_connected = True
        self._warned_missing = False
        log.info("パッドに接続しました (VID=0x%04X PID=0x%04X)", self.vendor_id, self.product_id)
        if self._on_connect:
            try:
                self._on_connect()
            except Exception:
                log.exception("接続時の処理で例外が発生しました")
        return True

    def _find_interface(self, hid_module) -> bytes | None:
        """Raw HID インタフェースのパスを探す。

        QMK のキーボードは複数のインタフェースを出すので、VID/PID だけでは
        通常のキーボード側を掴んでしまう。usage_page で絞り込む必要がある。
        """
        for info in hid_module.enumerate(self.vendor_id, self.product_id):
            if info.get("usage_page") == USAGE_PAGE and info.get("usage") == USAGE:
                return info["path"]
        return None

    def _close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except OSError:
                pass
            self._device = None
