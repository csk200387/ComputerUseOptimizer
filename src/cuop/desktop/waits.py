"""`wait` 명령 · 전역 `--wait-for/--wait-for-gone` · `batch` (agent-desktop commands-system.md)."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cuop.desktop.commands import Ctx, _role_query, ax, resolve_ref, snapshot  # pyright: ignore[reportPrivateUsage]
from cuop.desktop.errors import CuopError
from cuop.schema import Role, State, UIElementNode

POLL = 0.2
EVENTS = ("window-opened", "window-closed", "app-launched", "app-terminated", "focus-changed", "surface-appeared",
          "surface-dismissed")  # fmt: skip


@dataclass
class Selector:
    raw: str
    roles: set[Role] | None
    text: str | None

    @classmethod
    def parse(cls, raw: str) -> Selector:
        role, sep, text = raw.partition(":")
        roles = _role_query(role) if role.strip() else None
        t = text.strip() if sep else None
        if roles is None and not t:
            raise CuopError("INVALID_ARGS", f"empty selector {raw!r}; use role:text, role, or :text")
        return cls(raw, roles, t or None)

    def matches(self, n: UIElementNode) -> bool:
        if self.roles is not None and n.role not in self.roles:
            return False
        if self.text is None:
            return True
        q = self.text.casefold()
        return any(q in x.casefold() for x in (n.name, n.value, n.description) if x)


@dataclass
class Scope:
    """기다리며 다시 읽을 곳: 원래 프로세스의 고정된 창(또는 앱 소유 surface)."""

    pid: int
    window_id: int | None
    surface: str = "window"
    app: str | None = None

    def element(self) -> Any:
        m = ax()
        if self.window_id is not None:
            return m.window_by_id(self.window_id, self.pid).el
        if self.surface == "menubar":
            return m.attr(m.AXUIElementCreateApplication(self.pid), "AXMenuBar")
        return m.AXUIElementCreateApplication(self.pid)


def _poll(check: Callable[[], Any], timeout_ms: int) -> tuple[Any, int]:
    start = time.monotonic()
    deadline = start + timeout_ms / 1000
    while True:
        got = check()
        if got is not None or time.monotonic() >= deadline:
            return got, round((time.monotonic() - start) * 1000)
        time.sleep(POLL)


def selector_wait(
    ctx: Ctx, scope: Scope, selector: str, gone: bool, timeout_ms: int, then_snapshot: bool = True
) -> dict[str, Any]:
    """scope 트리를 다시 읽으며 selector가 나타날 때(gone이면 사라질 때)까지 기다린 뒤 스냅샷을 돌려준다."""
    sel = Selector.parse(selector)
    m = ax()
    last_error: dict[str, str] | None = None

    def check() -> Any:
        nonlocal last_error
        try:
            el = scope.element()
        except CuopError as e:
            if gone and e.code == "WINDOW_NOT_FOUND":
                return {"gone": True, "target_absent": True}
            last_error = {"code": e.code, "message": e.message}
            return None
        if el is None:
            return {"gone": True, "target_absent": True} if gone else None
        tree, walk = m.read_tree(el, max_depth=64, timeout_ms=3000)
        hit = any(sel.matches(n) for n in tree.walk())
        if gone and not hit and walk.complete:
            return {"gone": True}
        if not gone and hit:
            return {}
        return None

    got, elapsed = _poll(check, timeout_ms)
    if got is None:
        details: dict[str, Any] = {"kind": "wait_timeout", "selector": selector, "timeout_ms": timeout_ms}
        if last_error:
            details["last_error"] = last_error
        raise CuopError("TIMEOUT", f"{'gone ' if gone else ''}wait for {selector!r} timed out", details=details)
    if got.get("target_absent") or not then_snapshot:
        return {**got, "matched_selector": selector, "elapsed_ms": elapsed}
    if scope.window_id is not None:
        snap = snapshot(ctx, window_id=f"w-{scope.window_id}", interactive_only=True, compact=True)
    else:
        snap = snapshot(ctx, app=scope.app, interactive_only=True, compact=True)
    return {**snap, **got, "matched_selector": selector, "elapsed_ms": elapsed}


def scope_of(ctx: Ctx, command: str, args: dict[str, Any]) -> Scope:
    """명령이 다룬 곳 — ref 액션은 ref의 원래 창, press는 --app/--window-id, snapshot은 그 대상."""
    m = ax()
    ref = args.get("ref") or (args.get("to") if args.get("wait_for_scope", "to") == "to" else args.get("from_"))
    if ref:
        _, _, e, _ = resolve_ref(ctx.ns, ref)
        app = m.app_by_pid(e.pid)
        return Scope(e.pid, e.window_id, e.surface, app.name if app else None)
    surface = str(args.get("surface") or "window")
    if wid := args.get("window_id"):
        w = m.window_by_id(int(str(wid).removeprefix("w-")))
        return Scope(w.pid, w.id)
    if a := args.get("app"):
        app = m.find_app(a)
        if surface == "menubar":  # 고른 surface를 그대로 다시 읽는다(창으로 바꾸지 않게)
            return Scope(app.pid, None, surface, app.name)
        win = m.pick_window(app.pid, app.name)
        return Scope(app.pid, win.id if win else None, app=app.name)
    pid = m.frontmost_pid()
    if pid is None:
        raise CuopError("APP_NOT_FOUND", "no frontmost app to wait on")
    app = m.app_by_pid(pid)
    win = m.pick_window(pid, app.name if app else str(pid))
    return Scope(pid, win.id if win else None, app=app.name if app else None)


# --- wait 명령 ---
def wait(
    ctx: Ctx,
    ms: int | None = None,
    element: str | None = None,
    predicate: str = "exists",
    value: str | None = None,
    action: str = "click",
    window: str | None = None,
    text: str | None = None,
    count: int | None = None,
    menu: bool = False,
    menu_closed: bool = False,
    notification: bool = False,
    event: str | None = None,
    window_id: str | None = None,
    app: str | None = None,
    timeout: int = 30000,
) -> dict[str, Any]:
    # --notification --text = 그 글자가 든 새 알림(모드 둘이 아니라 필터, 감사 #20)
    modes = [ms is not None, element is not None, window is not None and event is None,
             text is not None and not notification, menu or menu_closed, notification, event is not None]  # fmt: skip
    if sum(modes) != 1:
        raise CuopError("INVALID_ARGS", "give exactly one wait mode: MS, --element, --window, --text, --menu, --event")
    if ms is not None:
        time.sleep(ms / 1000)
        return {"waited_ms": ms}
    m = ax()
    if notification:  # 시작 시점에 보이던 알림을 기준선으로, 새 알림(배너 포함)이 나타날 때까지
        from cuop.collectors import ax_system

        def rows() -> list[tuple[str, str]]:
            q = (text or "").casefold()
            return [(r["app_name"] or "", r["title"] or "") for _, r in ax_system.notifications()
                    if (not app or app.casefold() in (r["app_name"] or "").casefold())
                    and (not q or any(q in (r[k] or "").casefold() for k in ("title", "body")))]  # fmt: skip

        base = rows()

        def note_check() -> Any:
            new = [r for r in rows() if r not in base]
            return {"found": True, "notification": {"app_name": new[0][0]}} if new else None

        got, elapsed = _poll(note_check, timeout)
        if got is None:
            raise CuopError("TIMEOUT", "no new notification", details={"kind": "wait_timeout", "timeout_ms": timeout})
        return {**got, "elapsed_ms": elapsed}
    if element is not None:
        return _wait_element(ctx, element, predicate, value, action, timeout)
    if window is not None and event is None:

        def win_check() -> Any:
            for a in [m.find_app(app)] if app else [x for x in m.running_apps() if x.policy == 0]:
                for w in m.app_windows(a.pid):
                    if w.title and window.casefold() in w.title.casefold():
                        return {"found": True, "window": {"id": w.ref_id, "title": w.title, "app": a.name}}
            return None

        got, elapsed = _poll(win_check, timeout)
        if got is None:
            raise CuopError(
                "TIMEOUT", f"no window titled {window!r}", details={"kind": "wait_timeout", "timeout_ms": timeout}
            )
        return {**got, "elapsed_ms": elapsed}
    if text is not None:
        scope = scope_of(ctx, "wait", {"app": app})

        def text_check() -> Any:
            tree, walk = m.read_tree(scope.element(), max_depth=64, timeout_ms=3000)
            n = sum(1 for x in tree.walk() if any(text.casefold() in (f or "").casefold() for f in (x.name, x.value)))
            if count is None and n:
                return {"found": True, "count": n}
            if count is not None and n == count and walk.complete:
                return {"found": True, "count": n}
            return None

        got, elapsed = _poll(text_check, timeout)
        if got is None:
            raise CuopError(
                "TIMEOUT", f"text {text!r} not seen", details={"kind": "wait_timeout", "timeout_ms": timeout}
            )
        return {**got, "elapsed_ms": elapsed}
    if menu or menu_closed:
        a = m.find_app(app) if app else m.app_by_pid(m.frontmost_pid() or -1)
        if a is None:
            raise CuopError("APP_NOT_FOUND", "no app to watch for menus")
        from cuop.collectors import ax_actions

        def menu_check() -> Any:
            is_open = "menu" in ax_actions.open_surfaces(a.pid, None)
            return {"found": True, "open": is_open} if is_open == menu else None

        got, elapsed = _poll(menu_check, timeout)
        if got is None:
            raise CuopError(
                "TIMEOUT", "menu state did not change", details={"kind": "wait_timeout", "timeout_ms": timeout}
            )
        return {**got, "elapsed_ms": elapsed}
    assert event is not None
    return wait_event(Baseline.take(event, app), window_id, window, timeout)


def _wait_element(ctx: Ctx, ref: str, predicate: str, value: str | None, action: str, timeout: int) -> dict[str, Any]:
    if predicate not in ("exists", "enabled", "visible", "actionable", "value"):
        raise CuopError("INVALID_ARGS", "--predicate must be exists, enabled, visible, actionable or value")
    if predicate == "value" and value is None:
        raise CuopError("INVALID_ARGS", "--predicate value needs --value")
    m = ax()

    def check() -> Any:
        try:
            _, _, _, el = resolve_ref(ctx.ns, ref)
            tree, _ = m.read_tree(el, max_depth=0, timeout_ms=1000)
        except CuopError as e:
            if e.code in ("STALE_REF", "AMBIGUOUS_TARGET"):
                raise
            return None
        b = tree.bounds
        visible = b is None or (b.w > 0 and b.h > 0)
        enabled = State.DISABLED not in tree.states
        ok = {
            "exists": True,
            "enabled": enabled,
            "visible": visible,
            "actionable": visible
            and enabled
            and (
                action not in ("type", "set-value", "clear") or "SetValue" in tree.actions or tree.role is Role.TEXTBOX
            ),
            "value": (tree.value or tree.name or "") == value,
        }[predicate]
        return {"found": True, "predicate": predicate} if ok else None

    got, elapsed = _poll(check, timeout)
    if got is None:
        raise CuopError("TIMEOUT", f"{ref} did not satisfy {predicate}", details={"kind": "wait_timeout",
                        "predicate": predicate, "timeout_ms": timeout})  # fmt: skip
    return {**got, "elapsed_ms": elapsed}


# --- 이벤트 대기: 시작 시점 기준선과 새로 읽은 값을 비교 ---
@dataclass
class Baseline:
    event: str
    app: str | None
    pid: int | None
    windows: set[int]
    pids: set[int]
    focus: tuple[int | None, int | None]
    surfaces: set[str]

    @classmethod
    def take(cls, event: str, app: str | None) -> Baseline:
        if event not in EVENTS:
            raise CuopError("INVALID_ARGS", f"--event must be one of {', '.join(EVENTS)}")
        if event.startswith("surface") and app is None:
            raise CuopError("INVALID_ARGS", f"--event {event} needs --app")
        m = ax()
        pid = None
        if app is not None:
            try:
                pid = m.find_app(app).pid
            except CuopError as e:
                if e.code != "APP_NOT_FOUND" or event == "focus-changed":
                    raise
        return cls(event, app, pid, _windows(pid), {a.pid for a in m.running_apps()}, _focus(), _surfaces(pid))


def _windows(pid: int | None) -> set[int]:
    m = ax()
    return {int(w["kCGWindowNumber"]) for w in m.cg_windows() if pid is None or int(w["kCGWindowOwnerPID"]) == pid}


def _focus() -> tuple[int | None, int | None]:
    m = ax()
    pid = m.frontmost_pid()
    if pid is None:
        return None, None
    fw = m.attr(m.AXUIElementCreateApplication(pid), "AXFocusedWindow")
    return pid, m.window_number(fw) if fw is not None else None


def _surfaces(pid: int | None) -> set[str]:
    if pid is None:
        return set()
    from cuop.collectors import ax_actions

    m = ax()
    win = m.attr(m.AXUIElementCreateApplication(pid), "AXFocusedWindow")
    return set(ax_actions.open_surfaces(pid, win))


def wait_event(base: Baseline, window_id: str | None, window: str | None, timeout: int) -> dict[str, Any]:
    m = ax()
    if base.pid is None and base.app and base.event in ("app-terminated", "window-closed", "surface-dismissed"):
        return {"found": True, "event": base.event, "target_unresolved": True}

    def check() -> Any:
        ev = base.event
        if ev == "app-launched":
            new = [a for a in m.running_apps() if a.pid not in base.pids and (not base.app or a.matches(base.app))]
            return {"found": True, "app": {"name": new[0].name, "pid": new[0].pid}} if new else None
        if ev == "app-terminated":
            alive = {a.pid for a in m.running_apps()}
            gone = base.pid not in alive if base.pid else bool(base.pids - alive)
            return {"found": True} if gone else None
        if ev in ("window-opened", "window-closed"):
            pid = base.pid
            if pid is None and base.app:  # 기다리는 동안 앱이 뜬 경우
                hits = [a for a in m.running_apps() if a.matches(base.app)]
                pid = hits[0].pid if hits else None
                if pid is None:
                    return None
            now = _windows(pid)
            diff = (now - base.windows) if ev == "window-opened" else (base.windows - now)
            if window_id:
                diff &= {int(window_id.removeprefix("w-"))}
            if window and ev == "window-opened":
                diff = {w for w in diff if (x := _title(pid, w)) and window.casefold() in x.casefold()}
            return {"found": True, "window": {"id": f"w-{sorted(diff)[0]}"}} if diff else None
        if ev == "focus-changed":
            now = _focus()
            return (
                {"found": True, "focus": {"pid": now[0], "window_id": f"w-{now[1]}" if now[1] else None}}
                if now != base.focus
                else None
            )
        now_s = _surfaces(base.pid)
        diff_s = (now_s - base.surfaces) if ev == "surface-appeared" else (base.surfaces - now_s)
        return {"found": True, "surfaces": sorted(diff_s)} if diff_s else None

    got, elapsed = _poll(check, timeout)
    if got is None:
        raise CuopError("TIMEOUT", f"no {base.event} event", details={"kind": "wait_timeout", "timeout_ms": timeout,
                        "baseline_counts": {"windows": len(base.windows), "apps": len(base.pids)}})  # fmt: skip
    return {**got, "event": base.event, "elapsed_ms": elapsed}


def _title(pid: int | None, wid: int) -> str | None:
    if pid is None:
        return None
    return next((w.title for w in ax().app_windows(pid) if w.id == wid), None)
