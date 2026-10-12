"""エージェントのイベントを、パッドに出せる 4 スロットの状態へ集約する。

読むのは共通のイベント形式 (events.py) だけで、エージェントの種類は知らない。
セッションの ID は長くて人間には扱えないので、スロット（S1〜S4 のキー）へ
割り当てて番号で扱えるようにする。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

from .events import AgentEvent, EventKind, Request, RequestKind
from .protocol import MAX_SLOTS, NO_SLOT, Flag, SessionState, build_state_report

log = logging.getLogger(__name__)

# 完了(DONE)を何秒で待機(IDLE)に戻すか。
# 緑が永久に点いたままだと「完了したばかり」の情報価値が失われるため。
DONE_DECAY_SEC = 30.0

# この状態のあいだだけ、何を求められているかの説明 (detail) を持つ。
DETAIL_STATES = {SessionState.WAITING, SessionState.ERROR}

# 表示名 (label) の上限。作業フォルダ名から作る。
LABEL_MAX_CHARS = 16

# 作業フォルダの区切り。リモートのパスは、デーモンが動いている OS の流儀とは限らない。
PATH_SEPARATORS = re.compile(r"[\\/]")

# detail の上限。ディスプレイは入りきらない分を 1 行ずつ送って見せるので、
# 長すぎると一巡に時間がかかりすぎる。
DETAIL_MAX_CHARS = 200

# detail を上限で切ったことを示す末尾の印
DETAIL_TRUNCATION_MARK = ".."

# 取り返しのつかない操作によくある形。当てはまれば表示に印を付ける。
#
# 判定は規則で行い、エージェントが書いた説明文は見ない。説明を書くのは許可を
# 求めている本人であり、説明が穏当でもコマンドが危険なことはありうるため。
# あくまで目を引くための手がかりで、印が無いことは安全を意味しない。
DANGER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brm\s+(\S+\s+)*-[a-z]*[rf]",  # rm -r / rm -f
        r"\bsudo\b",
        r"--force\b",  # --force-with-lease も含む
        r"\bgit\s+push\b.*\s-[a-z]*f\b",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\b",
        r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)",
        r"\bdd\b.*\bof=",
        r"\bmkfs",
        r"\b(chmod|chown)\s+(\S+\s+)*-[a-z]*R",
        r"\|\s*(sudo\s+)?(ba|z)?sh\b",  # curl ... | sh
        r"\b(drop|truncate)\s+(table|database|schema)\b",
        r"\bdelete\s+from\b",
        r">\s*/dev/(sd|disk|nvme)",
    )
)

class SlotError(ValueError):
    """スロットの割り当て・解放ができなかった。メッセージはそのまま利用者に見せる。"""

# 表示を 1 スロットに集約するときの優先順位。
# LED が 1 個しかない場合、「いま最も対応が必要な状態」を出す必要がある。
# 対応を要する WAITING / ERROR を上位に、情報提供にすぎない BUSY を下位に置く。
AGGREGATE_PRIORITY = [
    SessionState.WAITING,
    SessionState.ERROR,
    SessionState.DONE,
    SessionState.BUSY,
    SessionState.IDLE,
]


@dataclass(frozen=True)
class Detail:
    """対応が必要なセッションが、何を求めているか。"""

    # エージェントが付けた短い説明（例: "Force push"）。無ければ空
    summary: str = ""
    # 求めている操作そのもの（例: "$ git push --force"、"Edit: src/app.py"）
    text: str = ""
    # 取り返しのつかない操作らしい (DANGER_PATTERNS)
    danger: bool = False


NO_DETAIL = Detail()


@dataclass
class Session:
    """1 スロット分のセッション情報。"""

    session_id: str
    slot: int
    state: SessionState = SessionState.IDLE
    label: str = ""
    # エージェントが動いているマシン。デーモンと同じマシンなら空
    host: str = ""
    # イベントを送ってきた変換の名前（例: "claude-code"）
    agent: str = ""
    # 最初に届いたイベントの cwd。端末を探す手がかりにする (focus.py)
    cwd: str = ""
    changed_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    # 最後に人間が発言した時刻。まだ無ければ 0
    last_prompt: float = 0.0
    # 何を求められているか。WAITING / ERROR のあいだだけ入る
    detail: Detail = field(default_factory=lambda: NO_DETAIL)

    @property
    def key(self) -> tuple[str, str]:
        """セッションを一意に決める組。ID だけでは別のマシンのものと重なりうる。"""
        return self.host, self.session_id

    @property
    def name(self) -> str:
        """ログと計測に出す名前。リモートならホスト名を前に付ける。"""
        return f"{self.host}:{self.label}" if self.host else self.label

    def age(self) -> int:
        """現在の状態になってからの経過秒。"""
        return int(time.monotonic() - self.changed_at)

    def set_state(self, state: SessionState) -> bool:
        """状態を更新する。実際に変化したら True。"""
        self.last_seen = time.monotonic()
        if self.state is state:
            return False
        self.state = state
        self.changed_at = time.monotonic()
        return True


class Registry:
    """セッション群の状態を保持し、STATE レポートを組み立てる。

    イベントは並行して飛んでくるのでスレッドセーフにしてある。
    """

    def __init__(self, on_change=None, metrics=None, display_slots: int = MAX_SLOTS) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str], Session] = {}
        self._auto_accept = False
        self._target_slot = NO_SLOT
        self._on_change = on_change
        self._metrics = metrics
        # 物理的に存在する LED の数。これより多いセッションは表示に出せない。
        self._display_slots = max(1, min(int(display_slots), MAX_SLOTS))

    @property
    def aggregated(self) -> bool:
        """表示を 1 スロットに集約しているか。

        LED が 1 個だけの構成では、どのセッションかを示せないため
        P2（セッションの同定）は評価できない。P1 の検証に限られる。
        """
        return self._display_slots == 1

    # ------------------------------------------------------------------ 入力

    def handle_event(self, event: AgentEvent) -> None:
        """イベントを 1 件処理する。"""
        key = (event.host, event.session)

        with self._lock:
            if event.kind is EventKind.END:
                removed = self._sessions.pop(key, None)
                if removed:
                    log.info("スロット %d を解放 (%s)", removed.slot, removed.name)
                    # 入力待ちのまま終了した場合も計測上は「解消」として閉じる
                    self._track_waiting(removed, removed.state, SessionState.EMPTY)
                    self._notify()
                return

            if event.kind is EventKind.ALIVE:
                session = self._sessions.get(key)
                if session:
                    session.last_seen = time.monotonic()
                return

            session = self._get_or_create(event)
            if session is None:
                return

            if event.prompt:
                session.last_prompt = time.monotonic()

            before = session.state
            changed = session.set_state(event.state)
            if self._update_detail(session, event.request, changed):
                self._notify()
            if changed:
                log.info("S%d %s -> %s", session.slot + 1, session.name, event.state.name)
                self._track_waiting(session, before, event.state)
                self._notify()

    def _get_or_create(self, event: AgentEvent) -> Session | None:
        """既存セッションを返す。無ければ空きスロットに割り当てる。"""
        session = self._sessions.get((event.host, event.session))
        if session:
            return session

        slot = self._free_slot()
        if slot is None:
            log.warning("空きスロットがありません。セッション %s を追跡しません", event.session[:8])
            return None

        session = Session(
            session_id=event.session,
            slot=slot,
            label=_label(event.session, event.cwd),
            host=event.host,
            agent=event.agent,
            cwd=event.cwd,
        )
        self._sessions[session.key] = session
        log.info("スロット %d に割り当て: %s", slot, session.name)
        return session

    @staticmethod
    def _update_detail(session: Session, request: Request | None, state_changed: bool) -> bool:
        """detail を更新する。表示が変わるなら True。

        許可要求の直後には、同じ件について文面だけのイベントも届くことがある
        （Claude Code の Notification）。そちらは「許可が必要です」としか言わないので、
        具体的な説明を上書きさせない。
        """
        before = session.detail
        specific = request is not None and request.kind is not RequestKind.MESSAGE
        if session.state not in DETAIL_STATES:
            session.detail = NO_DETAIL
        elif state_changed or specific or session.detail == NO_DETAIL:
            session.detail = _detail_from_request(request)
        return session.detail != before

    def _free_slot(self) -> int | None:
        """未使用のスロット番号。全部埋まっていたら、最も古い IDLE/DONE を追い出す。"""
        used = {s.slot for s in self._sessions.values()}
        for slot in range(MAX_SLOTS):
            if slot not in used:
                return slot

        # 全埋まり: 対応不要な状態のものだけを追い出す。
        # WAITING や ERROR を追い出すと「気づけない」という本来の目的を損なう。
        evictable = [
            s for s in self._sessions.values()
            if s.state in (SessionState.IDLE, SessionState.DONE)
        ]
        if not evictable:
            return None

        victim = min(evictable, key=lambda s: s.last_seen)
        del self._sessions[victim.key]
        log.info("スロット %d を再利用のため解放 (%s)", victim.slot, victim.name)
        return victim.slot

    # ------------------------------------------------------------- パッド操作

    def toggle_auto_accept(self) -> bool:
        with self._lock:
            self._auto_accept = not self._auto_accept
            self._notify()
            return self._auto_accept

    def set_target(self, slot: int) -> Session | None:
        """宛先セッションを切り替える。存在しないスロットなら何もしない。"""
        with self._lock:
            session = self.session_for_slot(slot)
            if session is None:
                return None
            self._target_slot = slot
            self._notify()
            return session

    def recent_session(self) -> Session | None:
        """直近に人間が発言したセッション。発言が無ければ直近に動きのあったもの。"""
        with self._lock:
            if not self._sessions:
                return None
            return max(self._sessions.values(), key=lambda s: (s.last_prompt, s.last_seen))

    def assign(
        self,
        slot: int,
        session_id: str | None = None,
        cwd: str | None = None,
        host: str | None = None,
    ) -> Session:
        """セッションを指定のスロットへ移す。先客がいれば入れ替える。

        セッションは session_id、無ければ cwd、どちらも無ければ直近に使ったもので選ぶ。
        host を省くとマシンを問わずに探す。
        session_id が未登録なら、その場で登録する（イベントがまだ届いていない場合）。
        """
        if not 0 <= slot < MAX_SLOTS:
            raise SlotError(f"スロットは 1〜{MAX_SLOTS} で指定してください")

        with self._lock:
            mover = self._find(session_id, cwd, host)
            origin = mover.slot if mover else None

            if mover is None:
                if not session_id:
                    raise SlotError("対象のセッションが見つかりません")
                mover = Session(
                    session_id=session_id,
                    slot=slot,
                    label=_label(session_id, cwd or ""),
                    host=host or "",
                    cwd=cwd or "",
                )

            if origin == slot:
                return mover

            occupant = self.session_for_slot(slot)
            if origin is None and occupant is not None:
                # 新顔なので入れ替え先が無い。先客には空きスロットへ移ってもらう。
                origin = self._free_slot()
                if origin is None:
                    raise SlotError(
                        f"スロットが埋まっており、S{slot + 1} の {occupant.name} を動かせません"
                    )
                if occupant.key not in self._sessions:
                    # 空きを作るために先客自身が追い出された。そのまま入る。
                    occupant, origin = None, None

            if occupant is not None:
                occupant.slot = origin
            mover.slot = slot
            self._sessions[mover.key] = mover

            if origin is not None:
                if self._metrics is not None:
                    self._metrics.slots_swapped(origin, slot)
                # 宛先はスロットではなくセッションに付いていく
                if self._target_slot == origin:
                    self._target_slot = slot
                elif self._target_slot == slot:
                    self._target_slot = origin if occupant is not None else NO_SLOT

            if occupant is not None:
                log.info(
                    "S%d (%s) と S%d (%s) を入れ替えました",
                    slot + 1, mover.name, origin + 1, occupant.name,
                )
            else:
                log.info("%s を S%d に割り当てました", mover.name, slot + 1)
            self._notify()
            return mover

    def release(self, slot: int) -> Session:
        """スロットを空ける。セッション自体には何もしない。

        そのセッションから次のイベントが届けば、空いているスロットに入り直す。
        """
        if not 0 <= slot < MAX_SLOTS:
            raise SlotError(f"スロットは 1〜{MAX_SLOTS} で指定してください")

        with self._lock:
            session = self.session_for_slot(slot)
            if session is None:
                raise SlotError(f"S{slot + 1} は空いています")
            del self._sessions[session.key]
            if self._target_slot == slot:
                self._target_slot = NO_SLOT
            log.info("スロット %d を解放 (%s)", slot, session.name)
            self._track_waiting(session, session.state, SessionState.EMPTY)
            self._notify()
            return session

    def _find(
        self, session_id: str | None, cwd: str | None, host: str | None
    ) -> Session | None:
        """host が None ならマシンを問わない。複数当てはまれば直近に動きのあったもの。"""
        if not session_id and not cwd:
            return self.recent_session()
        matching = [
            s
            for s in self._sessions.values()
            if (s.session_id == session_id if session_id else s.cwd == cwd)
            and (host is None or s.host == host)
        ]
        return max(matching, key=lambda s: s.last_seen) if matching else None

    def session_for_key(self, slot: int) -> Session | None:
        """キーが指しているセッション。

        LED 1 個の構成ではキーとセッションが対応しないので、
        最も古くから入力待ちのものを指しているとみなす（計測と同じ解釈）。
        """
        with self._lock:
            if self.aggregated:
                waiting = [
                    s for s in self._sessions.values() if s.state is SessionState.WAITING
                ]
                if waiting:
                    return min(waiting, key=lambda s: s.changed_at)
            return self.session_for_slot(slot)

    def session_for_slot(self, slot: int) -> Session | None:
        with self._lock:
            for session in self._sessions.values():
                if session.slot == slot:
                    return session
            return None

    def target_session(self) -> Session | None:
        with self._lock:
            if self._target_slot == NO_SLOT:
                return None
            return self.session_for_slot(self._target_slot)

    # ------------------------------------------------------------------ 出力

    def tick(self) -> bool:
        """時間経過による状態遷移。変化があれば True。

        DONE を一定時間後に IDLE へ落とす。定期的に呼ぶこと。
        """
        with self._lock:
            changed = False
            for session in self._sessions.values():
                if (
                    session.state is SessionState.DONE
                    and time.monotonic() - session.changed_at > DONE_DECAY_SEC
                ):
                    session.set_state(SessionState.IDLE)
                    changed = True
            if changed:
                self._notify()
            return changed

    def build_report(self) -> bytes:
        """現在の状態から STATE レポートを作る。

        display_slots が 1 の場合は、全セッションの中で最も優先度の高い状態を
        スロット 0 に集約する。LED が 1 個しかなくても
        「対応が必要かどうか」は伝わる。
        """
        with self._lock:
            states = [SessionState.EMPTY] * MAX_SLOTS
            ages = [0] * MAX_SLOTS

            if self.aggregated:
                states[0], ages[0] = self._aggregate()
            else:
                for session in self._sessions.values():
                    if session.slot < self._display_slots:
                        states[session.slot] = session.state
                        ages[session.slot] = session.age()

            flags = Flag.DAEMON_ALIVE
            if self._auto_accept:
                flags |= Flag.AUTO_ACCEPT
            if any(s is SessionState.WAITING for s in states):
                flags |= Flag.ANY_WAITING

            return build_state_report(states, ages, int(flags), self._target_slot)

    def snapshot(self, with_detail: bool = False) -> list[dict]:
        """スロットごとの内容。空きスロットも含め、必ず MAX_SLOTS 個返す。

        detail にはコマンドの中身が入りうるので、求められたときだけ含める。
        """
        with self._lock:
            by_slot = {s.slot: s for s in self._sessions.values()}
            rows = []
            for slot in range(MAX_SLOTS):
                session = by_slot.get(slot)
                rows.append(
                    {
                        "slot": slot + 1,
                        "state": session.state.name if session else SessionState.EMPTY.name,
                        "label": session.label if session else "",
                        "host": session.host if session else "",
                        "agent": session.agent if session else "",
                        "session_id": session.session_id if session else "",
                        "cwd": session.cwd if session else "",
                        "age_sec": session.age() if session else 0,
                        "target": session is not None and slot == self._target_slot,
                    }
                )
                if with_detail:
                    detail = session.detail if session else NO_DETAIL
                    rows[-1].update(
                        summary=detail.summary, detail=detail.text, danger=detail.danger
                    )
            return rows

    def describe(self) -> str:
        """ログ・デバッグ用の 1 行表現。"""
        with self._lock:
            if not self._sessions:
                return "(セッションなし)"
            parts = []
            for session in sorted(self._sessions.values(), key=lambda s: s.slot):
                parts.append(f"S{session.slot + 1}:{session.state.name}:{session.name}")
            line = "  ".join(parts)
            if self.aggregated:
                agg, _ = self._aggregate()
                line += f"   [LED={agg.name}]"
            return line

    def _aggregate(self) -> tuple[SessionState, int]:
        """全セッションから、最も対応が必要な状態を 1 つ選ぶ。"""
        if not self._sessions:
            return SessionState.EMPTY, 0

        for state in AGGREGATE_PRIORITY:
            matching = [s for s in self._sessions.values() if s.state is state]
            if matching:
                # 同じ状態が複数あれば、最も古いものの経過時間を使う
                oldest = max(matching, key=lambda s: s.age())
                return state, oldest.age()

        return SessionState.EMPTY, 0

    def _track_waiting(
        self, session: Session, before: SessionState, after: SessionState
    ) -> None:
        """WAITING への出入りを計測に渡す。

        P1・P2 の指標はすべて「入力待ちがいつ始まり、いつ気づかれ、いつ終わったか」
        から導かれる（docs/p1p2-poc-plan.md §1）。
        """
        if self._metrics is None or before is after:
            return
        if after is SessionState.WAITING:
            self._metrics.waiting_started(session.slot, session.label, session.host)
        elif before is SessionState.WAITING:
            self._metrics.waiting_ended(session.slot, session.label, session.host)

    def _notify(self) -> None:
        if self._on_change:
            self._on_change()


def _label(session_id: str, cwd: str) -> str:
    """セッションの短い表示名。

    ID は人間には読めないため、作業フォルダ名を使う。無ければ ID の先頭。
    """
    name = PATH_SEPARATORS.split(cwd.rstrip("/\\"))[-1]
    return name[:LABEL_MAX_CHARS] or session_id[:8] or "unknown"


def _detail_from_request(request: Request | None) -> Detail:
    """何を求められているかを、表示用にまとめる。分からなければ NO_DETAIL。"""
    if request is None:
        return NO_DETAIL

    if request.kind is RequestKind.MESSAGE:
        return Detail(text=_one_line(request.text))

    if request.kind is RequestKind.TOOL:
        text = f"{request.tool}: {request.text}" if request.text else request.tool
        return Detail(text=_one_line(text))

    return Detail(
        # エージェント自身が付けた短い説明。コマンドの代わりではなく、添えて見せる。
        summary=_one_line(request.summary),
        text=_one_line(f"$ {request.text}" if request.text else request.tool),
        # 上限で切る前の全文を調べる。危険な部分が末尾にあっても見落とさないため
        danger=any(pattern.search(request.text) for pattern in DANGER_PATTERNS),
    )


def _one_line(text: str) -> str:
    """空白をまとめて 1 行にし、上限で切る。"""
    text = " ".join(text.split())
    if len(text) > DETAIL_MAX_CHARS:
        text = text[: DETAIL_MAX_CHARS - len(DETAIL_TRUNCATION_MARK)] + DETAIL_TRUNCATION_MARK
    return text
