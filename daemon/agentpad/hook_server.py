"""エージェントのイベントと、スロット操作を受ける HTTP サーバ。

    POST /hook      Claude Code のフックの JSON。デーモンが共通形式に直す
    POST /event     共通形式のイベント (phase0/EVENTS.md)。ほかのエージェントはこちらへ
    POST /assign    {"slot": 2, "session_id": "...", "cwd": "...", "host": "..."}
                                                         セッションをスロットへ
    POST /release   {"slot": 2}                          スロットを空ける
    GET  /status    現在の割り当て

/hook と /event は、どのマシンからのイベントかを `?host=名前` で受ける。
省けばデーモンと同じマシンとして扱う。

スロット番号はキーの刻印と同じ 1 始まり。
ローカルホストにのみ束縛する。外部に開けてはならない。
リモートのマシンからは、SSH の転送などでローカルホストに届ける。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import claude_hooks
from .events import normalize_host, parse_event
from .registry import SlotError

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentPad/1.0"
    registry = None  # サーバ生成時に差し込む

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler の規約)
        path, host = self._route()
        if path in ("/assign", "/release"):
            self._slot_command(path)
            return
        if path not in ("/hook", "/event", ""):
            self._respond(404, {"error": "not found"})
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            if path == "/event":
                event = parse_event(payload, host)
            else:
                event = claude_hooks.from_hook(payload, host)
            self.registry.handle_event(event)
        except ValueError as exc:
            log.warning("イベントを無視しました (%s): %s", exc, payload)
        except Exception:
            log.exception("イベントの処理中に例外が発生しました")
        # 失敗しても 200 を返す。エラーを返すとエージェントの動作を妨げうる。
        # このデーモンは監視用であり、エージェントの実行を止めてはならない。

        self._respond(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        """動作確認用。ブラウザや curl で状態を見られるようにしておく。"""
        if self._route()[0] in ("/status", ""):
            self._respond(
                200,
                {
                    "ok": True,
                    "sessions": self.registry.describe(),
                    "slots": self.registry.snapshot(),
                },
            )
        else:
            self._respond(404, {"error": "not found"})

    def _route(self) -> tuple[str, str]:
        """要求のパスと、`?host=` で渡されたホスト名。"""
        url = urlsplit(self.path)
        host = parse_qs(url.query).get("host", [""])[0]
        return url.path.rstrip("/"), host

    def _slot_command(self, path: str) -> None:
        payload = self._read_json()
        if payload is None:
            return

        slot = payload.get("slot")
        if not isinstance(slot, int) or isinstance(slot, bool):
            self._respond(400, {"ok": False, "error": "slot を整数で指定してください"})
            return

        try:
            if path == "/assign":
                session = self.registry.assign(
                    slot - 1,
                    session_id=payload.get("session_id") or None,
                    cwd=payload.get("cwd") or None,
                    # 省けばマシンを問わずに探す
                    host=normalize_host(payload["host"]) if "host" in payload else None,
                )
            else:
                session = self.registry.release(slot - 1)
        except SlotError as exc:
            self._respond(409, {"ok": False, "error": str(exc)})
            return

        self._respond(
            200,
            {"ok": True, "label": session.name, "sessions": self.registry.describe()},
        )

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._respond(400, {"error": "invalid Content-Length"})
            return None

        if length <= 0:
            # http フックが本文を送らない場合ここに来る。
            # 0-A ではこの警告が出るかどうか自体が検証項目 (V-1)。
            log.warning("本文のない POST を受信しました。送信側の設定を確認してください")
            self._respond(400, {"error": "empty body"})
            return None

        if length > MAX_BODY_BYTES:
            self._respond(413, {"error": "body too large"})
            return None

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("本文を JSON として解釈できませんでした: %s", exc)
            self._respond(400, {"error": "invalid json"})
            return None

        if not isinstance(payload, dict):
            self._respond(400, {"error": "expected a JSON object"})
            return None

        return payload

    def _respond(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        # 既定の stderr 直書きをやめて logging に流す
        log.debug("http: " + fmt, *args)


def start_hook_server(registry, host: str, port: int) -> ThreadingHTTPServer:
    """イベント受信サーバを起動する。呼び出し側が shutdown() すること。"""
    handler = type("Handler", (_Handler,), {"registry": registry})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="hook-server", daemon=True)
    thread.start()
    log.info("イベント受信を開始しました: http://%s:%d/hook (共通形式は /event)", host, port)
    return server
