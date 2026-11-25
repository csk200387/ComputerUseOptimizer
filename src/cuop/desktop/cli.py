"""`cuop <command>` — 명령마다 stdout에 JSON envelope 하나 (agent-desktop 2.6 계약).

exit code: 0 성공 · 1 구조화 에러 · 2 인자 파싱 에러(envelope는 출력, INVALID_ARGS).
전역 플래그는 하위 명령 앞뒤 어디에 와도 된다(`cuop --headed click @s1:e2`, `cuop click @s1:e2 --headed`).
"""

import argparse
import functools
import inspect
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any, NoReturn

from cuop.desktop import actions as A
from cuop.desktop import commands as C
from cuop.desktop import system as S
from cuop.desktop import tools as T
from cuop.desktop import waits as W
from cuop.desktop.errors import CuopError, fail, ok

GLOBAL_KEYS = ("session", "headed", "fmt", "goal", "trace", "trace_strict", "verbose", "agent_id", "wait_for")
GLOBAL_KEYS_WAIT = ("wait_for_gone", "wait_timeout")


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CuopError("INVALID_ARGS", f"error: {message}", details={"kind": "parse_error"})


def _globals(p: argparse.ArgumentParser, top: bool, skip: frozenset[str] = frozenset()) -> None:
    d = None if top else argparse.SUPPRESS  # 하위 파서는 위에서 준 값을 덮지 않는다

    def add(*flags: str, **kw: Any) -> None:
        if skip & set(flags):  # 명령 자신의 같은 이름 플래그가 이긴다(clipboard-get --format)
            return
        if kw.get("action") in ("store_true",):
            kw["default"] = False if top else argparse.SUPPRESS
        else:
            kw.setdefault("default", d)
        p.add_argument(*flags, **kw)

    add("--session", help="snapshot namespace (or CUOP_SESSION)")
    add("--headed", action="store_true", help="permit physical input, focus and cursor side effects")
    add("--format", dest="fmt", choices=("json", "text"), help="text = CUOP compact tree (fewer tokens)")
    add("--goal", help="current sub-goal for the text format's task-aware filter")
    add("--trace", help="append trace JSONL to this path")
    add("--trace-strict", action="store_true")
    add("-v", "--verbose", action="store_true")
    add("--agent-id")
    add("-w", "--wait-for", help="poll until a role:text selector appears, then return a snapshot")
    add("--wait-for-gone")
    add("--wait-timeout", type=int)


Handler = Callable[..., dict[str, Any] | list[dict[str, Any]]]
REGISTRY: dict[str, tuple[Handler, list[tuple[tuple[str, ...], dict[str, Any]]]]] = {}


def command(name: str, fn: Handler, *args: tuple[tuple[str, ...], dict[str, Any]]) -> None:
    REGISTRY[name] = (fn, list(args))


def a(*flags: str, **kw: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    return flags, kw


APP = a("--app")
WIN = a("--window-id")
REFARG = a("ref")

command(
    "snapshot", C.snapshot, APP, WIN,
    a("--surface", default="window", choices=C.SURFACES), a("--root"),
    a("--max-depth", type=int, default=10), a("-i", "--interactive-only", action="store_true"),
    a("--compact", action="store_true"), a("--skeleton", action="store_true"),
    a("--include-bounds", action="store_true"), a("--timeout-ms", type=int, default=3000),
    a("--force-electron-a11y", action="store_true"), a("--diff", action="store_true"),
)  # fmt: skip
command(
    "find", C.find, APP, WIN, a("--role"), a("--name"), a("--value"), a("--text"), a("--description"),
    a("--native-id"), a("--exact", action="store_true"), a("--root"), a("--surface", default="window"),
    a("--state", action="append"), a("--timeout-ms", type=int, default=5000), a("--count", action="store_true"),
    a("--first", action="store_true"), a("--last", action="store_true"), a("--nth", type=int),
    a("--limit", type=int),
)  # fmt: skip
command("get", C.get, REFARG, a("--property", default="text"))
command("is", C.is_, REFARG, a("--property", default="visible"))
TMO = a("--timeout-ms", type=int, default=5000)
for _name, _fn in (
    ("click", A.click),
    ("double-click", A.double_click),
    ("triple-click", A.triple_click),
    ("right-click", A.right_click),
    ("clear", A.clear),
    ("focus", A.focus),
    ("toggle", A.toggle),
    ("check", A.check),
    ("uncheck", A.uncheck),
    ("expand", A.expand),
    ("collapse", A.collapse),
    ("scroll-to", A.scroll_to),
):
    command(_name, _fn, REFARG, TMO)
command("type", A.type_, REFARG, a("text", nargs="?"), a("--stdin", action="store_true"),
        a("--interval-ms", type=int), a("--timeout-ms", type=int))  # fmt: skip
command("set-value", A.set_value, REFARG, a("value", nargs="?"), a("--stdin", action="store_true"), TMO)
command("select", A.select, REFARG, a("value"), TMO)
command("scroll", A.scroll, REFARG, a("--direction", default="down"), a("--amount", type=int, default=3), TMO)
command("press", A.press, a("combo"), APP, WIN, a("--force", action="store_true"))
command("hover", A.hover, a("ref", nargs="?"), a("--xy"), a("--duration", type=int), TMO)
command(
    "drag", A.drag, a("--from", dest="from_"), a("--to"), a("--from-xy"), a("--to-xy"),
    a("--duration", type=int, default=300), a("--drop-delay", type=int, default=500), TMO,
    a("--wait-for-scope", choices=("from", "to"), default="to"),
)  # fmt: skip
command("mouse-move", A.mouse_move, a("--xy", required=True))
command("mouse-click", A.mouse_click, a("--xy", required=True), a("--button", default="left"),
        a("--count", type=int, default=1), a("--modifiers"))  # fmt: skip
command("mouse-wheel", A.mouse_wheel, a("--x", type=float, required=True), a("--y", type=float, required=True),
        a("--dx", type=int, default=0), a("--dy", type=int, default=0), a("--modifiers"))  # fmt: skip
for _name in ("key-down", "key-up"):
    command(_name, functools.partial(A.held, name=_name), a("combo"), a("--force", action="store_true"))
for _name in ("mouse-down", "mouse-up"):
    command(_name, functools.partial(A.held, name=_name), a("--xy", required=True),
            a("--button", default="left"), a("--modifiers"))  # fmt: skip
command(
    "wait", W.wait, a("ms", nargs="?", type=int), a("--element"), a("--predicate", default="exists"), a("--value"),
    a("--action", default="click"), a("--window"), a("--text"), a("--count", type=int),
    a("--menu", action="store_true"),
    a("--menu-closed", action="store_true"), a("--notification", action="store_true"), a("--event"), WIN, APP,
    a("--timeout", type=int, default=30000),
)  # fmt: skip
command(
    "launch", S.launch, a("app"), a("--timeout", type=int, default=30000), a("--arg", action="append"),
    a("--env", action="append"), a("--cwd"), a("--no-attach", action="store_true"),
    a("--activate", action="store_true"), a("--cdp", type=int, nargs="?", const=0),
)  # fmt: skip
command("close-app", S.close_app, a("app"), a("--force", action="store_true"))
command("focus-window", S.focus_window, WIN, APP, a("--title"))
command("resize-window", S.resize_window, APP, WIN, a("--width", type=float, required=True),
        a("--height", type=float, required=True))  # fmt: skip
command(
    "move-window", S.move_window, APP, WIN, a("--x", type=float, required=True), a("--y", type=float, required=True)
)
for _name, _fn in (("minimize", S.minimize), ("maximize", S.maximize), ("restore", S.restore)):
    command(_name, _fn, APP, WIN)
command("clipboard-get", S.clipboard_get, a("--format", default="text"), a("--out"))
command("clipboard-set", S.clipboard_set, a("text", nargs="?"), a("--stdin", action="store_true"), a("--image"),
        a("--file-url", action="append"))  # fmt: skip
command("clipboard-clear", S.clipboard_clear)
command("screenshot", S.screenshot, a("path", nargs="?"), APP, WIN, a("--screen", type=int))
command("list-surfaces", S.list_surfaces, a("--app", required=True))
command("open-system-surface", S.open_system_surface, a("--surface", required=True))
command("list-notifications", S.list_notifications, APP, a("--text"), a("--limit", type=int))
command("dismiss-notification", S.dismiss_notification, a("index", type=int), APP, a("--expected-app"),
        a("--expected-title"))  # fmt: skip
command("dismiss-all-notifications", S.dismiss_all_notifications, APP)
command("notification-action", S.notification_action, a("index", type=int), a("action"), a("--expected-app"),
        a("--expected-title"))  # fmt: skip
command("list-apps", C.list_apps, APP, a("--timeout-ms", type=int, default=5000))
command("list-windows", C.list_windows, APP, a("--timeout-ms", type=int, default=5000))
command("list-displays", C.list_displays)
command(
    "session", C.session, a("action", choices=("start", "end", "list", "gc")), a("id", nargs="?"), a("--name"),
    a("--no-trace", action="store_true"), a("--screenshots", action="store_true"), a("--cursor", action="store_true"),
    a("--older-than", type=float), a("--ended", action="store_true"), a("--multi-agent", action="store_true"),
)  # fmt: skip
command("trace", T.trace, a("action", choices=("show", "export")), a("--limit", type=int), a("--event"), a("--out"))
command("cursor-overlay", T.cursor_overlay, a("action", choices=("enable", "disable", "status")))


def _mcp_placeholder(ctx: C.Ctx, toolset: str = "full") -> dict[str, Any]:
    """Serve every command as an MCP tool over stdio (desktop_<command>); global flags apply to every call."""
    del ctx, toolset
    return {}


command("mcp", _mcp_placeholder, a("--toolset", choices=("full", "core"), default="full"))  # dispatch가 가로챈다
command("skills", T.skills, a("action", nargs="?", default="list", choices=("list", "get", "path")),
        a("name", nargs="?"), a("reference", nargs="?"), a("--full", action="store_true"))  # fmt: skip
command("status", C.status)
command("permissions", C.permissions, a("--request", action="store_true"), a("--guide", action="store_true"))
command("version", C.version)


def build() -> Parser:
    top = Parser(prog="cuop", description="Structured-UI computer use for AI agents (agent-desktop compatible)")
    _globals(top, top=True)
    sub = top.add_subparsers(dest="command", parser_class=Parser)
    for name, (_, args) in REGISTRY.items():
        p = sub.add_parser(name)
        _globals(p, top=False, skip=frozenset(f for flags, _ in args for f in flags))
        for flags, kw in args:
            p.add_argument(*flags, **kw)
    return top


def run(argv: list[str]) -> tuple[dict[str, Any], int]:
    """argv → (envelope, exit code). batch · MCP도 이 경로를 쓴다."""
    name = next((x for x in argv if x in REGISTRY), "cuop")
    try:
        ns = vars(build().parse_args(argv))
    except CuopError as e:
        return fail(name, e), 2
    name = ns.pop("command") or "help"
    if name == "help":
        return fail(name, CuopError("INVALID_ARGS", "no command given; run cuop --help")), 2
    g = {k: ns.pop(k, None) for k in GLOBAL_KEYS + GLOBAL_KEYS_WAIT}
    ctx = C.Ctx(
        session=g["session"] or os.environ.get("CUOP_SESSION") or None,
        headed=bool(g["headed"]),
        fmt=g["fmt"] or "json",
        goal=g["goal"],
    )
    return observed(name, ctx, ns, g)


QUIET = frozenset({"version", "skills", "trace", "session", "cursor-overlay", "status", "permissions", "help"})


def observed(name: str, ctx: C.Ctx, args: dict[str, Any], g: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """dispatch + 사람이 보는 진행 표시(HUD 이벤트)와 세션 trace 한 줄. 표시 · 기록 실패는 명령 결과를 바꾸지 않는다."""
    if name in QUIET:
        env, code = dispatch(name, ctx, args, g)
        if name == "status" and env["ok"] and g.get("trace"):
            env["data"]["tracing"] = True  # --trace PATH도 기록 중
        return env, code
    from cuop.desktop import trace
    from cuop.overlay import client as hud

    started = time.time()
    try:
        ns = ctx.ns  # 끝난 세션이면 여기서 거부 — 아무것도 보내지 않는다
    except CuopError as e:
        return fail(name, e), 1
    try:
        path = trace.sink(ns, g.get("trace"))
        show = hud.enabled(ns)
        if path is not None and g.get("trace_strict"):  # 동작 전에 쓸 수 있는지 — 동작한 뒤에 실패를 알게 되지 않게
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    except CuopError, OSError:
        if g.get("trace_strict"):
            return fail(name, CuopError("INTERNAL", "trace sink unavailable (--trace-strict); nothing was sent")), 1
        path, show = None, False
    target: dict[str, Any] = {}
    label = ""
    if ref := args.get("ref") or args.get("from_"):
        try:
            _, _, entry = ns.resolve(str(ref))
            b = entry.bounds
            target = {
                "ref": ref,
                "role": entry.role,
                "bounds": {"x": b.x, "y": b.y, "width": b.w, "height": b.h} if b else None,
            }
            label = f'{entry.role} "{entry.name or ""}" {ref}'  # 이름은 화면 HUD에만, trace에는 남기지 않는다
        except CuopError:
            target = (
                {"ref": ref} if trace.safe_ref(ref) else {}
            )  # 모양이 틀린 ref(에이전트가 넣은 글자)는 남기지 않는다
    elif args.get("app"):
        label = str(args["app"])
    try:
        step = trace.next_step(ns) if path else int(time.time() * 10) % 100000 if show else 0  # trace 없으면 잠금 없이
    except OSError:
        step = 0
    if show:
        hud.send({"kind": "step", "phase": "start", "step": step, "command": name, "label": label,
                  "bounds": target.get("bounds"), "session": ctx.session})  # fmt: skip
    env, code = dispatch(name, ctx, args, g)
    if show:
        err: dict[str, Any] = env.get("error") or {}
        hud.send({"kind": "step", "phase": "end", "step": step, "command": name, "label": label, "ok": env["ok"],
                  "code": err.get("code"), "ms": round((time.time() - started) * 1000)}, spawn=False)  # fmt: skip
    if path is not None:
        try:
            ev = trace.event(name, args, env, started, step)
            if target:
                ev["target"] = target
            trace.record(path, ev, ctx.session)
        except OSError as e:
            if g.get("trace_strict"):
                return fail(name, CuopError("INTERNAL", f"trace write failed: {e}", delivery="unknown")), 1
    return env, code


WAITABLE = frozenset({"snapshot", "press", "hover", "drag", "click", "double-click", "triple-click", "right-click",
                      "type", "set-value", "clear", "focus", "select", "toggle", "check", "uncheck", "expand",
                      "collapse", "scroll", "scroll-to"})  # fmt: skip


def dispatch(name: str, ctx: C.Ctx, args: dict[str, Any], g: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """명령 실행 + 전역 --wait-for/--wait-for-gone. envelope와 exit code."""
    if name == "mcp":  # stdio MCP 서버 — stdout은 JSON-RPC 전용, 끝날 때까지 돌아오지 않는다
        from cuop.desktop.mcp import serve

        serve(ctx, {k: v for k, v in g.items() if k in ("trace", "trace_strict")}, args.get("toolset") or "full")
        return ok(name, {}), 0
    fn = REGISTRY[name][0]
    selector, gone = g.get("wait_for") or g.get("wait_for_gone"), bool(g.get("wait_for_gone"))
    if g.get("wait_for") and g.get("wait_for_gone"):
        return fail(name, CuopError("INVALID_ARGS", "--wait-for and --wait-for-gone are mutually exclusive")), 1
    if selector and (name not in WAITABLE or args.get("root")):
        return fail(name, CuopError("INVALID_ARGS", f"--wait-for works on snapshot, press and ref actions (not {name}"
                                    + (" with --root" if args.get("root") else "") + ")")), 1  # fmt: skip
    if selector:
        try:
            W.Selector.parse(selector)  # 동작 전에 검사 — 잘못된 selector 때문에 동작 결과를 잃지 않게
        except CuopError as e:
            return fail(name, e), 1
    if selector and name == "snapshot":  # 먼저 기다리고 그다음 찍는다 — 실패하면 텍스트 기준 뷰도 그대로(감사 #11)
        try:
            waited = W.selector_wait(ctx, W.scope_of(ctx, name, args), selector, gone,
                                     g.get("wait_timeout") or 30000, then_snapshot=False)  # fmt: skip
            data = fn(ctx, **args)
            assert isinstance(data, dict)
            return ok(name, {**data, **{k: v for k, v in waited.items() if k != "gone" or gone}}), 0
        except CuopError as e:
            return fail(name, e), 1
        except Exception as e:  # noqa: BLE001
            return fail(name, CuopError("INTERNAL", f"{type(e).__name__}: {str(e)[:300]}", delivery="unknown")), 1
    pre: W.Scope | None = None  # 동작 전에 잡은 관찰 범위 — 동작이 ref 요소를 없애도(AC → C) 그 창을 다시 읽는다
    if name != "snapshot" and name in WAITABLE and (selector or ctx.fmt == "text"):
        try:
            pre = W.scope_of(ctx, name, args)
        except CuopError:
            pre = None
    try:
        data = fn(ctx, **args)
    except CuopError as e:
        return fail(name, e), 1
    except Exception as e:  # noqa: BLE001 — 예상 못 한 실패도 envelope로
        return fail(name, CuopError("INTERNAL", f"{type(e).__name__}: {str(e)[:300]}", delivery="unknown")), 1
    if isinstance(data, list):
        return ok(name, data), 0
    if not selector:
        if (
            ctx.fmt == "text" and name in WAITABLE - {"snapshot"} and not args.get("stdin")
        ):  # --stdin 글자를 관찰로 드러내지 않게
            _observe_after(ctx, name, args, data, pre)
        return ok(name, data), 0
    try:
        win: dict[str, Any] = data.get("window") or {}
        scope_args = (
            args
            if name != "snapshot"
            else {"app": args.get("app"), "window_id": args.get("window_id") or win.get("id")}
        )
        scope = pre or W.scope_of(ctx, name, scope_args)
        # --stdin: 스냅샷을 찍지도 저장하지도 않는다(입력 글자가 뷰 · Diff 기준에 남지 않게)
        got = W.selector_wait(ctx, scope, selector, gone, g.get("wait_timeout") or 30000,
                              then_snapshot=not args.get("stdin"))  # fmt: skip
    except Exception as raw:  # noqa: BLE001 — 관찰 실패도 동작 결과를 지니고 돌아간다
        e = raw if isinstance(raw, CuopError) else CuopError("INTERNAL", f"{type(raw).__name__}: {str(raw)[:300]}")
        if name != "snapshot":
            e.details["after_action"] = data
            disp: dict[str, Any] = data.get("disposition") or {}
            e.delivery = disp.get("delivery", e.delivery)
        return fail(name, e), 1
    if args.get("stdin"):  # --stdin 글자가 기다린 뒤의 스냅샷 값으로 드러나지 않게 — 트리는 빼고 결과만(감사 #15)
        got = {
            k: v for k, v in got.items() if k in ("snapshot_id", "matched_selector", "elapsed_ms", "gone", "ref_count")
        }
    if name != "snapshot":
        got["after_action"] = data
    return ok(name, got), 0


def _menu_command(ctx: C.Ctx, args: dict[str, Any]) -> bool:
    """잎 메뉴 명령인가 — 하위 메뉴를 여는 항목이면 메뉴 막대(열린 하위 메뉴)를 그대로 본다."""
    try:
        _, _, entry, el = C.resolve_ref(ctx.ns, str(args.get("ref") or ""))
    except CuopError:
        return False
    m = C.ax()
    return entry.raw_role.startswith("AXMenuItem") and not any(
        m.attr(c, "AXRole") == "AXMenu" for c in m.attr(el, "AXChildren") or ()
    )


def _observe_after(ctx: C.Ctx, name: str, args: dict[str, Any], data: dict[str, Any], pre: W.Scope | None) -> None:
    """텍스트 모드의 액션은 그 창의 변경분(Diff)을 바로 싣는다 — 확인용 snapshot 호출을 아낀다(CUOP 제안 방식)."""
    ctx = C.Ctx(session=ctx.session, headed=ctx.headed, fmt=ctx.fmt, goal=ctx.goal, auto=True)
    try:
        scope = pre or W.scope_of(ctx, name, args)
        wid = f"w-{scope.window_id}" if scope.window_id else None
        surface = scope.surface if scope.surface in ("menubar", "menu", "menu-bar-extras", "dock") else "window"
        if surface in ("menubar", "menu") and _menu_command(ctx, args):  # 잎 메뉴 명령은 창을 바꾼다 → 창의 Diff
            surface = "window"
        if surface != "window":
            obs = C.snapshot(ctx, app=scope.app, surface=surface, diff=True)
        else:
            obs = C.snapshot(ctx, app=None if wid else scope.app, window_id=wid, diff=True)
        data["observation"] = {"snapshot_id": obs["snapshot_id"], "complete": obs["complete"], "text": obs.get("text")}
    except Exception as e:  # noqa: BLE001 — 관찰 실패는 동작 결과를 바꾸지 않는다
        data["observation"] = {"error": e.code if isinstance(e, CuopError) else "INTERNAL"}


def batch(ctx: C.Ctx, json_: str, stop_on_error: bool, timeout_ms: int) -> dict[str, Any]:
    """한 프로세스에서 차례로(트랜잭션 아님). 항목 = {"command", "args", "session"?}. 중첩 batch 금지.

    모든 항목을 먼저 검사한다 — 뒤 항목이 잘못돼서 앞의 동작 결과(전달 증거)가 사라지지 않게.
    """
    from cuop.desktop.errors import VERSION

    if len(json_.encode()) > 1 << 20:
        raise CuopError("INVALID_ARGS", "batch JSON exceeds 1 MiB")
    try:
        items = json.loads(json_)
    except ValueError as e:
        raise CuopError("INVALID_ARGS", f"batch JSON: {e}") from None
    if not isinstance(items, list) or len(items) > 64:  # pyright: ignore[reportUnknownArgumentType]
        raise CuopError("INVALID_ARGS", "batch takes a JSON array of at most 64 entries")
    ctx.ns  # 끝난 세션이면 아무것도 실행하지 않는다  # noqa: B018
    plan: list[tuple[str, dict[str, Any], str | None]] = []
    raw: list[Any] = items  # pyright: ignore[reportUnknownVariableType]
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            raise CuopError("INVALID_ARGS", f"batch entry {i}: use {{command, args, session}}")
        entry: dict[str, Any] = dict(e)  # pyright: ignore[reportUnknownArgumentType]
        cmd: Any = entry.get("command")
        args: Any = entry.get("args") or {}
        if set(entry) - {"command", "args", "session"} or cmd == "batch" or cmd not in REGISTRY:
            raise CuopError("INVALID_ARGS", f"batch entry {i}: unknown, nested or malformed command {cmd!r}")
        if not isinstance(args, dict):
            raise CuopError("INVALID_ARGS", f"batch entry {i}: args must be an object")
        if args.get("stdin"):  # pyright: ignore[reportUnknownMemberType]
            raise CuopError("INVALID_ARGS", f"batch entry {i}: stdin is not available inside batch; use text")
        plan.append((str(cmd), _params(str(cmd), dict(args)), entry.get("session")))  # pyright: ignore[reportUnknownArgumentType]
    start = time.monotonic()
    deadline = start + timeout_ms / 1000
    results: list[dict[str, Any]] = []
    stopped: dict[str, Any] | None = None
    baseline: Any = None
    for i, (cmd, params, session) in enumerate(plan):
        sub = C.Ctx(session=session or ctx.session, headed=ctx.headed, fmt=ctx.fmt, goal=ctx.goal)
        head = {"version": VERSION, "command": cmd, "index": i}
        nxt = plan[i + 1] if i + 1 < len(plan) else None
        left = max(0, round((deadline - time.monotonic()) * 1000))
        sig = inspect.signature(REGISTRY[cmd][0]).parameters
        try:
            if "timeout_ms" in sig:  # 남은 batch 시간 안에서만 기다린다(형식이 틀리면 이 항목의 INVALID_ARGS)
                want = params.get("timeout_ms", sig["timeout_ms"].default)
                if want is not None and (isinstance(want, bool) or not isinstance(want, int)):
                    raise CuopError("INVALID_ARGS", f"{cmd}: timeout_ms must be an integer")
                params: dict[str, Any] = {**params, "timeout_ms": min(left if want is None else want, left)}
            sub.ns  # 항목마다 그 세션이 열려 있는지  # noqa: B018
            if nxt and nxt[0] == "wait" and nxt[1].get("event") and cmd != "wait":
                try:  # 동작 뒤 이벤트 대기: 기준선을 동작 전에 잡는다 — 못 잡으면 동작하지 않는다
                    baseline = W.Baseline.take(nxt[1]["event"], nxt[1].get("app"))
                except CuopError as err:  # 이 항목은 시작하지 않는다 — 아래에서 stop_on_error를 똑같이 적용
                    results.append({**head, "ok": False, "execution": "not_started",
                                    "not_started_reason": "baseline_failed", "error": err.to_json()})  # fmt: skip
                    baseline = None
            if results and results[-1]["index"] == i:
                pass
            elif cmd == "wait" and baseline is not None and params.get("event"):
                data = W.wait_event(
                    baseline, params.get("window_id"), params.get("window"), params.get("timeout", 30000)
                )
                baseline = None
                results.append({**head, "ok": True, "execution": "completed", "data": data})
            else:  # 항목마다 HUD · trace를 거친다 — 사람이 batch 안의 단계도 본다(감사 #12)
                env, _ = observed(cmd, sub, params, {})
                body = {"data": env["data"]} if env["ok"] else {"error": env["error"]}
                results.append({**head, "ok": bool(env["ok"]), "execution": "completed", **body})
        except CuopError as err:
            results.append({**head, "ok": False, "execution": "completed", "error": err.to_json()})
        except Exception as err:  # noqa: BLE001 — 한 항목의 예상 못 한 실패가 앞 결과를 지우지 않게
            bad = CuopError("INTERNAL", f"{type(err).__name__}: {str(err)[:300]}", delivery="unknown")
            results.append({**head, "ok": False, "execution": "completed", "error": bad.to_json()})
        if not results[-1]["ok"] and stop_on_error:
            stopped = {"reason": "stop_on_error", "index": i}
            break
        if time.monotonic() > deadline and i + 1 < len(plan):
            stopped = {"reason": "deadline", "after_index": i}
            break
    done = len(results)
    for j, (cmd, _, _) in enumerate(plan[done:], start=done):
        why = "stop_on_error" if stopped and stopped["reason"] == "stop_on_error" else "deadline"
        err = CuopError("ACTION_FAILED", f"not started ({why})", details={"kind": "not_started"})
        results.append({"version": VERSION, "ok": False, "command": cmd, "index": j, "execution": "not_started",
                        "not_started_reason": why, "error": err.to_json()})  # fmt: skip
    body: dict[str, Any] = {
        "results": results,
        "semantics": {
            "atomic": False,
            "order": "sequential",
            "batch_retries": False,
            "command_retry_contracts": "preserved",
            "successful_action_disposition": "data.disposition",
            "error_disposition": "error.disposition",
        },  # fmt: skip
        "total_entries": len(plan),
        "completed_entries": done,
        "not_started_entries": len(plan) - done,
        "timeout_ms": timeout_ms,
        "elapsed_ms": round((time.monotonic() - start) * 1000),
        "limits": {"max_entries": 64, "max_input_bytes": 1 << 20},
    }
    if stopped:
        body["stopped"] = stopped
    return body


command("batch", batch, a("json_", metavar="JSON"), a("--stop-on-error", action="store_true"),
        a("--timeout-ms", type=int, default=60000))  # fmt: skip


def _params(cmd: str, args: dict[str, Any]) -> dict[str, Any]:
    """batch 인자 → 함수 인자. 키 = 명령 자신의 필드 이름(`ref_id` → ref, `combo`, `text` …)."""
    fn = REGISTRY[cmd][0]
    sig = inspect.signature(fn)
    alias = {
        "ref_id": "ref",
        "from": "from_",
        "json": "json_",
    }
    out = {alias.get(k, k): v for k, v in args.items()}
    unknown = set(out) - set(sig.parameters) - {"ctx"}
    if unknown:
        raise CuopError("INVALID_ARGS", f"{cmd}: unknown fields {sorted(unknown)}")
    try:
        sig.bind(None, **out)  # 필수 인자 누락도 실행 전에(앞 항목이 이미 동작한 뒤 INTERNAL로 끝나지 않게)
    except TypeError as e:
        raise CuopError("INVALID_ARGS", f"{cmd}: {e}") from None
    return out


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") or len(sys.argv) == 1:
        build().print_help(sys.stderr)
        sys.exit(0)
    argv = ["version" if x in ("-V", "--version") and i == 0 else x for i, x in enumerate(sys.argv[1:])]
    if "-v" in argv or "--verbose" in argv:  # 진단은 stderr로만(stdout은 envelope 한 줄)
        import logging

        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="cuop %(levelname)s %(name)s: %(message)s")
    env, code = run(argv)
    if env["command"] == "mcp" and env["ok"]:
        sys.exit(0)
    sys.stdout.write(json.dumps(env, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.exit(code)
