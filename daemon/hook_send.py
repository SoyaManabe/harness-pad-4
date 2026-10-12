"""Claude Code のフックの内容を、そのままデーモンへ送る（Windows 用）。

    python hook_send.py [PORT]

標準入力で受けた JSON を POST するだけ。macOS と Linux では同じことを curl で行う
(hooks.example.json)。Windows ではフックを動かすシェルが環境によって違う
（Git Bash か PowerShell）ので、シェルを介さずに起動できるこのスクリプトを使う。

何が起きても何も出力せず、終了コード 0 で終わる。フックの出力や失敗は
エージェントの動作に影響しうるが、このフックは監視のためのものであり、
エージェントを妨げてはならない。
"""

import socket
import sys
import urllib.parse
import urllib.request

DEFAULT_PORT = 8787

# デーモンが止まっているときに、エージェントを待たせない
TIMEOUT_SEC = 2.0


def main() -> int:
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
        body = sys.stdin.buffer.read()
        host = urllib.parse.quote(socket.gethostname())
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/hook?host={host}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # 環境にプロキシが設定されていても、ローカルホストへは直接繋ぐ
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(request, timeout=TIMEOUT_SEC).read()
    except BaseException:  # noqa: BLE001 (失敗は捨てる。上の説明を参照)
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
