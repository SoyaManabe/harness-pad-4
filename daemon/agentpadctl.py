"""agentpadctl.py — AgentPad デーモンの常駐化と操作（Windows 用）。

    agentpadctl install      ログイン時に自動で起動するよう登録して、起動する
    agentpadctl uninstall    登録を外して止める
    agentpadctl start|stop|restart
    agentpadctl status       稼働状況とスロットの割り当て
    agentpadctl assign N [SESSION_ID] [CWD] [HOST]
                             セッションを S<N> へ。先客がいれば入れ替える。
                             SESSION_ID を省くと、直近に発言したセッション。
                             HOST を省くと、マシンを問わずに探す
    agentpadctl release N    S<N> を空ける
    agentpadctl logs         ログを追う
    agentpadctl check        hidapi を読めるか、パッドが見えるか、フックが登録されているかを調べる

同じフォルダの agentpadctl.cmd から呼ぶ。macOS では agentpadctl（launchd 版）を使う。

起動引数は下の既定値で決まる。環境変数で上書きして install し直せば変えられる:

    set AGENTPAD_PORT=8788 & agentpadctl install

macOS の launchd と違い、落ちたデーモンを起こし直す仕組みは無い。
止まっていたら agentpadctl start で起動する。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ───────────────────────────────── 設定

# パッドのプロダクト ID
DEFAULT_PAD_PID = "0x0005"
# 物理的に存在する LED の数
DEFAULT_SLOTS = "4"
DEFAULT_PORT = "8787"

PAD_VID = 0xFEED
# パッドの版ごとのプロダクト ID と、LED の数（1 個の版は全セッションを 1 個に集約する）
PAD_VERSIONS = {0x0004: ("ブレッドボード版", "1"), 0x0005: ("基板版", "4")}
# パッドの Raw HID インタフェース (agentpad/protocol.py と同じ値)
RAW_HID_USAGE_PAGE = 0xFF60

# ログイン時に起動するプログラムを並べるレジストリのキーと、そこに置く名前
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AgentPad"
# Run キーの値は 260 文字までしか読まれない
RUN_VALUE_MAX_CHARS = 260

DAEMON_DIR = Path(__file__).resolve().parent
# ログと、install したときの起動引数を置く場所
STATE_DIR = Path(
    os.environ.get("AGENTPAD_STATE_DIR")
    or Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "state") / "AgentPad"
)
LOG = STATE_DIR / "agentpad.log"
SETTINGS = STATE_DIR / "service.json"

HTTP_TIMEOUT_SEC = 3.0
# 起動・停止を待つ上限と、確かめる間隔
WAIT_SEC = 8.0
POLL_SEC = 0.25
LOG_TAIL_LINES = 30

IS_WINDOWS = sys.platform == "win32"

# ───────────────────────────────── 部品


def die(message: str) -> None:
    sys.exit(f"agentpadctl: {message}")


def settings() -> dict:
    """起動引数。install のときに決めて保存し、以後はそれを使う。"""
    try:
        saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {
        "pid": saved.get("pid") or os.environ.get("AGENTPAD_PID") or DEFAULT_PAD_PID,
        "slots": saved.get("slots") or os.environ.get("AGENTPAD_SLOTS") or DEFAULT_SLOTS,
        "port": saved.get("port") or os.environ.get("AGENTPAD_PORT") or DEFAULT_PORT,
    }


def save_settings() -> dict:
    """環境変数と既定値から起動引数を決め直して保存する。"""
    SETTINGS.unlink(missing_ok=True)
    chosen = settings()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(chosen, indent=2), encoding="utf-8")
    return chosen


def url(path: str) -> str:
    return f"http://127.0.0.1:{settings()['port']}{path}"


def request(path: str, body: dict | None = None) -> dict | None:
    """デーモンに問い合わせる。繋がらなければ None。"""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url(path), data=data, headers={"Content-Type": "application/json"})
    # 環境にプロキシが設定されていても、ローカルホストへは直接繋ぐ
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT_SEC) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # 割り当ての失敗などは、理由を本文に入れて 4xx で返ってくる
        try:
            return json.loads(exc.read())
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except (OSError, ValueError):
        return None


def wait_until(condition) -> bool:
    deadline = time.monotonic() + WAIT_SEC
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(POLL_SEC)
    return condition()


def daemon_python() -> str:
    """デーモンを動かす Python。専用の環境 (.venv) があればそれを使う。

    Windows では画面を出さない pythonw を選ぶ。python だとコンソールの窓が開く。
    """
    if IS_WINDOWS:
        candidates = [
            DAEMON_DIR / ".venv" / "Scripts" / "pythonw.exe",
            Path(sys.executable).with_name("pythonw.exe"),
        ]
    else:
        candidates = [DAEMON_DIR / ".venv" / "bin" / "python3"]
    return str(next((c for c in candidates if c.exists()), sys.executable))


def daemon_command() -> list[str]:
    chosen = settings()
    return [
        daemon_python(), "-m", "agentpad",
        "--pid", chosen["pid"],
        "--slots", chosen["slots"],
        "--port", chosen["port"],
        "--log", str(LOG),
    ]  # fmt: skip


def spawn(command: list[str]) -> None:
    """デーモンを、このコマンドが終わっても動き続ける形で起動する。"""
    options = {
        "cwd": DAEMON_DIR,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if not IS_WINDOWS:
        subprocess.Popen(command, start_new_session=True, **options)
        return

    detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # 呼び出し元が子プロセスをまとめて終わらせる仕組み（ジョブ）の中にいると、
        # このコマンドが終わったときにデーモンも道連れになる。抜けられるなら抜ける。
        subprocess.Popen(
            command, creationflags=detached | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options
        )
    except OSError:
        # 抜けることを許していないジョブでは、指定しただけで起動に失敗する
        subprocess.Popen(command, creationflags=detached, **options)


def kill(pid: int) -> None:
    if IS_WINDOWS:
        # Windows には、画面を持たないプロセスに終了を頼む手段が無いので強制終了する。
        # デーモンが消えると、パッドは約 10 秒で LED を消す。
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
        )
    else:
        os.kill(pid, signal.SIGTERM)


# ───────────────────────────────── 自動起動の登録 (Windows)


def run_key(write: bool = False):
    import winreg

    access = winreg.KEY_SET_VALUE if write else winreg.KEY_QUERY_VALUE
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def registered() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with run_key() as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def register() -> None:
    import winreg

    # ログイン時にはこのスクリプトの start を呼ぶ。起動のしかたを 1 か所にまとめるため
    value = subprocess.list2cmdline([daemon_python(), str(Path(__file__).resolve()), "start"])
    if len(value) > RUN_VALUE_MAX_CHARS:
        die(
            f"このフォルダの場所が深すぎて、自動起動を登録できません（{len(value)} 文字。"
            f"上限は {RUN_VALUE_MAX_CHARS} 文字）。浅い場所へ移してやり直してください"
        )
    with run_key(write=True) as key:
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, value)


def unregister() -> None:
    import winreg

    try:
        with run_key(write=True) as key:
            winreg.DeleteValue(key, RUN_NAME)
    except FileNotFoundError:
        pass


# ───────────────────────────────── コマンド


def start() -> None:
    if request("/status") is not None:
        print("すでに動いています")
        return
    spawn(daemon_command())
    if not wait_until(lambda: request("/status") is not None):
        die(f"起動しましたが応答がありません。ログを確かめてください: {LOG}")
    print("起動しました")


def stop() -> None:
    reply = request("/status")
    if reply is None:
        print("動いていません")
        return
    pid = reply.get("pid")
    if not isinstance(pid, int):
        die("動いているデーモンが古く、止める相手を確かめられません。手動で終了してください")
    kill(pid)
    if not wait_until(lambda: request("/status") is None):
        die(f"止まりません (PID {pid})")
    print("止めました（次のログイン時、または agentpadctl start で再び起動します）")


def status() -> int:
    if IS_WINDOWS:
        print("自動起動: " + ("登録済み" if registered() else "未登録"))
    reply = request("/status")
    if reply is None:
        print(f"デーモン: 停止中（{url('')} から応答がありません）")
        return 1
    print(f"デーモン: 稼働中 (PID {reply.get('pid', '?')})")
    if reply.get("pad") is not None:
        chosen = settings()
        print(
            f"パッド: 接続中 (PID {chosen['pid']}, LED {chosen['slots']} 個)"
            if reply["pad"]
            else f"パッド: 未接続（PID {chosen['pid']} を探しています。agentpadctl check で調べられます）"
        )
    for row in reply["slots"]:
        mark = "*" if row["target"] else " "
        if row["state"] == "EMPTY":
            print(f"  S{row['slot']}{mark} (空き)")
            continue
        where = f"{row['host']}:{row['cwd']}" if row.get("host") else row["cwd"]
        print(
            f"  S{row['slot']}{mark} {row['state']:<8} {row['label']:<16} "
            f"{where}  [{row['session_id'][:8]}]"
        )
    warning = hook_problem()
    if warning:
        # セッションが出ない理由として最も多い。直るまで毎回出す
        print(f"警告: {warning}")
        print("      install.py をやり直すと登録されます")
    return 0


def hook_problem() -> str | None:
    """Claude Code にフックが登録されていなければ、その説明。"""
    if not IS_WINDOWS:
        return None  # macOS のフックは curl で送る別の形で、ここでは調べない
    import hook_config

    return hook_config.problem(settings()["port"])


def slot_command(path: str, body: dict, message: str) -> None:
    reply = request(path, body)
    if reply is None:
        die("デーモンに繋がりません。agentpadctl status で確認してください")
    if not reply.get("ok"):
        die("失敗: " + reply.get("error", "不明なエラー"))
    print(message.format(**reply))
    print(reply.get("sessions", ""))


def slot_arg(args: list[str]) -> int:
    if not args or args[0] not in ("1", "2", "3", "4"):
        die("スロットは 1〜4 で指定してください")
    return int(args[0])


def assign(args: list[str]) -> None:
    slot = slot_arg(args)
    session_id, cwd, host = (args[1:4] + ["", "", ""])[:3]
    body = {"slot": slot, "session_id": session_id, "cwd": cwd}
    if host:
        body["host"] = host
    slot_command("/assign", body, f"{{label}} を S{slot} に割り当てました")


def release(args: list[str]) -> None:
    slot = slot_arg(args)
    slot_command("/release", {"slot": slot}, f"S{slot} ({{label}}) を空けました")


def logs() -> None:
    """ログの末尾を出し、続きを追う。Ctrl-C で終わる。"""
    if not LOG.exists():
        die(f"ログがまだありません: {LOG}")
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-LOG_TAIL_LINES:]), flush=True)
    position = LOG.stat().st_size
    try:
        while True:
            time.sleep(1.0)
            size = LOG.stat().st_size if LOG.exists() else 0
            if size < position:
                position = 0  # 上限に達して新しいファイルに切り替わった
            if size == position:
                continue
            # 開きっぱなしにしない。Windows では、開いているあいだログの切り替えを妨げる
            with LOG.open("rb") as stream:
                stream.seek(position)
                chunk = stream.read()
            position += len(chunk)
            print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
    except KeyboardInterrupt:
        pass


def check() -> int:
    """動く前提が揃っているかを調べる。デバイスは開かない。"""
    warning = hook_problem()
    if warning:
        print(f"フック: {warning}")
        print("  install.py をやり直すと登録されます")
    elif IS_WINDOWS:
        print("フック: 登録済み（登録した直後は、Claude Code で /hooks を開くと読み込まれます）")
    result = check_pad()
    return 1 if warning else result


def check_pad() -> int:
    try:
        import hid
    except ImportError as exc:
        print(f"hid モジュールを読めません: {exc}")
        print("  install.py をやり直すか、この Python で pip install hidapi を実行してください")
        return 1
    print(f"hid モジュール: 読めました ({getattr(hid, '__file__', '?')})")
    try:
        import psutil  # noqa: F401 (読めるかどうかだけを見る)
    except ImportError:
        print("psutil: 読めません。キーを押しても端末が前に出ません。install.py をやり直してください")

    product_id = int(settings()["pid"], 0)
    # プロダクト ID を問わずに探す。別の版が繋がっていることに気づけるように
    pads = {
        info["product_id"]: info
        for info in hid.enumerate(PAD_VID, 0)
        if info.get("usage_page") == RAW_HID_USAGE_PAGE
    }
    name = f"VID=0x{PAD_VID:04X} PID=0x{product_id:04X}"
    if product_id in pads:
        print(f"パッド ({name}): 見つかりました ({pads[product_id].get('product_string') or '名前なし'})")
        return 0

    print(f"パッド ({name}): 見つかりません")
    if not pads:
        print("  USB で繋ぐと、デーモンが自動で接続します")
    for other in sorted(pads):
        version, slots = PAD_VERSIONS.get(other, ("別の版", settings()["slots"]))
        print(f"  {version}のパッド (PID=0x{other:04X}) が繋がっています。次のように入れ直すと繋がります:")
        if IS_WINDOWS:
            print(f"    set AGENTPAD_PID=0x{other:04X}& set AGENTPAD_SLOTS={slots}& daemon\\agentpadctl install")
        else:
            print(f"    AGENTPAD_PID=0x{other:04X} AGENTPAD_SLOTS={slots} ./daemon/agentpadctl install")
    return 1


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        # 画面以外（Claude Code など）へ出すときは UTF-8 にそろえる。
        # 任せると Windows の既定の文字コードになり、受け取る側で化ける。
        if stream is not None and not stream.isatty():
            stream.reconfigure(encoding="utf-8")

    command, args = (argv[0], argv[1:]) if argv else ("", [])

    if command in ("install", "uninstall") and not IS_WINDOWS:
        die("このコマンドは Windows 用です。macOS では agentpadctl を使ってください")

    if command == "install":
        if request("/status") is not None:
            stop()
        chosen = save_settings()
        register()
        start()
        print(f"自動起動に登録しました (PID {chosen['pid']}, LED {chosen['slots']} 個, ポート {chosen['port']})")
        print(f"ログ: {LOG}")
    elif command == "uninstall":
        stop()
        unregister()
        SETTINGS.unlink(missing_ok=True)
        print("登録を外しました")
    elif command == "start":
        start()
    elif command == "stop":
        stop()
    elif command == "restart":
        if request("/status") is not None:
            stop()
        start()
    elif command == "status":
        return status()
    elif command == "assign":
        assign(args)
    elif command == "release":
        release(args)
    elif command == "logs":
        logs()
    elif command == "check":
        return check()
    else:
        print(__doc__.split("\n\n")[1])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
