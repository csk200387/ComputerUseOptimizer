"""명령 구현 — CLI · batch · MCP가 같은 함수를 부른다. 각 명령은 envelope의 data를 돌려주거나 CuopError를 던진다.

OS 종속 호출은 collectors.ax(macOS)로만 한다(지연 import — 다른 OS에서도 import 가능).
"""

import platform
import time
from dataclasses import dataclass
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Any

from cuop.desktop import present
from cuop.desktop.errors import CuopError
from cuop.desktop.present import (
    compact as compact_tree,
)
from cuop.desktop.present import (
    identity_key,
    refs_of,
    relabel,
    to_json,
    twins,
)
from cuop.desktop.store import Namespace, RefEntry, Snapshot, parse_ref, state_root
from cuop.schema import Role, UIElementNode

if TYPE_CHECKING:
    from cuop.collectors import ax as AX  # noqa: N812

INTERACTIVE_ROLES = frozenset({"button", "cell", "checkbox", "colorwell", "combobox", "dockitem", "incrementor", "link",
    "listbox", "menubutton", "menuitem", "option", "radio", "slider", "switch", "tab", "textbox", "treeitem",
    "scrollarea", "disclosure", "datefield", "timefield"})  # fmt: skip
SURFACES = ("window", "focused", "menu", "menubar", "sheet", "popover", "alert", "dock", "menu-bar-extras")
APP_OWNED = ("menu", "menubar", "menu-bar-extras", "dock")
DIALOGS = ("AXDialog", "AXSystemDialog")  # alert surface가 되는 창 subrole


def ax() -> Any:
    from cuop.collectors import ax as mod

    if not mod.trusted():
        raise CuopError(
            "PERM_DENIED",
            "Accessibility permission is not granted to the app that launches cuop",
            details={"permission": "accessibility"},
        )
    return mod


@dataclass
class Ctx:
    """명령 하나의 실행 문맥 — 전역 플래그."""

    session: str | None = None
    headed: bool = False
    fmt: str = "json"  # json | text (CUOP compact 텍스트)
    goal: str | None = None
    auto: bool = (
        False  # 액션 뒤 자동 관찰 — 그 창에 마지막으로 쓴 goal을 이어 쓴다(필터가 바뀌어 거짓 삭제가 나지 않게)
    )

    @property
    def ns(self) -> Namespace:
        ns = Namespace(self.session)
        ns.check_open()
        return ns


# --- 대상 해석 ---
@dataclass
class Target:
    el: Any
    app: AX.App
    window: AX.Window | None
    surface: str


def _wid(text: str | None) -> int | None:
    if text is None:
        return None
    raw = text.removeprefix("w-")
    if not raw.isdigit():
        raise CuopError("INVALID_ARGS", f"--window-id must look like w-1234, got {text!r}")
    return int(raw)


def resolve_target(app: str | None, window_id: str | None, surface: str = "window") -> Target:
    tgt = _resolve_target(app, window_id, surface)
    ax().allowed(tgt.app.pid)
    return tgt


def _resolve_target(app: str | None, window_id: str | None, surface: str = "window") -> Target:
    m = ax()
    if surface not in SURFACES:
        raise CuopError("INVALID_ARGS", f"unknown surface {surface!r}", details={"supported_surfaces": list(SURFACES)})
    wid = _wid(window_id)
    if surface == "dock":
        dock = m.find_app("com.apple.dock")
        lst = next((c for c in m.attr(m.AXUIElementCreateApplication(dock.pid), "AXChildren") or ()), None)
        if lst is None:
            raise CuopError("WINDOW_NOT_FOUND", "the Dock exposes no list")
        return Target(lst, dock, None, surface)
    if surface == "menu-bar-extras" and app is None:
        cc = m.find_app("com.apple.controlcenter")
        bar = m.attr(m.AXUIElementCreateApplication(cc.pid), "AXExtrasMenuBar")
        if bar is None:
            raise CuopError("WINDOW_NOT_FOUND", "no menu bar extras surface")
        return Target(bar, cc, None, surface)
    if wid is not None and app is None:
        win = m.window_by_id(wid)
        a = m.app_by_pid(win.pid) or m.App(win.pid, str(win.pid), None, 0, None, None)
    else:
        a = m.find_app(app) if app else _front_app(m)
        win = m.window_by_id(wid, a.pid) if wid is not None else None
    app_el = m.AXUIElementCreateApplication(a.pid)
    if surface == "menubar":
        bar = m.attr(app_el, "AXMenuBar")
        if bar is None:
            raise _absent("menubar")
        return Target(bar, a, None, surface)
    if surface == "menu-bar-extras":
        bar = m.attr(app_el, "AXExtrasMenuBar")
        if bar is None:
            raise CuopError("WINDOW_NOT_FOUND", f"{a.name} has no menu bar extra")
        return Target(bar, a, None, surface)
    if surface == "menu":
        menu = _open_menu(m, app_el)
        if menu is None:
            raise _absent("menu")
        return Target(menu, a, None, surface)
    if surface == "focused":
        el = m.attr(app_el, "AXFocusedUIElement")
        if el is None:
            raise CuopError("WINDOW_NOT_FOUND", f"{a.name} has no focused element")
        return Target(el, a, None, surface)
    if surface == "alert" and win is None:
        # 앱 모달 대화상자(Excel '이동' · 경고)는 문서 창의 자식이 아니라 앱의 다른 창 — 기본 창을 고르기 전에 본다
        # (문서 창이 둘이면 pick_window가 AMBIGUOUS_TARGET을 낸다). 포커스된 것 먼저
        dlg = [
            w for w in m.app_windows(a.pid) if w.subrole in DIALOGS and not w.minimized and (w.focused or w.on_screen)
        ]
        if dlg:
            pick = min(dlg, key=lambda w: (not w.focused, not w.on_screen))
            return Target(pick.el, a, pick, surface)
    if win is None:
        win = m.pick_window(a.pid, a.name)
    if win is None:
        if a.policy != 0:  # 창 없는 백그라운드 앱(메뉴 막대 앱 · Control Center) → 프로세스로 관찰
            return Target(app_el, a, None, surface)
        raise CuopError(
            "WINDOW_NOT_FOUND",
            f"Application {a.name!r} is running but has no matching window",
            details={"app": a.name, "pid": a.pid},
        )
    if surface == "window":
        return Target(win.el, a, win, surface)
    if surface == "alert" and win.subrole in DIALOGS:  # --window-id로 고른 창이 곧 대화상자
        return Target(win.el, a, win, surface)
    want = {"sheet": ("AXSheet",), "popover": ("AXPopover",), "alert": ("AXSheet", "AXWindow")}[surface]
    found = _find_surface(m, win.el, want, alert=surface == "alert")
    if found is None:
        raise CuopError("ELEMENT_NOT_FOUND", f"no open {surface}", details=_absent_details(surface))
    return Target(found, a, win, surface)


def _absent_details(surface: str) -> dict[str, Any]:
    return {"kind": "surface_absent", "surface": surface, "complete": True}


def _absent(surface: str) -> CuopError:
    return CuopError("ELEMENT_NOT_FOUND", f"no open {surface} surface", details=_absent_details(surface))


def _front_app(m: Any) -> AX.App:
    pid = m.frontmost_pid()
    a = m.app_by_pid(pid) if pid else None
    if a is None:
        raise CuopError("APP_NOT_FOUND", "no frontmost application with a window")
    m.allowed(a.pid)  # 맨 앞 앱도 허용 목록 안이어야 — 창 목록 · 후보를 만들기 전에
    return a


def _open_menu(m: Any, app_el: Any) -> Any:
    """열린 메뉴: 앱 바로 아래 AXMenu(컨텍스트 메뉴) 또는 메뉴 막대 항목 아래 크기 있는 AXMenu."""
    for c in m.attr(app_el, "AXChildren") or ():
        if m.attr(c, "AXRole") == "AXMenu":
            return c
    bar = m.attr(app_el, "AXMenuBar")
    for item in m.attr(bar, "AXChildren") or () if bar is not None else ():
        for c in m.attr(item, "AXChildren") or ():
            size = m.attr(c, "AXSize")
            if m.attr(c, "AXRole") == "AXMenu" and isinstance(size, tuple) and size[0] and size[1]:
                return c
    return None


def _find_surface(m: Any, root: Any, roles: tuple[str, ...], alert: bool, depth: int = 6) -> Any:
    queue = [(root, 0)]
    while queue:
        el, d = queue.pop(0)
        for c in m.attr(el, "AXChildren") or ():
            r = m.attr(c, "AXRole")
            if r in roles and (not alert or r == "AXSheet" or m.attr(c, "AXSubrole") in DIALOGS):
                return c
            if d < depth:
                queue.append((c, d + 1))
    return None


# --- snapshot ---
def snapshot(
    ctx: Ctx,
    app: str | None = None,
    window_id: str | None = None,
    surface: str = "window",
    root: str | None = None,
    max_depth: int = 10,
    interactive_only: bool = False,
    compact: bool = False,
    skeleton: bool = False,
    include_bounds: bool = False,
    timeout_ms: int = 3000,
    force_electron_a11y: bool = False,
    diff: bool = False,
) -> dict[str, Any]:
    m = ax()
    ns = ctx.ns
    if root is not None:
        if surface != "window":
            raise CuopError("INVALID_ARGS", "--root cannot combine with --surface")
        base, _, entry, el = resolve_ref(ns, root)
        tgt = Target(el, m.app_by_pid(entry.pid) or m.App(entry.pid, base.app, base.bundle_id, 0, None, None), None,
                     entry.surface)  # fmt: skip
    else:
        base = None
        tgt = resolve_target(app, window_id, surface)
    renderer = m.renderer(tgt.app)
    if renderer == "chromium" and not force_electron_a11y:
        m.enable_web_accessibility(tgt.app.pid)
    depth = 3 if skeleton else max_depth
    tree, walk = m.read_tree(tgt.el, max_depth=depth, timeout_ms=timeout_ms)
    if root is not None and not walk.complete:
        raise CuopError(
            "TIMEOUT", "drill-down did not finish within --timeout-ms", details={"kind": "drill_incomplete"}
        )
    sid, ids = ns.allocate(walk.tokens) if base is None else (base.snapshot_id, ns.allocate(walk.tokens)[1])
    tree = relabel(tree, ids)
    if skeleton:  # 잘린 컨테이너는 drill 대상 → ref
        tree = _mark_drill(tree)
    shown: UIElementNode | None = tree
    if interactive_only:
        shown = present.interactive_only(tree)
    if compact and shown is not None:
        shown = compact_tree(shown)
    shown = shown or tree.model_copy(update={"children": []})
    published = list(shown.walk()) if ctx.fmt == "text" else refs_of(shown)  # 텍스트에 보인 id는 모두 쓸 수 있게
    snap = _publish(ns, tgt, tree, walk, ids, published, sid, base, exhaustive=root is None and not skeleton,
                    replace_drill=True)  # snapshot --root만 그 root의 지난 drill을 바꾼다(find는 더하기만)  # fmt: skip
    refs = snap.refs
    data: dict[str, Any] = {
        "app": tgt.app.name,
        "snapshot_id": sid,
        "ref_count": sum(1 for r in refs.values() if r.actions or r.role in INTERACTIVE_ROLES),
        "complete": walk.complete,
    }
    if snap.window_id is not None and tgt.surface not in APP_OWNED:
        data["window"] = {"id": f"w-{snap.window_id}", "title": snap.window_title}
    if not walk.complete:
        data.update(truncated=True, nodes_observed=walk.count)
        if any(r.token is None for r in refs.values()):
            data["identity_unproven"] = True
    if renderer:
        data["renderer"] = renderer
    if (
        walk.complete
        and tgt.app.bundle_id == "com.microsoft.Word"
        and any(n.raw_role.startswith("AXLayoutArea") and not n.children and not n.children_count for n in tree.walk())
    ) and not ax().is_frontmost(
        tgt.app.pid
    ):  # Word는 한 번 맨 앞에 와야 본문을 만든다(Office 시험) — 에이전트가 빈 문서 영역에서 헤매지 않게
        data["hint"] = ("a document area is empty while the app is in the background; some apps (Microsoft Word) "
                        "fill it only after focus-window (headed) — snapshot again ~3 s later")  # fmt: skip
    if ctx.fmt == "text":
        from cuop.desktop.text import render

        data["text"] = render(ns, snap, shown, ctx.goal, want_diff=diff, inherit=ctx.auto)
    else:
        data["tree"] = to_json(shown, sid, include_bounds)
    return data


def _publish(
    ns: Namespace,
    tgt: Target,
    tree: UIElementNode,
    walk: Any,
    ids: list[str],
    nodes: list[UIElementNode],
    sid: str,
    base: Snapshot | None,
    exhaustive: bool,
    replace_drill: bool = False,
) -> Snapshot:
    """ref를 refmap에 저장. 토큰 없는 ref의 capture proof(유일성)는 창 전체를 깊이 제한 없이 다 읽었을 때만 성립한다."""
    proof = exhaustive and walk.complete and not any(n.children_count for n in tree.walk())
    dup = twins(tree) if proof else set[tuple[str, str | None, str | None]]()
    pos = {eid: i for i, eid in enumerate(ids)}
    start = ax().process_start(tgt.app.pid)
    win_id = tgt.window.id if tgt.window else base.window_id if base else None
    refs = {
        n.id: RefEntry(
            token=walk.tokens[pos[n.id]], pid=tgt.app.pid, proc_start=start, role=n.role.value, raw_role=n.raw_role,
            name=n.name, native_id=n.native_id, window_id=win_id, surface=tgt.surface, bounds=n.bounds,
            actions=list(n.actions), unique=proof and identity_key(n) not in dup,
        )
        for n in nodes
    }  # fmt: skip
    if base is not None:
        return ns.save(base.model_copy(update={"refs": refs}), merge=True, drill=tree.id if replace_drill else None)
    return ns.save(
        Snapshot(
            snapshot_id=sid,
            created_at=time.time(),
            app=tgt.app.name,
            pid=tgt.app.pid,
            bundle_id=tgt.app.bundle_id,
            window_id=win_id,
            window_title=tgt.window.title if tgt.window else None,
            surface=tgt.surface,
            complete=walk.complete,
            refs=refs,
        )  # fmt: skip
    )


def _mark_drill(node: UIElementNode) -> UIElementNode:
    kids = [_mark_drill(c) for c in node.children]
    return node.model_copy(update={"children": kids, "ref": node.ref or node.children_count is not None})


# --- 재식별 ---
def resolve_ref(ns: Namespace, ref: str) -> tuple[Snapshot, str, RefEntry, Any]:
    """한정 ref → 살아 있는 AX 요소. 토큰이 있으면 토큰으로(쌍둥이도 각자), 없으면 capture proof로만."""
    snap, eid, entry = ns.resolve(ref)  # 형식 · 스냅샷 검사 먼저(OS 호출 없이)
    m = ax()
    m.allowed(entry.pid)
    if entry.proc_start is not None and m.process_start(entry.pid) != entry.proc_start:
        raise CuopError("STALE_REF", f"{ref}: the app restarted", details={"kind": "retained_object_stale",
                        "reason": "pid", "retryable": False})  # fmt: skip
    if entry.token is not None:  # 토큰이 있으면 토큰으로만 — 못 쓰면 닮은 요소를 찾지 않고 실패한다
        if not m.HAS_TOKENS:
            raise CuopError("STALE_REF", f"{ref}: this macOS cannot restore element addresses",
                            details={"kind": "retained_object_stale", "reason": "dead"})  # fmt: skip
        return snap, eid, entry, m.resolve(entry.token, entry.pid, entry.raw_role, entry.native_id)
    if not entry.unique or entry.window_id is None:
        raise CuopError(
            "AMBIGUOUS_TARGET",
            f"{ref} was not proven unique when it was captured",
            details={"kind": "identity_not_unique", "retryable": False},
        )
    win = m.window_by_id(entry.window_id, entry.pid)
    tree, walk = m.read_tree(win.el, max_depth=64, timeout_ms=5000)
    if not walk.complete:
        raise CuopError("TIMEOUT", "live read incomplete", details={"kind": "resolution_identity_unknown"})
    key = (entry.raw_role, entry.native_id, entry.name)
    hits = [walk.elements[int(n.id[1:])] for n in tree.walk() if identity_key(n) == key]
    if len(hits) != 1:
        kind = "saved_ref_absent" if not hits else "saved_ref_ambiguous"
        code = "STALE_REF" if not hits else "AMBIGUOUS_TARGET"
        raise CuopError(code, f"{ref}: {len(hits)} live matches", details={"kind": kind, "retryable": False})
    return snap, eid, entry, hits[0]


# --- find · get · is ---
def find(
    ctx: Ctx,
    app: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    name: str | None = None,
    value: str | None = None,
    text: str | None = None,
    description: str | None = None,
    native_id: str | None = None,
    exact: bool = False,
    root: str | None = None,
    surface: str = "window",
    state: list[str] | None = None,
    timeout_ms: int = 5000,
    count: bool = False,
    first: bool = False,
    last: bool = False,
    nth: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    m = ax()
    ns = ctx.ns
    picks = sum(bool(x) for x in (count, first, last, nth is not None))
    if picks > 1:  # 트리를 읽기 전에 검사한다(감사 #19)
        raise CuopError("INVALID_ARGS", "--count, --first, --last and --nth are mutually exclusive")
    if picks and limit is not None:
        raise CuopError("INVALID_ARGS", "--limit applies to the match list; drop it with --count/--first/--last/--nth")
    if root is not None and surface != "window":
        raise CuopError("INVALID_ARGS", "--root and --surface cannot combine")
    if root is not None:
        base, _, entry, el = resolve_ref(ns, root)
        tgt = Target(el, m.app_by_pid(entry.pid) or m.App(entry.pid, base.app, None, 0, None, None), None,
                     entry.surface)  # fmt: skip
    else:
        base, tgt = None, resolve_target(app, window_id, surface)
    tree, walk = m.read_tree(tgt.el, max_depth=64, timeout_ms=timeout_ms)
    sid, ids = ns.allocate(walk.tokens) if base is None else (base.snapshot_id, ns.allocate(walk.tokens)[1])
    tree = relabel(tree, ids)
    want_role = _role_query(role) if role else None
    states = [_state_query(s) for s in state or []]

    def match(field_value: str | None, q: str | None) -> bool:
        if q is None:
            return True
        if field_value is None:
            return False
        a, b = field_value.casefold(), q.casefold()
        return a == b if exact else b in a

    def subtree_has(n: UIElementNode, q: str) -> bool:  # 필드마다 따로 — 노드 · 필드 경계를 넘는 매치는 없다
        return any(match(x, q) for d in n.walk() for x in (d.name, d.value, d.description) if x)

    hits: list[UIElementNode] = []
    for n in tree.walk():
        if want_role is not None and n.role not in want_role:
            continue
        if not (match(n.name, name) and match(n.value, value) and match(n.description, description)):
            continue
        if native_id is not None and n.native_id != native_id:
            continue
        if text is not None and not subtree_has(n, text):
            continue
        if any((s in {x.value for x in n.states}) != want for s, want in states):
            continue
        hits.append(n)
    data: dict[str, Any] = {}
    if want_role is not None and not hits:
        data["roles_present"] = sorted({to_json(n, sid, False)["role"] for n in tree.walk()})
    exact_count = count or last or nth is not None
    if exact_count and not walk.complete:  # 다 못 읽은 트리의 개수 · 마지막 · n번째는 근거가 없다
        raise CuopError("TIMEOUT", "search did not finish within --timeout-ms", details={"kind": "locator_incomplete"})
    if count:
        return {**data, "count": len(hits)}
    total = len(hits)
    if first:
        hits = hits[:1]
    elif last:
        hits = hits[-1:]
    elif nth is not None:
        hits = hits[nth : nth + 1]
    lim = 50 if limit is None else limit
    truncated = bool(lim) and len(hits) > lim
    if truncated:
        hits = hits[:lim]
    # 텍스트 모드는 snapshot처럼 보인 매치를 모두 쓸 수 있게 — 아니면 에이전트가 snapshot에서 본 번호로 ref를 지어내
    # STALE_REF가 난다(Office C회차 PowerPoint 슬라이드 편집 영역 layoutitem, 3번)
    # 비조작 매치는 AX 토큰이 있을 때만(토큰 없는 ref는 다시 찾을 때 다른 surface의 닮은 요소로 갈 수 있다 — Codex)
    token = dict(zip(ids, walk.tokens, strict=False))
    usable = [n for n in hits if n.ref or (ctx.fmt == "text" and token.get(n.id) is not None)]
    _publish(ns, tgt, tree, walk, ids, usable, sid, base, exhaustive=root is None)
    pub = {n.id for n in usable}
    matches = [_match(n, sid, n.id in pub) for n in hits]
    if first or last or nth is not None:
        if not matches:
            raise CuopError("ELEMENT_NOT_FOUND", "no element matched", details={"kind": "no_match", **data})
        return {**data, "snapshot_id": sid, "match": matches[0], "complete": walk.complete}
    return {**data, "snapshot_id": sid, "matches": matches, "total_matches": total, "truncated": truncated,
            "complete": walk.complete}  # fmt: skip


def _match(n: UIElementNode, sid: str, usable: bool) -> dict[str, Any]:
    j = to_json(
        n.model_copy(update={"children": [], "ref": usable}), sid, bounds=not n.ref
    )  # 조작 요소가 아니면 bounds도
    j["interactive"] = n.ref
    return j


ROLE_ALIASES = {
    "gridcell": "cell", "img": "image", "radio": "radiobutton", "searchbox": "textfield", "searchfield": "textfield",
    "textarea": "textfield", "textbox": "textfield", "spinbutton": "incrementor", "togglebutton": "button",
    "tree": "outline", "text": "statictext",
}  # fmt: skip


def _role_query(role: str) -> set[Role]:
    from cuop.desktop.present import AD_ROLE

    q = ROLE_ALIASES.get(role.strip().lower(), role.strip().lower())
    hits = {r for r in Role if AD_ROLE.get(r, r.value) == q}
    if not hits:
        raise CuopError("INVALID_ARGS", f"unknown role {role!r}", details={"kind": "unknown_role"})
    return hits


def _state_query(s: str) -> tuple[str, bool]:
    token, _, flag = s.partition("=")
    return token.strip().lower(), flag.strip().lower() not in ("false", "0", "no")


GET_PROPS = ("text", "value", "title", "bounds", "role", "states")
IS_PROPS = ("visible", "enabled", "checked", "focused", "expanded", "selected")


def get(ctx: Ctx, ref: str, property: str = "text") -> dict[str, Any]:  # noqa: A002
    if property not in GET_PROPS:
        raise CuopError("INVALID_ARGS", f"--property must be one of {', '.join(GET_PROPS)}")
    m = ax()
    _, _, entry, el = resolve_ref(ctx.ns, ref)
    node = _live_node(m, el)
    out: dict[str, Any] = {"ref": ref, "property": property}
    if property == "bounds":
        live = node is not None and node.bounds is not None
        b = node.bounds if node is not None and node.bounds is not None else entry.bounds
        out["value"] = {"x": b.x, "y": b.y, "width": b.w, "height": b.h} if b else None
        out["live"] = live
    elif property == "role":
        from cuop.desktop.present import AD_ROLE

        r = Role(entry.role)
        out["value"] = AD_ROLE.get(r, r.value)
    elif property == "states":
        out["value"] = sorted(s.value for s in node.states) if node else []
    elif property == "title":
        out["value"] = entry.name
    elif property == "value":
        out["value"] = node.value if node else None
    else:  # text: 글자 칸은 내용, 그 밖은 이름 — 비면 다른 쪽
        textual = node is not None and node.role in (Role.TEXTBOX, Role.COMBOBOX, Role.DATEFIELD, Role.TIMEFIELD)
        a, b = (node.value, node.name) if node and textual else (node.name, node.value) if node else (entry.name, None)
        out["value"] = a or b
    return out


def is_(ctx: Ctx, ref: str, property: str = "visible") -> dict[str, Any]:  # noqa: A002
    if property not in IS_PROPS:
        raise CuopError("INVALID_ARGS", f"--property must be one of {', '.join(IS_PROPS)}")
    m = ax()
    _, _, _, el = resolve_ref(ctx.ns, ref)
    node = _live_node(m, el)
    out: dict[str, Any] = {"ref": ref, "property": property}
    if node is None:
        return {**out, "result": False, "applicable": False}
    st = {s.value for s in node.states}
    if property == "visible":
        b = node.bounds
        out["result"] = bool(b and b.w > 0 and b.h > 0) and "hidden" not in st and "offscreen" not in st
        out["applicable"] = b is not None
    elif property == "checked":
        out["result"] = "checked" in st or (node.role is Role.TAB and "selected" in st)
        out["applicable"] = node.role in (Role.CHECKBOX, Role.SWITCH, Role.RADIO, Role.TAB, Role.MENUITEM, Role.BUTTON)
    elif property == "expanded":
        out["result"] = "expanded" in st
        out["applicable"] = bool({"expanded", "collapsed"} & st)
    elif property == "enabled":
        out["result"] = "disabled" not in st
        out["applicable"] = True
    else:
        out["result"] = property in st
        out["applicable"] = True
    return out


def _live_node(m: Any, el: Any) -> UIElementNode | None:
    try:
        tree, _ = m.read_tree(el, max_depth=0, timeout_ms=1000)
    except Exception:  # noqa: BLE001 — 읽기 실패 = 저장한 값으로
        return None
    return tree.model_copy(update={"children": []})


# --- 시스템 ---
def permissions(ctx: Ctx, request: bool = False, guide: bool = False) -> dict[str, Any]:
    del ctx
    if request or guide:  # 권한 창 · 설정 창을 띄우는 경로는 사람이 눌러야 한다 → 열지 않는다
        raise CuopError(
            "PLATFORM_NOT_SUPPORTED",
            "cuop never opens permission prompts; grant Accessibility and Screen Recording in System Settings",
        )
    from cuop.collectors import ax as mod

    def st(ok: bool, what: str) -> dict[str, Any]:
        return {"state": "granted"} if ok else {"state": "denied", "suggestion": f"Grant {what} to the launching app"}

    return {
        "accessibility": st(mod.trusted(), "Accessibility"),
        "screen_recording": st(mod.screen_recording(), "Screen Recording"),
        "automation": {"state": "unknown"},  # cuop는 AppleEvent를 쓰지 않는다 — 읽지도 않는다(agent-desktop과 같은 값)
    }


def version(ctx: Ctx) -> dict[str, Any]:
    del ctx
    arch = {"arm64": "aarch64"}.get(
        platform.machine(), platform.machine()
    )  # Rust target_arch 이름(agent-desktop과 같게)
    return {"version": pkg_version("cuop"), "target": arch, "os": "macos"}


def status(ctx: Ctx) -> dict[str, Any]:
    ns = ctx.ns
    latest = ns.latest() or {}
    m = ns.manifest
    return {
        "platform": "macos",
        "version": pkg_version("cuop"),
        "permissions": permissions(ctx),
        "supported_surfaces": list(SURFACES),
        "snapshot_id": latest.get("snapshot_id"),
        "ref_count": latest.get("ref_count"),
        "session_id": ctx.session,
        "tracing": bool(m and m.get("trace") == "on" and not m.get("ended_at")),
        "state_root": str(state_root()),
    }


__all__ = ["Ctx", "find", "get", "is_", "parse_ref", "permissions", "snapshot", "status", "version"]


# --- 목록 ---
def list_apps(ctx: Ctx, app: str | None = None, timeout_ms: int = 5000) -> dict[str, Any]:
    del ctx, timeout_ms
    m = ax()
    rows: list[dict[str, Any]] = []
    for a in m.running_apps():
        if a.policy == 2 and not a.bundle_id:
            continue  # UI 없는 도우미
        if app and app.lower() not in a.name.lower():
            continue
        row: dict[str, Any] = {"name": a.name, "pid": a.pid, "bundle_id": a.bundle_id}
        if a.presentation:
            row["presentation"] = a.presentation
        rows.append(row)
    return {"apps": rows}


def list_windows(ctx: Ctx, app: str | None = None, timeout_ms: int = 5000) -> list[dict[str, Any]]:
    """CG 창 목록(layer 0)을 AX 창과 짝짓는다. AX 요소가 없는 창은 accessible: false (좌표 입력 · 캡처만)."""
    del ctx, timeout_ms
    m = ax()
    apps = {a.pid: a for a in m.running_apps()}
    want = {a.pid for a in apps.values() if a.matches(app)} if app else None
    if app and not want:
        want = {a.pid for a in apps.values() if app.lower() in a.name.lower()}
    cg = m.cg_windows()
    onscreen = {int(w["kCGWindowNumber"]) for w in m.cg_windows(on_screen=True)}
    front = m.frontmost_pid()
    rows: list[dict[str, Any]] = []
    for pid in dict.fromkeys(int(w["kCGWindowOwnerPID"]) for w in cg):
        if want is not None and pid not in want or pid not in apps or not m._permitted(pid):  # pyright: ignore[reportPrivateUsage]
            continue
        ax_wins = {w.id: w for w in m.app_windows(pid)}
        for w in (x for x in cg if int(x["kCGWindowOwnerPID"]) == pid):
            wid = int(w["kCGWindowNumber"])
            aw = ax_wins.get(wid)
            app_name = w.get("kCGWindowOwnerName") or apps[pid].name
            row: dict[str, Any] = {
                "id": f"w-{wid}",
                "title": w.get("kCGWindowName") or (aw.title if aw else None) or app_name,
                "app_name": app_name,
                "pid": pid,
                "is_focused": bool(aw and aw.focused and pid == front),
                "visible": wid in onscreen,
                "accessible": aw is not None,
            }
            b: dict[str, float] = dict(w.get("kCGWindowBounds") or {})
            if {"X", "Y", "Width", "Height"} <= b.keys():
                row["bounds"] = {"x": b["X"], "y": b["Y"], "width": b["Width"], "height": b["Height"]}
            if aw is not None:
                row["minimized"] = aw.minimized
            rows.append(row)
    return rows


def list_displays(ctx: Ctx) -> list[dict[str, Any]]:
    del ctx
    displays: list[dict[str, Any]] = ax().displays()
    ordered = sorted(displays, key=lambda d: (not d["is_primary"], int(d["id"])))
    return [{**d, "id": str(d["id"])} for d in ordered]


def session(
    ctx: Ctx,
    action: str,
    id: str | None = None,  # noqa: A002
    name: str | None = None,
    no_trace: bool = False,
    screenshots: bool = False,
    cursor: bool = False,
    older_than: float | None = None,
    ended: bool = False,
    multi_agent: bool = False,
) -> dict[str, Any]:
    from cuop.desktop import store

    if action not in ("start", "end", "list", "gc"):
        raise CuopError("INVALID_ARGS", "session action must be start, end, list or gc")
    if action == "start":
        # D10: trace는 화면을 찍지 않는다(개인 화면이 남지 않게) — --screenshots는 받되 artifacts는 events
        m = store.start_session(name, trace=not no_trace, artifacts="events", overlay=cursor)
        out = {"session_id": m["id"], "name": m["name"], "trace": m["trace"], "artifacts": m["artifacts"],
               "created_at": m["created_at"], "next": f"export CUOP_SESSION={m['id']}"}  # fmt: skip
        if screenshots:
            out["screenshots"] = "not_recorded"
        if multi_agent:
            out["multi_agent"] = "accepted_only"  # HUD는 세션마다 한 줄 — 에이전트별 색 · 커서는 없다(D10)
        return out
    if action == "end":
        sid = id or ctx.session
        if sid is None:
            raise CuopError("INVALID_ARGS", "session end needs an id (or --session / CUOP_SESSION)")
        m = store.end_session(sid)
        return {"session_id": sid, "ended_at": m["ended_at"]}
    if action == "list":
        return {"sessions": store.list_sessions()}
    return {"removed": store.gc_sessions(ended_only=ended, older_than=older_than)}
