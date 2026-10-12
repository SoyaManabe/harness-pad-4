"""パッドとの Raw HID 接続。

デバイスが繋がっていなくてもデーモンは動き続ける。再接続は自動で試みる。
パッド側も STATE が途切れたら通常のキーボードとして振る舞うため、
どちらが落ちても相手を巻き込まない (phase0/PROTOCOL.md §3)。

`hid` という名前のモジュールを出す PyPI のパッケージは 2 つあり、API が違う。
どちらが入っていても動くように、違いは下の 2 つのクラスで吸収する:

    hid      ctypes 版。hidapi 本体を別に入れる必要がある (macOS: brew install hidapi)
    hidapi   Cython 版。hidapi 本体を同梱している。Windows ではこちらを使う
"""

from __future__ import annotations

import logging
import sys
import threading

from .protocol import REPORT_SIZE, USAGE, USAGE_PAGE

log = logging.getLogger(__name__)

RECONNECT_INTERVAL_SEC = 2.0
READ_TIMEOUT_MS = 500

# hid を読めないときに、何を入れればよいか
INSTALL_HINT = (
    "`pip install hidapi` を確かめてください"
    if sys.platform == "win32"
    else "`pip install hid` と `brew install hidapi` を確かめてください"
)


class _CtypesDevice:
    """PyPI の `hid` で開いたデバイス。"""

    def __init__(self, hid_module, path: bytes) -> None:
        self._device = hid_module.Device(path=path)

    def read(self, size: int, timeout_ms: int) -> bytes:
        return bytes(self._device.read(size, timeout=timeout_ms))

    def write(self, data: bytes) -> None:
        self._device.write(data)

    def close(self) -> None:
        self._device.close()


class _CythonDevice:
    """PyPI の `hidapi` で開いたデバイス。"""

    def __init__(self, hid_module, path: bytes) -> None:
        self._device = hid_module.device()
        self._device.open_path(path)

    def read(self, size: int, timeout_ms: int) -> bytes:
        return bytes(self._device.read(size, timeout_ms))

    def write(self, data: bytes) -> None:
        # 失敗を例外ではなく負の戻り値で知らせる
        if self._device.write(data) < 0:
            raise OSError(self._device.error() or "write failed")

    def close(self) -> None:
        self._device.close()


def open_device(hid_module, path: bytes):
    """パスを指定してデバイスを開く。失敗はどちらのパッケージでも OSError にそろえる。"""
    backend = _CythonDevice if hasattr(hid_module, "device") else _CtypesDevice
    return _Normalized(backend, hid_module, path)


class _Normalized:
    """パッケージごとに違う例外を OSError にそろえる。

    `hid` の HIDException は OSError ではなく、`hidapi` は閉じた後の操作に ValueError を
    投げる。そろえておかないと、パッドを抜いたときの例外を呼び出し側が取りこぼす。
    """

    def __init__(self, backend, hid_module, path: bytes) -> None:
        self._inner = self._call(backend, hid_module, path)

    def read(self, size: int, timeout_ms: int) -> bytes:
        return self._call(self._inner.read, size, timeout_ms)

    def write(self, data: bytes) -> None:
        self._call(self._inner.write, data)

    def close(self) -> None:
        self._call(self._inner.close)

    @staticmethod
    def _call(function, *args):
        try:
            return function(*args)
        except OSError:
            raise
        except Exception as exc:
            raise OSError(str(exc) or type(exc).__name__) from exc


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
        # 送信に失敗した。閉じるのは受信スレッドに任せる (send を参照)
        self._broken = False
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
            if self._device is None or self._broken:
                return False
            try:
                # hidapi は先頭にレポート ID を要求する。QMK は 0 を使う。
                self._device.write(b"\x00" + report)
                return True
            except OSError as exc:
                log.warning("送信に失敗しました。切断とみなします: %s", exc)
                # ここでは閉じない。受信スレッドが同じデバイスで read の最中かもしれず、
                # その下で閉じると hidapi が解放済みのメモリに触る。
                self._broken = True
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
                    broken = self._broken
                if device is None:
                    continue
                if broken:
                    self._close()
                    continue
                data = device.read(REPORT_SIZE, READ_TIMEOUT_MS)
            except OSError as exc:
                log.warning("受信に失敗しました。切断とみなします: %s", exc)
                self._close()
                # すぐに開き直さない。開けるのに読めない状態が続くと、空回りしてログが溢れる
                self._stop.wait(RECONNECT_INTERVAL_SEC)
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
            log.error("Python の hid モジュールか hidapi ライブラリを読めません。%s", INSTALL_HINT)
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
            device = open_device(hid, path)
        except OSError as exc:
            log.warning("パッドを開けませんでした: %s", exc)
            return False

        with self._lock:
            self._device = device
            self._broken = False
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
            self._broken = False
