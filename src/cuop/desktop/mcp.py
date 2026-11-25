"""`cuop mcp` — 모든 명령을 stdio MCP 도구로(agent-desktop 카탈로그와 같은 이름: `desktop_<command>` ·
하위 명령은 `desktop_session_start`처럼 하나씩, 인자 이름도 공개 스키마(`ref_id` · `from` …)).

도구의 인자 = 그 명령 함수의 인자(시그니처에서 스키마를 만든다). 결과 = structuredContent와 텍스트 블록의
envelope, 인라인 스크린샷은 이미지 블록. `--format text`로 띄우면 텍스트 블록에서만 텍스트 관찰
(`data.text` · `data.observation.text`)을 JSON 밖 다음 줄에 붙인다 — 줄바꿈 escape 토큰을 아낀다.
`mcp`에 준 전역 플래그(--session · --headed · --format · --goal)는 모든 호출에 적용된다. 호출은 한 번에 하나씩(잠금).
"""

import inspect
import json
import os
import threading
import time
from typing import Annotated, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent
from pydantic import Field

from cuop.desktop import commands as C
from cuop.optimizer import jev

SKIP = frozenset({"mcp"})
# agent-desktop처럼 하위 명령마다 도구 하나(desktop_session_start …) — 동작 인자 대신 이름, 그 동작이 쓰는 인자만
LEAVES: dict[str, dict[str, tuple[str, ...]]] = {
    "session": {"start": ("name", "no_trace", "screenshots", "cursor", "multi_agent"), "end": ("id",), "list": (),
                "gc": ("ended", "older_than")},
    "trace": {"show": ("limit", "event"), "export": ("limit", "out")},
    "skills": {"list": (), "get": ("name", "reference", "full"), "path": ()},
    "cursor-overlay": {"enable": (), "disable": (), "status": ()},
}  # fmt: skip
# 공개 인자 이름(agent-desktop MCP 스키마) ← 함수 인자 이름
RENAME = {"ref": "ref_id", "from_": "from"}
RENAME_BY = {"find": {"state": "states"}, "launch": {"arg": "args"}, "screenshot": {"path": "output_path"},
             "batch": {"json_": "commands_json"}}  # fmt: skip
_OPT = str | int | float | bool | None
# 상태 없는 프로세스라 누른 채로 둘 수 없어 항상 거부하지만, 인자 모양은 agent-desktop과 같게(검사 후 거부)
HELD: dict[str, list[tuple[str, Any, Any]]] = {
    "key-down": [("combo", str, inspect.Parameter.empty), ("force", bool, False)],
    "key-up": [("combo", str, inspect.Parameter.empty), ("force", bool, False)],
    "mouse-down": [("xy", str, inspect.Parameter.empty), ("button", str, "left"), ("modifiers", str | None, None)],
    "mouse-up": [("xy", str, inspect.Parameter.empty), ("button", str, "left"), ("modifiers", str | None, None)],
}
# headed 인자를 받는 도구 — ref 액션 · 키 · 마우스 · 알림 변경(물리 입력이나 앞으로 가져오기가 필요할 수 있는 것)
HEADED = frozenset({"click", "double-click", "triple-click", "right-click", "type", "set-value", "clear", "focus",
                    "select", "toggle", "check", "uncheck", "expand", "collapse", "scroll", "scroll-to", "press",
                    "hover", "drag", "mouse-move", "mouse-click", "mouse-wheel", "dismiss-notification",
                    "dismiss-all-notifications", "notification-action", "notifications"})  # fmt: skip
# --toolset core: 자주 쓰는 도구만 짧은 설명으로 — 도구 정의는 턴마다 다시 읽히는 고정비다(전체 68개 ≈ 10k토큰, D14)
CORE: dict[str, str] = {
    "snapshot": 'Read a window as text: one line per element `eN role "name" ="value" [states]`; refs are '
    "@<snapshot>:eN from the header. goal = words on screen to keep only related elements; root = a ref to drill into; "
    "surface = menubar | menu | menu-bar-extras | sheet | popover | alert; diff = only changes since your last view. "
    "interactive_only hides read-only text such as a result display. For a menu command use find(surface=menubar, "
    "name=…) and click it. `^(…)` marks superscript text (2^(10) = 2¹⁰).",
    "find": "Find elements by role / name / text in an app (also closed menus with surface=menubar); returns refs.",
    "get": "Read one property of a ref (value, bounds, states, …).",
    "click": "Click (press) a ref. Menu items run even in closed menus. Each action returns a diff of the window "
    "(`(no change)` = its accessibility tree did not change); take refs from the newest header — a control whose "
    "label changed (AC → C) can need its new ref. Wait for one click's result before the next dependent click.",
    "double_click": "Double-click a ref (needs headed).",
    "right_click": "Open a ref's context menu.",
    "type": "Insert text at the caret of a ref (headless: accessibility insert; headed: real key presses).",
    "set_value": "Replace a field's whole value.",
    "focus": "Give a ref keyboard focus (e.g. a document area before pressing keys).",
    "clear": "Empty a field.",
    "select": "Choose an option by its label in a popup, combo box or list.",
    "check": "Turn a checkbox / switch / radio on.",
    "uncheck": "Turn a checkbox / switch off.",
    "toggle": "Flip a checkbox / switch.",
    "expand": "Expand a disclosure / tree row.",
    "collapse": "Collapse a disclosure / tree row.",
    "scroll": "Scroll a ref's scroll area (direction up|down|left|right, amount).",
    "press": "Send a key or shortcut (e.g. return, cmd+s) to app / window_id.",
    "wait": "Wait for ms, an element (role:text), a window, text, a menu or an event.",
    "launch": "Open an app that is not running (background unless activate); a running app needs no launch.",
    "focus_window": "Bring a window to the front.",
    "list_windows": "List an app's windows (ids w-N for window_id).",
    "screenshot": "Capture one window as an image (for canvases and visual questions).",
    "mouse_click": "Click screen point xy=X,Y (points; headed). From a screenshot: origin + pixel / scale_factor.",
    "skills_get": "Read the full usage guide (name=cuop or cuop-macos).",
}
# 커서 모양 인자: 받기만 한다(D10 — cuop은 커서 대신 HUD)
CURSOR_STYLE = ("accent", "bow", "dwell_ms", "fill", "hotspot", "image", "label", "max_words", "motion_seed",
                "multi_agent", "no_highlight", "no_ripple", "overshoot", "pointer_hotspot", "pointer_image", "rim",
                "size", "travel_ms", "tremor")  # fmt: skip
_LOCK = threading.Lock()


def render(env: dict[str, Any]) -> str:
    """envelope → 도구 결과 문자열. 텍스트 관찰은 JSON에서 빼서 아래 줄에 원문으로."""
    raw: object = env.get("data")
    if not isinstance(raw, dict):
        return json.dumps(env, ensure_ascii=False, separators=(",", ":"))
    data = cast(dict[str, Any], raw).copy()
    text = data.pop("text", None) if isinstance(data.get("text"), str) else None
    obs: dict[str, Any] = data.get("observation") or {}
    if text is None and isinstance(obs.get("text"), str):
        obs = obs.copy()
        text = obs.pop("text")
        data["observation"] = obs
    head = json.dumps({**env, "data": data}, ensure_ascii=False, separators=(",", ":"))
    return head if text is None else f"{head}\n{text}"


def _image_tokens(env: dict[str, Any]) -> int:
    """인라인 스크린샷의 토큰 추정(긴 변 1568 · 1.15MP로 줄인 뒤 w·h/750, D4) — 하네스 Step 로그용."""
    import math

    data: Any = env.get("data")
    if not isinstance(data, dict):
        return 0
    d: dict[str, Any] = dict(data)  # pyright: ignore[reportUnknownArgumentType]
    w, h = float(d.get("width") or 0), float(d.get("height") or 0)
    if not isinstance(d.get("data"), str) or w <= 0 or h <= 0:
        return 0
    s = min(1.0, 1568 / max(w, h), math.sqrt(1_150_000 / (w * h)))
    return round(w * s * h * s / 750)


def _step_log(
    name: str,
    env: dict[str, Any],
    out: str,
    t0: float,
    image_tokens: int = 0,
    client: str | None = None,
    guide: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """하네스용(`CUOP_STEP_LOG` · `CUOP_RUN_ID`): 도구 호출마다 결과 토큰 수만 한 줄 — 내용은 남기지 않는다."""
    path = os.environ.get("CUOP_STEP_LOG")
    if not path:
        return
    from cuop.desktop.text import tokens

    err: dict[str, Any] = env.get("error") or {}
    row = {"run_id": os.environ.get("CUOP_RUN_ID", ""), "tool": name, "ok": env.get("ok"), "error": err.get("code")}
    row |= {"out_tokens": tokens(out), "image_tokens": image_tokens,
            "latency_ms": round((time.monotonic() - t0) * 1000), "client": client, "guide": guide}  # fmt: skip
    row |= extra or {}  # Jev 계측(호출 · 토큰 · 지연 · 경로 판단) — 내용 · 키는 없다
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _image(env: dict[str, Any]) -> ImageContent | None:
    """인라인 스크린샷 바이트는 이미지 블록으로 옮긴다(agent-desktop call.rs)."""
    raw: object = env.get("data")
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, Any], raw)
    fmt = data.get("format")
    if not isinstance(fmt, str):
        return None
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(fmt)
    if mime is None or not isinstance(encoded := data.get("data"), str):
        return None
    env["data"] = {k: v for k, v in data.items() if k != "data"}
    return ImageContent(type="image", data=encoded, mime_type=mime)


def _params(name: str, fixed: dict[str, Any]) -> tuple[list[inspect.Parameter], dict[str, str]]:
    """도구 인자 목록과 공개 이름 → 함수 인자 이름 표. 파이썬 예약어(from)는 별칭으로."""
    from cuop.desktop.cli import REGISTRY

    if name in HELD:
        params = [
            inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=d, annotation=t) for n, t, d in HELD[name]
        ]
    else:
        sig = inspect.signature(REGISTRY[name][0])
        skip = {"ctx", "stdin", *fixed}
        params = [p for n, p in sig.parameters.items() if n not in skip and p.kind is not p.VAR_KEYWORD]
    names: dict[str, str] = {}
    out: list[inspect.Parameter] = []
    for p in params:
        public = RENAME_BY.get(name, {}).get(p.name) or RENAME.get(p.name, p.name)
        names[public] = p.name
        ann = p.annotation
        if public != p.name:
            ann = Annotated[ann, Field(alias=public)]  # 스키마 · 입력 키 = 공개 이름
        out.append(p.replace(kind=inspect.Parameter.KEYWORD_ONLY, annotation=ann))
    return out, names


def _slim(schema: Any) -> Any:
    """JSON 스키마에서 토큰만 먹는 부분을 뺀다: title, 선택 인자의 null 분기(anyOf [X, null] → X). 검증은 pydantic."""
    if isinstance(schema, list):
        return [_slim(x) for x in schema]  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(schema, dict):
        return schema
    src: dict[str, Any] = dict(schema)  # pyright: ignore[reportUnknownArgumentType]
    d: dict[str, Any] = {k: _slim_props(v) if k == "properties" else _slim(v) for k, v in src.items() if k != "title"}
    alts: list[Any] = list(d.get("anyOf") or [])
    rest = [a for a in alts if a != {"type": "null"}]
    one: dict[str, Any] = dict(rest[0]) if len(rest) == 1 and isinstance(rest[0], dict) else {}  # pyright: ignore[reportUnknownArgumentType]
    if len(rest) < len(alts) and set(one) == {"type"} and isinstance(one["type"], str):
        d.pop("anyOf")  # {"anyOf":[{"type":X},{"type":"null"}]} → {"type":[X,"null"]} — null도 그대로 받는다
        d["type"] = [one["type"], "null"]
    if d.get("default", 0) is None:
        d.pop("default")
    return d


def _slim_props(props: Any) -> Any:
    """properties의 키는 인자 이름이다(focus-window의 `title` 등) — 지우지 않고 값만 줄인다."""
    if not isinstance(props, dict):
        return props
    p: dict[str, Any] = dict(props)  # pyright: ignore[reportUnknownArgumentType]
    return {k: _slim(v) for k, v in p.items()}


# 서버 지시문 = 호출 수를 줄이는 안내(INSTRUCTIONS_BRIEF, ≤ 400토큰). 누적 입력 ≈ 모델 호출 수 × 대화 길이라
# 호출 하나를 덜 부르는 것이 관찰 하나를 줄이는 것보다 크다(CODEX_CU_COMPARE: cuop 모델 호출 52–76 · CU 25–32).
# SKILL.md 'Fewest calls'와 같은 내용.
INSTRUCTIONS = (
    "Desktop computer use through the macOS accessibility tree. Every model turn re-reads the whole conversation, "
    "so make as few calls as you can.\n"
    "- Start with one desktop_snapshot; pass goal = words shown on screen for what you need. Refs are "
    "@<snapshot>:eN from the header.\n"
    "- Most actions return the window's diff. Trust it: do not snapshot, find or get the same screen again to "
    "confirm. If it is missing, snapshot with diff=true (changes only).\n"
    "- Use find only for a ref you have not seen (a menu command: find surface=menubar name=…). Do not get a "
    "property that the tree already shows.\n"
    "- Retry only when error.disposition.retry is safe.\n"
    "- Actions are headless; pass headed=true when a background app ignores semantic input (a search field, a list "
    "that will not open, typing into Office): type and press then send real keys and bring that window forward. "
    "Words go in with type or set_value; "
    "press is for single keys and shortcuts.\n"
    "- Some apps keep their content out of the tree: Excel's cell grid, PowerPoint's slide canvas, canvases and "
    "images. Do not search the tree for it; go straight to keys and at most one screenshot (Excel: headed type a "
    "cell address into the Name Box + return, then headed type the value). Other apps: use the tree.\n"
    "- Word builds its body text only after it has been frontmost once: focus_window, then snapshot (allow a few "
    "seconds).\n"
    "- Open/Save panels: press cmd+shift+g, type the full path, press return — once, instead of browsing folders.\n"
    "Full guide: desktop_skills_get name=cuop (macOS details: cuop-macos)."
)
# MCP 지시문을 모델에 넣지 않는 클라이언트(실측, INSTRUCTIONS_EXP 1단계) — 첫 snapshot 응답 머리에 한 번 붙인다
NO_INSTRUCTIONS = frozenset({"codex-mcp-client"})


ROUTE_MIN = 0.7  # Jev 경로 판단 확신도 임계값 — 낮으면 힌트 없이 규칙(지시문) 그대로(D18)
ROUTE_LABEL = {"keys": "keys+screenshot", "screenshot": "screenshot"}


def _route(name: str, env: dict[str, Any], args: dict[str, Any], call: C.Ctx, out: str, routed: set[str]) -> str:
    """Jev 플러그인: 창마다 첫 snapshot에서 '작업 내용이 트리에 있는가'를 묻고, tree가 아니면 한 줄 힌트를 붙인다."""
    if name != "snapshot" or env.get("ok") is not True or call.fmt != "text" or not jev.enabled():
        return out
    if args.get("surface", "window") != "window" or args.get("root") or args.get("diff"):
        return out
    data = cast(dict[str, Any], env.get("data") or {})
    win = cast(dict[str, Any], data.get("window") or {})
    wid = str(win.get("id") or "")
    head = next((line for line in out.split("\n") if line.startswith("[@")), "")  # 첫 줄은 JSON 봉투일 수 있다
    if not wid or not head or wid in routed or "(partial" in head:  # 창 id가 있는 완전한 첫 뷰만(부분 뷰로 쓰지 않게)
        return out
    routed.add(wid)
    r = jev.route(head, out[:6000], call.goal)
    shown = r is not None and r[0] != "tree" and r[1] >= ROUTE_MIN
    jev.STATS.events.append({"route": r[0] if r else None, "conf": round(r[1], 3) if r else None, "shown": shown})
    if r is not None and shown:
        return f"[route: {ROUTE_LABEL[r[0]]}, jev {r[1]:.2f}]\n{out}"
    return out


def _client(mctx: Any) -> str | None:
    try:
        return str(mctx.session.client_params.client_info.name)
    except Exception:  # noqa: BLE001 — 요청 컨텍스트 없이 직접 부른 경우(SDK는 ValueError) · clientInfo 없음
        return None


def build(ctx: C.Ctx, globals_: dict[str, Any], toolset: str = "full") -> MCPServer:
    from cuop.desktop.cli import REGISTRY, observed

    server = MCPServer(
        "cuop",
        instructions=INSTRUCTIONS,
    )

    guide = [True]  # 지시문을 못 받는 클라이언트에게 첫 snapshot으로 한 번
    routed: set[str] = set()  # Jev 경로 판단을 한 창(서버마다 창 하나에 한 번)

    def add(
        name: str, tool_name: str, fixed: dict[str, Any], keep: tuple[str, ...] | None, extra: tuple[str, ...]
    ) -> None:
        params, names = _params(name, fixed)
        if keep is not None:
            params = [p for p in params if p.name in keep]
            names = {k: v for k, v in names.items() if v in keep}
        params += [inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=_OPT)
                   for n in extra if n not in names]  # fmt: skip
        if name in HEADED:  # 호출마다 물리 입력 허용(전역 --headed와 같은 뜻) — agent-desktop 스키마의 상위 집합
            params.append(inspect.Parameter("headed", inspect.Parameter.KEYWORD_ONLY, default=False, annotation=bool))
        if name == "snapshot":  # 호출마다 goal(전역 --goal과 같은 뜻) — 텍스트 형식의 Task-aware Filter
            params.append(
                inspect.Parameter("goal", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None)
            )

        params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context))

        def tool(**kw: Any) -> CallToolResult:
            mctx = kw.pop("ctx", None)
            headed = bool(kw.pop("headed", False))
            goal = kw.pop("goal", None) or ctx.goal
            args = {names[k]: v for k, v in kw.items() if k in names} | fixed  # extra(받기만 하는 인자)는 버린다
            call = C.Ctx(session=ctx.session, headed=headed or ctx.headed, fmt=ctx.fmt, goal=goal)
            with _LOCK:
                t0 = time.monotonic()
                env, _ = observed(name, call, args, dict(globals_))
                shot = _image_tokens(env)  # _image가 바이트를 옮기기 전에
                image = _image(env)
                out = render(env)
                who = _client(mctx)
                once = (name == "snapshot" and env.get("ok") is True and guide[0] and who in NO_INSTRUCTIONS
                        and call.fmt == "text")  # fmt: skip  # JSON 형식은 첫 줄이 봉투 — 머리말을 붙이지 않는다
                out = _route(name, env, args, call, out, routed)
                if once:
                    out = f"[cuop guide — read once]\n{INSTRUCTIONS}\n\n{out}"
                _step_log(tool_name, env, out, t0, shot, who, once, jev.STATS.take() if jev.enabled() else None)
                if once:
                    guide[0] = False  # 서버(= 에이전트 세션)마다 한 번 — 응답을 만든 뒤에 내린다
            content: list[ContentBlock] = [TextContent(type="text", text=out)]
            if image is not None:
                content.append(image)
            # structuredContent는 JSON 모드에서만(agent-desktop 호환) — 텍스트 모드는 같은 내용을 두 번 안 보낸다
            structured = env if ctx.fmt != "text" else None
            return CallToolResult(content=content, structured_content=structured, is_error=env.get("ok") is not True)

        tool.__signature__ = inspect.Signature(params, return_annotation=CallToolResult)  # type: ignore[attr-defined]
        tool.__annotations__ = {p.name: p.annotation for p in params} | {"return": CallToolResult}
        tool.__name__ = tool_name
        fn = REGISTRY[name][0]
        short = tool_name.removeprefix("desktop_")
        if toolset == "core":
            desc = CORE[short]
        else:
            doc = (inspect.getdoc(fn) or f"agent-desktop `{name}` command").split("\n\n", 1)[0]
            desc = f"agent-desktop {' '.join(filter(None, [name, *fixed.values()]))}: {doc}"
        server.add_tool(tool, name=tool_name, description=desc, structured_output=False)
        registered = server._tool_manager.get_tool(tool_name)  # pyright: ignore[reportPrivateUsage]
        if registered is not None:
            registered.parameters = _slim(registered.parameters)

    for name in REGISTRY:
        if name in SKIP:
            continue
        base = "desktop_" + name.replace("-", "_")
        leaves = [(f"{base}_{a}", {"action": a}, keep) for a, keep in LEAVES[name].items()] if name in LEAVES else []
        for tool_name, fixed, keep in leaves or [(base, {}, None)]:
            if toolset == "core" and tool_name.removeprefix("desktop_") not in CORE:
                continue
            extra = CURSOR_STYLE if tool_name == "desktop_cursor_overlay_enable" else ()
            add(name, tool_name, fixed, keep, extra)
    return server


def serve(ctx: C.Ctx, globals_: dict[str, Any], toolset: str = "full") -> None:
    build(ctx, globals_, toolset).run()  # stdio
