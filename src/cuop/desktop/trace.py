"""세션 trace — 명령마다 JSONL 한 줄(사람이 에이전트가 한 일을 단계별로 본다). 민감 필드는 기록하지 않는다.

배치: `<session>/trace/<pid>-<시작 ms>.jsonl`(프로세스마다 한 파일, 첫 줄 `trace.meta`). `--trace PATH`면 그 파일 하나.
기록: 세션 매니페스트 `trace: on`(끝나지 않음) 또는 `--trace`. 이름 · 값 · 글자 · 제목 · URL은 `{"redacted": true}`.
"""

import json
import os
import re
import time
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, cast

from cuop.desktop.errors import CuopError
from cuop.desktop.store import REF, Namespace

SCHEMA = 1
# 인자는 허용 목록만 그대로 — 나머지(글자 · 값 · 이름 · 제목 · env · arg · 지문 …)는 모두 가린다(Codex M5 #1)
SAFE = frozenset({"ref", "from_", "to", "app", "window_id", "surface", "root", "max_depth", "interactive_only",
                  "compact", "skeleton", "include_bounds", "timeout_ms", "diff", "direction", "amount", "property",
                  "predicate", "action", "count", "first", "last", "nth", "limit", "role", "exact", "button",
                  "interval_ms", "duration", "drop_delay", "force", "headed", "format", "index", "screen", "timeout",
                  "event", "menu", "menu_closed", "no_attach", "activate", "width", "height", "x", "y", "dx",
                  "dy"})  # fmt: skip
# 허용 키라도 정해진 모양의 값만 그대로 — ref 자리의 글자 · 알림 동작 이름(사용자 문구)은 가린다
_SHAPE = {k: REF for k in ("ref", "from_", "to", "root")} | {"window_id": re.compile(r"^w-[0-9]+$")}
_ACTIONS = frozenset({"click", "double-click", "triple-click", "right-click", "type", "set-value", "clear", "focus",
                      "select", "toggle", "check", "uncheck", "expand", "collapse", "scroll", "scroll-to", "hover",
                      "drag"})  # fmt: skip
MAX_EVENTS = 100_000
_START_MS = int(time.time() * 1000)


def safe_ref(v: Any) -> str | None:
    """trace · HUD에 남겨도 되는 ref(올바른 모양)만. 그 밖은 None."""
    return v if isinstance(v, str) and REF.match(v) else None


def redact(v: Any, key: str = "") -> Any:
    if v in (None, False, "", [], {}):
        return v
    if key and (
        key not in SAFE
        or (key in _SHAPE and not (isinstance(v, str) and _SHAPE[key].match(v)))
        or (key == "action" and v not in _ACTIONS)
    ):
        return {"redacted": True}
    if isinstance(v, dict):
        d: dict[str, Any] = dict(v)  # pyright: ignore[reportUnknownArgumentType]
        return {k: redact(x, k) for k, x in d.items()}
    if isinstance(v, list):
        items: list[Any] = list(v)  # pyright: ignore[reportUnknownArgumentType]
        return [redact(x) for x in items]
    return v


def sink(ns: Namespace, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    m = ns.manifest
    if m is None or m.get("trace") != "on" or m.get("ended_at"):
        return None
    return ns.dir / "trace" / f"{os.getpid()}-{_START_MS}.jsonl"


def next_step(ns: Namespace) -> int:
    with ns.lock():
        path = ns.dir / "trace" / ".step"
        n = int(path.read_text() or 0) + 1 if path.exists() else 1
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(str(n))
    return n


def record(path: Path, event: dict[str, Any], session: str | None) -> None:
    """한 줄씩 원자적으로 덧붙인다(O_APPEND). 새 파일이면 meta 줄부터."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines: list[str] = []
    if not path.exists() or path.stat().st_size == 0:  # --trace-strict 사전 검사가 만든 빈 파일에도 meta부터
        meta = {"type": "trace.meta", "schema": SCHEMA, "version": pkg_version("cuop"), "os": "macos",
                "pid": os.getpid(), "proc_start_ms": _START_MS, "session_id": session, "ts_ms": _START_MS}  # fmt: skip
        lines.append(json.dumps(meta, ensure_ascii=False))
    lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def event(name: str, args: dict[str, Any], env: dict[str, Any], started: float, step: int) -> dict[str, Any]:
    raw: object = env.get("data")
    data = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    err: dict[str, Any] = env.get("error") or {}
    disp: dict[str, Any] = data.get("disposition") or err.get("disposition") or {}
    ev: dict[str, Any] = {
        "type": "command",
        "ts_ms": int(started * 1000),
        "duration_ms": round((time.time() - started) * 1000),
        "pid": os.getpid(),
        "step": step,
        "command": name,
        "args": redact({k: v for k, v in args.items() if k != "app" or env.get("ok")}),  # 못 찾은 앱 이름은 빼고
        "ok": bool(env.get("ok")),
        "code": err.get("code"),
        "delivery": disp.get("delivery"),
        "retry": disp.get("retry"),
    }
    for k in ("app", "snapshot_id", "ref_count"):
        if k in data:
            ev[k] = data[k]
    if win := data.get("window"):
        ev["window"] = {"id": win.get("id")}
    return ev


def _clean(ev: dict[str, Any]) -> dict[str, Any]:
    """읽을 때도 다시 가린다 — 예전 판이 쓴 줄 · 손으로 고친 파일이 show · export로 새지 않게."""
    if "args" in ev:
        ev["args"] = redact(ev["args"])
    tgt: Any = ev.get("target")
    if isinstance(tgt, dict):
        t: dict[str, Any] = dict(tgt)  # pyright: ignore[reportUnknownArgumentType]
        ev["target"] = {k: v for k, v in t.items() if k in ("role", "bounds")} | (
            {"ref": t["ref"]} if safe_ref(t.get("ref")) else {}
        )
    return ev


def read(ns: Namespace, limit: int = 500, prefix: str | None = None) -> tuple[list[dict[str, Any]], int]:
    """세그먼트를 모두 합쳐 (ts_ms, pid, 파일 안 순서)로 정렬. 망가진 줄은 세고 건너뛴다."""
    rows: list[tuple[int, int, int, dict[str, Any]]] = []
    bad = 0
    for seg in sorted((ns.dir / "trace").glob("*.jsonl")) if (ns.dir / "trace").is_dir() else []:
        if seg.stat().st_size > 32 << 20:
            raise CuopError("INVALID_ARGS", f"trace segment {seg.name} exceeds 32 MiB")
        for i, line in enumerate(seg.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                e = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(e, dict):
                ev: dict[str, Any] = _clean(dict(e))  # pyright: ignore[reportUnknownArgumentType]
                rows.append((int(ev.get("ts_ms") or 0), int(ev.get("pid") or 0), i, ev))
    events = [e for *_, e in sorted(rows, key=lambda r: r[:3])]
    if prefix:
        events = [e for e in events if str(e.get("command") or e.get("type", "")).startswith(prefix)]
    events = events[-MAX_EVENTS:]  # 가장 최근 것을 남긴다
    return (events[-limit:] if limit else events), bad
