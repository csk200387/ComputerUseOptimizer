"""상태 루트 · 세션 namespace · 스냅샷 refmap 저장 (OS 무관).

배치 (CUOP_HOME, 기본 ~/.cuop):
  global/ 또는 sessions/<id>/   — namespace. 다른 namespace의 스냅샷은 보이지 않는다
    ids.json                    — {"next": N, "snap": M, "tokens": {토큰 hex: N}} 요소 → 안정 번호 eN (D3)
    latest.json                 — 마지막 스냅샷 id
    snapshots/<sid>.json        — Snapshot (refmap)
    views/<key>.json            — 에이전트가 이미 받은 뷰와 키별 revision(텍스트 형식 Diff 기준)
    session.json                — 세션 매니페스트 (sessions/ 아래만)
여러 CLI 프로세스가 같은 namespace를 쓰므로 쓰기는 namespace의 lock 파일(fcntl.flock)로 직렬화한다.
"""

import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from cuop.desktop.errors import CuopError
from cuop.schema import Bounds

KEEP_SNAPSHOTS = 64  # ponytail: namespace마다 최근 64개만 보관 — 오래된 ref는 SNAPSHOT_NOT_FOUND
REF = re.compile(r"^@(s[0-9a-z]+):(e[0-9]+)$")
SALT = (
    "abcdefghijkmnpqrstuvwxyz23456789"  # namespace마다 다른 스냅샷 id 접두어 — 다른 세션의 ref가 엉뚱하게 풀리지 않게
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefEntry(_Model):
    """ref 하나를 나중에 되찾는 데 필요한 증거. 값 · bounds는 정체성이 아니다(표시용)."""

    token: str | None = None  # AX 원격 토큰 hex — 같은 요소를 다른 프로세스에서 복원
    pid: int
    proc_start: int | None = None  # 프로세스 세대(시작 시각 ms) — pid 재사용 감지
    role: str
    raw_role: str
    name: str | None = None
    native_id: str | None = None
    window_id: int | None = None  # CGWindowID, 앱 소유 surface(menubar 등)는 None
    surface: str = "window"
    bounds: Bounds | None = None
    actions: list[str] = []
    unique: bool = True  # capture proof: 창 전체를 다 읽었고 role + 안정 필드가 유일했는가 (토큰 없는 ref용)


class Snapshot(_Model):
    snapshot_id: str
    created_at: float
    app: str
    pid: int
    bundle_id: str | None = None
    window_id: int | None = None
    window_title: str | None = None
    surface: str = "window"
    complete: bool = True
    refs: dict[str, RefEntry] = {}
    drills: dict[str, list[str]] = {}  # --root drill마다 그때 실은 ref — 같은 root를 다시 drill하면 사라진 것을 뺀다


def state_root() -> Path:
    raw = os.environ.get("CUOP_HOME")
    if raw is not None and not Path(raw).is_absolute():
        raise CuopError("INVALID_ARGS", f"CUOP_HOME must be an absolute path, got {raw!r}")
    return Path(raw) if raw else Path.home() / ".cuop"


def parse_ref(text: str) -> tuple[str, str]:
    """`@s7:e42` → ("s7", "e42"). bare `@e42`는 어느 스냅샷인지 몰라 거부한다(agent-desktop과 같음)."""
    if m := REF.match(text.strip()):
        return m.group(1), m.group(2)
    raise CuopError(
        "INVALID_ARGS",
        f"{text!r} is not a qualified ref; use the @<snapshot_id>:eN form a snapshot printed",
        details={"kind": "unqualified_ref" if re.match(r"^@?e[0-9]+$", text.strip()) else "malformed_ref"},
    )


class Namespace:
    """세션 하나(또는 global)의 디스크 상태."""

    def __init__(self, session: str | None = None) -> None:
        root = state_root()
        if session is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
            raise CuopError("INVALID_ARGS", f"invalid session id {session!r}")
        self.session = session
        # 대소문자를 구분하지 않는 볼륨에서 'A'와 'a' 세션이 한 폴더가 되지 않게 대문자가 있으면 해시를 붙인다
        key = session if session is None or session == session.lower() else f"{session.lower()}-{_h(session)}"
        self.dir = root / "sessions" / key if key else root / "global"
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # --- 잠금 · JSON ---
    @contextmanager
    def lock(self) -> Generator[None]:
        with (self.dir / ".lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self, name: str) -> Any:
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _write(self, name: str, data: Any) -> None:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )  # 남이 미리 만든 링크를 따라가지 않게
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        tmp.replace(path)  # 원자적 교체 — 읽는 쪽이 반쯤 쓴 파일을 보지 않게

    # --- 매니페스트 ---
    @property
    def manifest(self) -> dict[str, Any] | None:
        return self._read("session.json") if self.session else None

    def check_open(self) -> None:
        if (m := self.manifest) is not None and m.get("ended_at"):
            raise CuopError("INVALID_ARGS", f"session {self.session} has ended", details={"kind": "session_ended"})

    # --- 안정 번호 · 스냅샷 id ---
    def allocate(self, tokens: list[str | None]) -> tuple[str, list[str]]:
        """새 스냅샷 id와 토큰별 안정 번호(eN). 토큰 없는 요소는 매번 새 번호(다음에 같은 요소라 증명할 수 없다)."""
        with self.lock():
            ids: dict[str, Any] = self._read("ids.json") or {"next": 1, "snap": 1, "tokens": {}}
            salt = ids.setdefault("salt", "".join(secrets.choice(SALT) for _ in range(4)))  # 세션끼리 id가 겹치지 않게
            known: dict[str, int] = ids["tokens"]
            out: list[str] = []
            for tok in tokens:
                if tok is not None and tok in known:
                    out.append(f"e{known[tok]}")
                    continue
                n = ids["next"]
                ids["next"] = n + 1
                if tok is not None:
                    known[tok] = n
                out.append(f"e{n}")
            sid = f"s{salt}{ids['snap']}"
            ids["snap"] += 1
            self._write("ids.json", ids)
        return sid, out

    def save(self, snap: Snapshot, merge: bool = False, drill: str | None = None) -> Snapshot:
        """저장 + latest 갱신. merge(--root drill)는 잠금 안에서 최신 refmap을 다시 읽어 합친다(동시 drill 보호).
        같은 root를 다시 drill하면 지난 drill에만 있던 하위 ref는 지운다 — 사라진 요소가 주소로 남지 않게(감사 #7)."""
        with self.lock():
            if merge:
                cur = self._read(f"snapshots/{snap.snapshot_id}.json")
                if cur is None:
                    raise CuopError("SNAPSHOT_NOT_FOUND", f"snapshot {snap.snapshot_id} expired during the drill")
                base = Snapshot.model_validate(cur)
                refs = dict(base.refs)
                drills = dict(base.drills)
                if drill is not None:
                    for eid in drills.get(drill, []):
                        if eid != drill and eid not in snap.refs:
                            refs.pop(eid, None)
                    drills[drill] = sorted(snap.refs)
                snap = base.model_copy(update={"refs": {**refs, **snap.refs}, "drills": drills})
            self._write(f"snapshots/{snap.snapshot_id}.json", snap.model_dump(mode="json", exclude_defaults=True))
            self._write("latest.json", {"snapshot_id": snap.snapshot_id, "ref_count": len(snap.refs)})
            old = sorted((self.dir / "snapshots").glob("s*.json"), key=lambda p: p.stat().st_mtime)
            for p in old[:-KEEP_SNAPSHOTS]:
                if p.stem != snap.snapshot_id:
                    p.unlink(missing_ok=True)
        return snap

    def load(self, snapshot_id: str) -> Snapshot:
        data = self._read(f"snapshots/{snapshot_id}.json")
        if data is None:
            where = f"session {self.session}" if self.session else "the global namespace"
            raise CuopError(
                "SNAPSHOT_NOT_FOUND",
                f"snapshot {snapshot_id} not found in {where}",
                details={"snapshot_id": snapshot_id, "session": self.session},
            )
        return Snapshot.model_validate(data)

    def latest(self) -> dict[str, Any] | None:
        return self._read("latest.json")

    def resolve(self, ref: str) -> tuple[Snapshot, str, RefEntry]:
        sid, eid = parse_ref(ref)
        snap = self.load(sid)
        if (entry := snap.refs.get(eid)) is None:
            raise CuopError(
                "STALE_REF", f"{ref} is not in snapshot {sid}'s RefMap", details={"kind": "saved_ref_absent"}
            )
        return snap, eid, entry

    # --- 텍스트 형식 Diff 기준 뷰 ---
    def view(self, key: str) -> tuple[dict[str, Any] | None, int | None]:
        data: dict[str, Any] | None = self._read(f"views/{_safe(key)}.json")
        return data, data.get("revision", 0) if data is not None else None  # 이전 버전이 저장한 뷰는 revision 0

    def save_view(self, key: str, data: dict[str, Any], *, expected_revision: int | None) -> bool:
        """Commit the rendered view only if its baseline is still current."""
        with self.lock():
            _, revision = self.view(key)
            if revision != expected_revision:
                return False
            self._write(f"views/{_safe(key)}.json", {**data, "revision": (expected_revision or 0) + 1})
            return True


def _h(s: str) -> str:
    import hashlib

    return hashlib.sha1(s.encode()).hexdigest()[:8]


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]


# --- 세션 관리 (session start · end · list · gc) ---
def new_session_id() -> str:
    return "ses-" + secrets.token_hex(4)


def start_session(name: str | None, trace: bool, artifacts: str = "events", overlay: bool = False) -> dict[str, Any]:
    if name is not None and (not 1 <= len(name.encode()) <= 128 or any(ord(c) < 32 for c in name)):
        raise CuopError("INVALID_ARGS", "--name must be 1-128 bytes without control characters")
    sid = new_session_id()
    ns = Namespace(sid)
    manifest: dict[str, Any] = {
        "id": sid,
        "name": name,
        "created_at": time.time(),
        "ended_at": None,
        "trace": "on" if trace else "off",
        "artifacts": artifacts,
        "overlay": overlay,
    }
    (ns.dir / "trace").mkdir(exist_ok=True, mode=0o700)
    ns._write("session.json", manifest)  # pyright: ignore[reportPrivateUsage]
    return manifest


def end_session(sid: str) -> dict[str, Any]:
    ns = Namespace(sid)
    with ns.lock():  # 저장 · gc와 같은 잠금
        if (m := ns.manifest) is None:
            raise CuopError("INVALID_ARGS", f"no session {sid}", details={"kind": "session_absent"})
        if not m.get("ended_at"):
            m["ended_at"] = time.time()
            ns._write("session.json", m)  # pyright: ignore[reportPrivateUsage]
    return m


def list_sessions() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in sorted((state_root() / "sessions").glob("*/session.json")):
        try:
            out.append(json.loads(d.read_text(encoding="utf-8")))
        except OSError, ValueError:
            continue
    return out


LIVENESS = 300.0  # agent-desktop TRACE_LIVENESS_WINDOW — 5분 안에 쓴 세션은 쓰는 중으로 본다


def gc_sessions(ended_only: bool, older_than: float | None) -> list[str]:
    """agent-desktop gc 규칙: 기본은 끝난 세션만. --older-than이면 그보다 오래된(끝난 시각, 없으면 만든 시각) 것도.
    쓰는 중인 세션(잠금을 쥔 프로세스 · 5분 안의 기록)은 지우지 않는다 — 끝났는데 --ended면 기록 시각은 보지 않는다."""
    import shutil

    now, removed = time.time(), list[str]()
    for m in list_sessions():
        ended = bool(m.get("ended_at"))
        if ended_only and not ended:
            continue
        if older_than is not None:
            if now - (m.get("ended_at") or m["created_at"]) < older_than:
                continue
        elif not ended:
            continue
        d = state_root() / "sessions" / m["id"]
        if d.is_symlink() or not d.is_dir():
            continue
        with (d / ".lock").open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 지금 쓰는 프로세스가 있으면 건너뛴다
            except BlockingIOError:
                continue
            recent = now - max((p.stat().st_mtime for p in d.rglob("*") if p.is_file() and p.name != ".lock"),
                               default=m["created_at"]) < LIVENESS  # fmt: skip
            if recent and not (ended_only and ended):
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(m["id"])
    return removed
