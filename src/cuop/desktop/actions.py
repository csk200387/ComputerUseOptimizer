"""ref 액션 · 키보드 · 마우스 — 정책(headless/headed) · actionability · disposition · 사후 검증.

headless(기본): 접근성 의미 액션만(AXPress · AXValue …), 포커스 · 커서를 건드리지 않는다. 의미 경로가 없으면 실패한다.
--headed: 정확한 창을 앞으로 가져온 뒤 물리 입력(CGEvent)을 먼저 쓴다. 메뉴 막대 항목은 headed여도 AXPress.
결과 `data`: action · disposition · steps[{label, outcome, mechanism}] · post_state · surfaces (agent-desktop 2.6).
"""

import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from cuop.desktop.commands import DIALOGS, Ctx, ax, resolve_ref
from cuop.desktop.errors import CuopError, Delivery, disposition
from cuop.desktop.store import RefEntry
from cuop.schema import Role, State, UIElementNode

POLL = 0.1
TEXT_LIMIT, VALUE_LIMIT = 10_000, 128 * 1024


def _io() -> Any:
    from cuop.collectors import ax_actions

    return ax_actions


@dataclass
class Act:
    """액션 하나의 진행 기록 → data."""

    action: str
    ref: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    delivery: Delivery = "not_delivered"
    extra: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    def step(self, label: str, ok: bool, mechanism: str = "semantic_api", setup: bool = False, **kw: Any) -> None:
        """setup = 준비(창을 앞으로)일 뿐 액션을 보낸 것이 아니다 — 전달 상태를 올리지 않는다(Codex Office 리뷰 #5)."""
        self.steps.append({"label": label, "outcome": "succeeded" if ok else "attempted", "mechanism": mechanism, **kw})
        if ok and not setup and self.delivery == "not_delivered":
            self.delivery = "delivered_unverified"

    def ax(self, err: int | None, label: str) -> bool:
        """AX 쓰기 한 번의 결과. 0 = 전달(True) · 미지원이 증명된 오류(또는 None = 그 경로 없음) = False(다음 수단) ·
        그 밖(-25204 응답 없음 등) = 이미 반영됐을 수 있다 → delivery_uncertain으로 멈춘다(재시도 안전이라 안 함)."""
        if err == 0:
            self.step(label, True)
            return True
        if err is None or err in _io().ABSENT:
            return False
        self.step(label, False)
        self.delivery = "delivery_uncertain"
        raise self.fail("APP_UNRESPONSIVE" if err == -25204 else "ACTION_FAILED",
                        f"{label} returned AXError {err}; the action may have landed — inspect before retrying",
                        kind="mutation_uncertain", ax_error=err)  # fmt: skip

    def data(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action}
        if self.ref:
            out["ref"] = self.ref
        out["disposition"] = disposition(self.delivery)
        if self.steps:
            out["steps"] = self.steps
        return {**out, **self.extra}

    def fail(self, code: Any, message: str, **details: Any) -> CuopError:
        d = {**details, "steps": self.steps} if self.steps else details
        return CuopError(code, message, details=d, delivery=self.delivery)


@dataclass
class Live:
    entry: RefEntry
    el: Any
    node: UIElementNode
    window: Any  # AX 창 요소(없으면 None)


def _read(el: Any) -> UIElementNode | None:
    try:
        tree, _ = ax().read_tree(el, max_depth=0, timeout_ms=1000)
    except Exception:  # noqa: BLE001 — 죽은 요소 등
        return None
    return tree.model_copy(update={"children": []})


def _window_el(entry: RefEntry) -> Any:
    if entry.window_id is None:
        return None
    try:
        return ax().window_by_id(entry.window_id, entry.pid).el
    except CuopError:
        return None


def prepare(ctx: Ctx, ref: str, act: Act, timeout_ms: int, need: str | None = None, editable: bool = False) -> Live:
    """재식별 → actionability(보임 · 활성 · 지원 액션 · 편집 가능)를 timeout까지 폴링. 지원 안 하는 액션은 바로 실패."""
    _, _, entry, el = resolve_ref(ctx.ns, ref)
    _io().guard(entry.pid)
    deadline = time.monotonic() + timeout_ms / 1000
    tried_scroll = False
    while True:
        node = _read(el)
        if node is None:
            raise act.fail("STALE_REF", f"{ref} no longer answers", kind="retained_object_stale", reason="dead")
        checks = _checks(node, need, editable)
        terminal = [c for c in checks if c["status"] == "fail" and c["check"] in ("supported_action", "editable")]
        if terminal:
            raise act.fail("ACTION_NOT_SUPPORTED", f"{ref} ({node.role.value}) does not support {act.action}",
                           actionable=False, checks=checks)  # fmt: skip
        if all(c["status"] != "fail" for c in checks):
            return Live(entry, el, node, _window_el(entry))
        if (
            not tried_scroll
            and "ScrollTo" in node.actions
            and any(c["check"] == "visible" and c["status"] == "fail" for c in checks)
        ):
            tried_scroll = True  # 한 번만 — 실패해도 다시 시도하며 마감을 넘기지 않게
            act.step("scroll_into_view", _io().perform(el, "AXScrollToVisible") == 0, setup=True)
            continue
        if time.monotonic() > deadline or timeout_ms == 0:
            report = {"actionable": False, "checks": checks}
            raise act.fail("TIMEOUT", f"{ref} did not become actionable within {timeout_ms} ms",
                           kind="actionability_timeout", last_report=report)  # fmt: skip
        time.sleep(POLL)


def _checks(node: UIElementNode, need: str | None, editable: bool) -> list[dict[str, str]]:
    b = node.bounds
    # 메뉴 항목은 닫힌 메뉴(크기 0)에서도 AXPress로 실행된다(2026-11-03 확인) — 메뉴를 열어 둘 필요가 없다
    # 코드 편집기(Monaco · CodeMirror)의 입력 칸은 폭 0 · 캐럿 높이의 숨은 textarea다 — 높이가 있으면 보이는 것으로
    caret = node.role is Role.TEXTBOX and b is not None and b.h > 0
    shown = b is None or (b.w > 0 and b.h > 0) or caret or node.raw_role.startswith("AXMenuItem")
    out = [
        {"check": "visible", "status": "pass" if shown else "fail"},
        {"check": "enabled", "status": "fail" if State.DISABLED in node.states else "pass"},
    ]
    if need is not None:
        out.append({"check": "supported_action", "status": "pass" if need in node.actions else "fail"})
    if editable:
        ok = (
            node.role in (Role.TEXTBOX, Role.COMBOBOX, Role.DATEFIELD, Role.TIMEFIELD, Role.SLIDER)
            or "SetValue" in node.actions
        )
        out.append({"check": "editable", "status": "pass" if ok else "fail"})
    return out


def _center(node: UIElementNode) -> tuple[float, float]:
    if node.bounds is None:
        raise CuopError("ACTION_FAILED", "element has no bounds for pointer input", details={"kind": "no_bounds"})
    b = node.bounds
    return b.x + b.w / 2, b.y + b.h / 2


FRONT_UNCONFIRMED = "could not confirm the target window is frontmost"


def modal_focus(pid: int, window: Any) -> Any:
    """같은 앱의 대화상자 창(AXDialog · AXSystemDialog)이 그 창 대신 포커스를 쥐고 있으면 그 대화상자."""
    m = ax()
    fw = m.attr(m.app_element(pid), "AXFocusedWindow")
    return fw if fw is not None and fw != window and m.attr(fw, "AXSubrole") in DIALOGS else None


def not_front(
    act: Act, pid: int, window: Any, code: str = "TIMEOUT", message: str = FRONT_UNCONFIRMED, **extra: Any
) -> CuopError:
    """창을 앞으로 못 가져왔을 때의 에러. 같은 앱의 모달 대화상자(Excel '이동' · 경고)가 포커스를 쥐고 있으면
    그 창을 알려 준다 — 대화상자는 문서 창의 자식이 아니라 앱의 다른 창이라 문서 창 트리만 보면 모른다
    (Office 본 실험 C회차)."""
    m = ax()
    if (fw := modal_focus(pid, window)) is not None:
        fid = m.window_number(fw)
        where = f"snapshot --window-id w-{fid} (or --surface alert)" if fid is not None else "snapshot --surface alert"
        message = f"a dialog of this app holds focus; read it with {where} and close it first"
        extra["focused_window"] = f"w-{fid}" if fid is not None else "alert"
    return act.fail(code, message, kind="focus_verification", **extra)


def _headed_focus(live: Live, act: Act) -> None:
    ok = _io().raise_window(live.window, live.entry.pid)
    act.step("focus_window", ok, "semantic_api", setup=True)
    if not ok:
        raise not_front(act, live.entry.pid, live.window, physical_delivery_started=False)


def _has_focus(live: Live) -> bool:
    """그 요소가 키보드 포커스인가: 자신이 AXFocused이거나 앱의 포커스 요소가 바로 그것. 예외 하나 —
    콤보박스는 자식으로 안 보이는 필드 편집기(AXTextField)가 포커스를 받고 자신은 AXFocused=false로 남는다(Excel 이름
    상자) → 대상이 콤보박스이고 포커스 요소가 그 바로 아래 글자 칸일 때만 인정한다(Codex Office 리뷰 #2)."""
    n = _read(live.el)
    if n is not None and State.FOCUSED in n.states:
        return True
    m = ax()
    el = m.attr(m.app_element(live.entry.pid), "AXFocusedUIElement")
    if el is None:
        return False
    if el == live.el:
        return True
    return (
        live.node.role is Role.COMBOBOX
        and m.attr(el, "AXRole") in ("AXTextField", "AXTextArea")
        and m.attr(el, "AXParent") == live.el
    )


POWERPOINT = "com.microsoft.Powerpoint"


def _slide_editor(live: Live) -> tuple[Any, Any] | None:
    """PowerPoint 슬라이드 편집 영역 = (스크롤 영역, 안의 레이아웃 영역). 트리에 도형이 없고 키는 스크롤 영역이 받는다:
    제목을 눌러 편집해도 축소판을 먼저 눌렀다면 키가 버려졌다 — AX 포커스는 레이아웃 영역이라고 하면서. 스크롤 영역에
    AXFocused를 주면 편집 중인 개체 틀로 들어간다(Office 시험) → 키 대상은 언제나 스크롤 영역. PowerPoint일 때만."""
    raw = live.node.raw_role
    if not raw.startswith(("AXLayoutArea", "AXScrollArea")):
        return None
    m = ax()
    app = m.app_by_pid(live.entry.pid)
    if app is None or app.bundle_id != POWERPOINT:
        return None
    if raw.startswith("AXLayoutArea"):
        # AXChildren으로는 스크롤 영역 바로 아래인데 AXParent는 숨은 AXGroup을 거친다(D회차 실패: layoutitem ref로
        # 쳐서 키가 버려졌다) → 몇 단계 올라가 찾고, 그 스크롤 영역의 자식에 이 영역이 정말 있을 때만 쌍으로 본다
        up = live.el
        for _ in range(3):
            up = m.attr(up, "AXParent")
            if up is None or m.attr(up, "AXRole") == "AXScrollArea":
                break
        if up is None or m.attr(up, "AXRole") != "AXScrollArea":
            return None
        return (up, live.el) if live.el in list(m.attr(up, "AXChildren") or []) else None
    kids: list[Any] = list(m.attr(live.el, "AXChildren") or [])
    inner: Any = next((k for k in kids if m.attr(k, "AXRole") == "AXLayoutArea"), None)
    return (live.el, inner) if inner is not None else None


def _focused_here(live: Live, focused: Any) -> bool:
    """키 입력 조각 사이 확인: 대상 앱이 아직 맨 앞이고, 그 창이 앱의 포커스 창이고, 그 요소가 아직 포커스."""
    m = ax()
    if live.entry.window_id is not None and live.window is None:  # 저장된 창을 다시 못 찾으면 확인 불가 → 멈춘다
        return False
    if live.window is not None and m.attr(m.app_element(live.entry.pid), "AXFocusedWindow") != live.window:
        return False
    return focused() and m.is_frontmost(live.entry.pid)


def _aim(live: Live, act: Act, strict: bool = False) -> tuple[float, float]:
    x, y, _ = _aim_at(live, act, strict)
    return x, y


def _aim_at(live: Live, act: Act, strict: bool = False) -> tuple[float, float, int | None]:
    """앞으로 가져온 뒤의 좌표: bounds를 다시 읽고(창이 움직였을 수 있다) 그 점의 요소가 대상이거나 대상의 자손인지
    AX hit test로 확인한다. 다른 창 · 다른 요소가 덮고 있으면 누르지 않는다(감사 #2). 셋째 값은 hit test 전에 본
    그 점의 맨 앞 창 — 포인터 헬퍼가 보내기 직전에 같은지 다시 본다(hit test 뒤 창이 바뀌면 보내지 않게, Codex)."""
    now = _read(live.el)
    if now is None:
        raise act.fail("STALE_REF", "the target disappeared while coming forward", kind="retained_object_stale",
                       physical_delivery_started=False)  # fmt: skip
    x, y = _center(now)
    wid = _io().window_at(x, y)
    m, (hit, pid) = ax(), _io().element_at(x, y)
    for _ in range(20):  # 앞으로 가져온 직후엔 화면 합성이 늦어 이전 창이 맞을 수 있다 — 1초까지 다시 본다
        if pid is None or pid == live.entry.pid:
            break
        time.sleep(0.05)
        hit, pid = _io().element_at(x, y)
    if pid is not None and pid != live.entry.pid:
        raise act.fail("ACTION_FAILED", "another app's window covers the target point", kind="occluded",
                       physical_delivery_started=False, point={"x": x, "y": y})  # fmt: skip
    if hit is None and (strict or _io().owner_at(x, y) != live.entry.pid):  # hit test가 없으면 창 주인만이라도
        raise act.fail("ACTION_FAILED", "cannot confirm what is under the target point", kind="hit_test_unavailable",
                       physical_delivery_started=False, point={"x": x, "y": y})  # fmt: skip
    el, depth = hit, 0
    while el is not None and depth < 40 and el != live.el:
        el, depth = m.attr(el, "AXParent"), depth + 1
    if hit is not None and el != live.el:  # 위로 따라가도 대상이 아니다(또는 너무 깊다)
        raise act.fail("ACTION_FAILED", "the point hits a different element than the target", kind="target_mismatch",
                       physical_delivery_started=False, point={"x": x, "y": y})  # fmt: skip
    if _io().window_at(x, y) != wid:  # hit test 사이에 맨 앞 창이 바뀌었다
        raise act.fail("ACTION_FAILED", "the window under the target point changed; nothing was clicked",
                       kind="focus_verification", physical_delivery_started=False)  # fmt: skip
    return x, y, wid


def _finish(act: Act, live: Live) -> dict[str, Any]:
    from cuop.desktop.present import AD_ROLE

    after = _read(live.el)
    if after is not None:
        role = AD_ROLE.get(after.role, after.role.value)
        ps: dict[str, Any] = {"role": role, "states": sorted(s.value for s in after.states)}
        if after.value is not None and State.SECURE not in after.states:
            ps["value"] = after.value
        act.extra["post_state"] = ps
    surfaces = _io().open_surfaces(live.entry.pid, live.window)
    if surfaces:
        act.extra["surfaces"] = [{"id": k, "type": k} for k in surfaces]  # agent-desktop SurfaceInfo 모양
    return act.data()


def _menu_item(live: Live) -> bool:
    return live.entry.raw_role.startswith(("AXMenuBarItem", "AXMenuItem"))


# --- click 계열 ---
def click(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    act = Act("click", ref)
    if ctx.headed:  # 메뉴 명령은 활성 창이 대상 — 백그라운드에선 disabled라 actionability 전에 앱을 앞으로
        _, _, entry, _ = resolve_ref(ctx.ns, ref)
        if entry.raw_role.startswith(("AXMenuBarItem", "AXMenuItem")):
            ok = _io().raise_window(None, entry.pid)
            act.step("focus_app", ok)
            if not ok:
                raise act.fail("TIMEOUT", "could not bring the app to the front for its menu",
                               kind="focus_verification", physical_delivery_started=False)  # fmt: skip
    live = prepare(ctx, ref, act, timeout_ms)
    io = _io()
    if ctx.headed and not _menu_item(live):
        _headed_focus(live, act)
        x, y, wid = _aim_at(live, act)
        io.mouse_click(x, y, expect=live.entry.pid, expect_window=wid)
        act.step("pointer_click", True, "physical_synthetic")
        return _finish(act, live)
    for action in ("AXPress", "AXOpen", "AXConfirm", "AXPick"):
        if act.ax(io.perform(live.el, action), action.lower().replace("ax", "ax_", 1)):
            break
    else:  # 목록 행 · 셀은 고르는 것이 활성화
        if not act.ax(io.set_attr(live.el, "AXSelected", True), "ax_selected"):
            raise act.fail("ACTION_NOT_SUPPORTED", f"{ref} publishes no activation; for a text field use focus",
                           kind="no_semantic_activation")  # fmt: skip
    if live.entry.raw_role.startswith("AXMenuItem"):  # 메뉴는 누른 뒤 닫히는 데 ~0.4초
        _wait(lambda: not io.open_surfaces(live.entry.pid, None), 1.0)
    return _finish(act, live)


def _physical_clicks(ctx: Ctx, ref: str, name: str, count: int, button: str, timeout_ms: int) -> dict[str, Any]:
    act = Act(name, ref)
    if not ctx.headed:
        raise CuopError(
            "POLICY_DENIED", f"{name} is a physical gesture; pass --headed", details={"kind": "headed_required"}
        )
    live = prepare(ctx, ref, act, timeout_ms)
    _headed_focus(live, act)
    x, y, wid = _aim_at(live, act)
    _io().mouse_click(x, y, button=button, count=count, expect=live.entry.pid, expect_window=wid)
    act.step("pointer_click", True, "physical_synthetic", count=count)
    return _finish(act, live)


def double_click(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    return _physical_clicks(ctx, ref, "double-click", 2, "left", timeout_ms)


def triple_click(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    return _physical_clicks(ctx, ref, "triple-click", 3, "left", timeout_ms)


def right_click(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    if ctx.headed:
        return _physical_clicks(ctx, ref, "right-click", 1, "right", timeout_ms)
    act = Act("right-click", ref)
    live = prepare(ctx, ref, act, timeout_ms)
    # -25204 = 메뉴가 열린 채 modal 추적에 들어가 응답을 못 함 — 열렸을 수 있다 → act.ax가 uncertain으로 멈춘다
    if not act.ax(_io().perform(live.el, "AXShowMenu"), "ax_show_menu"):
        raise act.fail("POLICY_DENIED", f"{ref} has no AXShowMenu; a physical right-click needs --headed")
    return _finish(act, live)


def _wait(done: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.05)
    return bool(done())


# --- 글자 ---
def _text_arg(text: str | None, stdin: bool, limit: int) -> str:
    if stdin:
        if text is not None:
            raise CuopError("INVALID_ARGS", "--stdin cannot combine with a command-line value")
        if sys.stdin.isatty():
            raise CuopError("INVALID_ARGS", "--stdin needs a pipe or redirected file, not a terminal")
        raw = sys.stdin.buffer.read(limit + 1)
    else:
        if text is None:
            raise CuopError("INVALID_ARGS", "give the text or --stdin")
        raw = text.encode()
    if len(raw) > limit:
        raise CuopError("INVALID_ARGS", f"input exceeds {limit} bytes")
    if b"\0" in raw:
        raise CuopError("INVALID_ARGS", "input must not contain NUL bytes")
    try:
        return raw.decode()
    except UnicodeDecodeError:
        raise CuopError("INVALID_ARGS", "input must be valid UTF-8") from None


def _redact(data: dict[str, Any], stdin: bool, secure: bool) -> dict[str, Any]:
    ps: Any = data.get("post_state")
    if (stdin or secure) and isinstance(ps, dict):
        ps.pop("value", None)  # pyright: ignore[reportUnknownMemberType]
    return data


def type_(ctx: Ctx, ref: str, text: str | None = None, stdin: bool = False, interval_ms: int | None = None,
          timeout_ms: int | None = None) -> dict[str, Any]:  # fmt: skip
    """글자를 지금 커서 자리에 넣는다(선택 영역은 대체). headless = AXSelectedText, headed = 키 입력."""
    body = _text_arg(text, stdin, TEXT_LIMIT)
    if interval_ms is not None and not 1 <= interval_ms <= 2000:
        raise CuopError("INVALID_ARGS", "--interval-ms must be 1-2000")
    act = Act("type", ref)
    # headed는 포커스 확인이 지킨다 → 편집 가능 role이 아니어도(Office 문서 영역: PowerPoint 슬라이드 · Excel 격자는
    # 트리에 글자 요소가 없고 포커스만 받는다) 키를 보낸다. headless는 의미 경로가 있는 입력 칸만(D15)
    timeout = timeout_ms if timeout_ms is not None else 5000 + 30 * len(body)
    live = prepare(ctx, ref, act, timeout, editable=not ctx.headed)
    io = _io()
    before = live.node.value or ""
    secure = State.SECURE in live.node.states
    if ctx.headed:
        _headed_focus(live, act)
        if live.entry.window_id is not None and live.window is None:
            raise act.fail("STALE_REF", f"{ref}'s window could not be found again; nothing typed",
                           kind="window_unresolved", physical_delivery_started=False)  # fmt: skip
        pair, clicked = _slide_editor(live), False

        def focused() -> bool:
            if pair is None:
                return _has_focus(live)
            m = ax()
            return m.attr(m.app_element(live.entry.pid), "AXFocusedUIElement") == pair[0]  # 정확히 스크롤 영역

        if not focused():  # 이미 포커스면 건드리지 않는다 — 콤보박스에 다시 주면 포커스가 창으로 빠진다
            io.set_attr(pair[0] if pair else live.el, "AXFocused", True)
            if pair is not None and _wait(focused, 1.0):
                time.sleep(0.6)  # PowerPoint는 포커스를 받은 뒤 편집 중인 개체 틀로 키를 보내기까지 조금 걸린다
            if not _wait(focused, 1.0):  # 의미 포커스를 무시하는 칸(Excel 이름 상자)은 한 번 눌러서
                x, y, wid = _aim_at(live, act, strict=True)  # 대상(또는 자손)임이 AX로 확인된 점만
                io.mouse_click(x, y, expect=live.entry.pid, expect_window=wid)
                act.step("pointer_focus", True, "physical_synthetic")  # 눌렀다 = 부작용 가능 → 재시도 unsafe
                clicked = True
        if not _wait(focused, 1.0):
            msg = f"{ref} did not take keyboard focus; nothing typed"
            raise act.fail("ACTION_FAILED", msg, kind="focus_verification", physical_delivery_started=clicked)
        n, total = io.type_unicode(body, live.entry.pid, interval_ms or 0, lambda: _focused_here(live, focused))
        act.step("keyboard_text", n > 0, "physical_synthetic")
        if n < total:
            raise act.fail("ACTION_FAILED", "focus left the target while typing; stopped", kind="focus_lost",
                           typed_keystrokes=n, keystrokes=total)  # fmt: skip
        if interval_ms:
            act.extra.update(keystrokes=n, interval_ms=interval_ms)
    else:
        chunks = [body] if not interval_ms else list(body)
        raw_before = ax().attr(live.el, "AXValue")
        for i, chunk in enumerate(chunks):
            err = io.set_attr(live.el, "AXSelectedText", chunk)
            if err and i == 0 and err in io.ABSENT:
                # 빈 칸에 한 번에: 값 쓰기 = 같은 결과. 그 밖은 의미 경로가 없다(다른 글자를 덮어쓰지 않게)
                if raw_before == "" and len(chunks) == 1 and act.ax(io.set_attr(live.el, "AXValue", body),
                                                                    "ax_set_value"):  # fmt: skip
                    break
                msg = f"{ref} has no insert-at-selection; use set-value or --headed"
                raise act.fail("ACTION_NOT_SUPPORTED", msg, kind="no_semantic_insert")
            if err and i == 0:
                act.ax(err, "ax_insert_text")  # 반영됐을 수 있다 → uncertain으로 멈춘다
            if err:  # 앞 조각은 이미 들어갔다
                raise act.fail("ACTION_FAILED", "typing stopped part way", typed_keystrokes=i, keystrokes=len(chunks),
                               ax_error=err)  # fmt: skip
            if i == 0:
                act.step("ax_insert_text", True)  # 첫 글자부터 전달됨으로 기록 — 실패해도 재시도가 안전하다고 하지 않게
            if interval_ms:
                time.sleep(interval_ms / 1000)
        if interval_ms:
            act.extra.update(keystrokes=len(chunks), interval_ms=interval_ms)
    after = _read(live.el)
    if after is not None and not secure and body and live.node.value is not None and after.value == before:
        latest, reads = (
            [after],
            [0],
        )  # 늦게 반영되는 앱 — 잠깐 다시 읽고, 마지막으로 읽힌 값으로 판정한다(Codex 확인 P3)

        def moved() -> bool:
            n = _read(live.el)
            if n is not None:
                latest[0], reads[0] = n, reads[0] + 1
            return n is not None and n.value != before

        _wait(moved, 0.5)
        after = latest[0] if reads[0] else None  # 다시 읽기가 모두 실패하면 '그대로'라고 하지 않는다
    expect = before + body if ctx.headed is False and before and live.node.value is not None else None
    if (
        after is not None
        and not secure
        and after.value is not None
        and body
        and (
            after.value == expect
            or (expect is None and after.value.endswith(body) and len(after.value) >= len(before) + len(body))
        )
    ):
        act.delivery = "delivered_verified"
        act.extra["details"] = {"verification_scope": "element_value", "application_commit": "not_verified"}
    else:
        reason = "secure_field" if secure else "insufficient_evidence"
        if not secure and body and live.node.value is not None and after is not None and after.value == before:
            reason = (
                "value_unchanged"  # 앱이 0을 돌려주고 무시한 쓰기(Word 본문의 AXSelectedText) → --headed 등 다른 방법
            )
        act.extra["details"] = {"verification_scope": "unavailable", "verification_reason": reason}
    return _redact(_finish(act, live), stdin, secure)


def set_value(
    ctx: Ctx, ref: str, value: str | None = None, stdin: bool = False, timeout_ms: int = 5000
) -> dict[str, Any]:
    body = _text_arg(value, stdin, VALUE_LIMIT)
    act = Act("set-value", ref)
    live = prepare(ctx, ref, act, timeout_ms, editable=True)
    secure = State.SECURE in live.node.states
    return _redact(_write_value(act, live, body, redact=stdin or secure), stdin, secure)


def clear(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    act = Act("clear", ref)
    return _write_value(act, prepare(ctx, ref, act, timeout_ms, editable=True), "")


def _write_value(act: Act, live: Live, body: str, redact: bool = False) -> dict[str, Any]:
    io = _io()
    target: Any = body
    if live.node.role in (Role.SLIDER, Role.INCREMENTOR):
        try:
            target = float(body)
        except ValueError:
            raise CuopError("INVALID_ARGS", f"{live.node.role.value} needs a number") from None
    if not act.ax(io.set_attr(live.el, "AXValue", target), "ax_set_value"):
        raise act.fail("ACTION_NOT_SUPPORTED", f"{act.ref} does not accept a value write", kind="value_not_settable")
    if State.SECURE in live.node.states:
        act.extra["details"] = {"verification_scope": "unavailable", "verification_reason": "secure_field"}
        return _finish(act, live)
    numeric = live.node.role in (Role.SLIDER, Role.INCREMENTOR)
    got = _poll_value(live.el, body, numeric)
    if got is None:
        act.extra["details"] = {"verification_scope": "unavailable", "verification_reason": "insufficient_evidence"}
    elif _same(got, body, numeric):
        act.delivery = "delivered_verified"
        act.extra["details"] = {"verification_scope": "element_value", "application_commit": "not_verified"}
    elif got.strip() == body.strip():  # 앱이 앞뒤 공백을 다듬었다 — 확인했다고 하지 않는다
        act.extra["details"] = {"verification_scope": "unavailable", "verification_reason": "normalized_by_app"}
    else:
        shown = {} if redact else {"post_state": {"value": got}}  # --stdin · 비밀 칸은 실패해도 값을 드러내지 않는다
        raise act.fail("ACTION_FAILED", "the element's value differs from what was written",
                       kind="post_action_verification", **shown)  # fmt: skip
    return _finish(act, live)


def _same(got: str, want: str, numeric: bool = False) -> bool:
    if got == want:
        return True
    if not numeric:
        return False
    try:
        return abs(float(got) - float(want)) < 1e-6
    except ValueError:
        return False


def _poll_value(el: Any, want: str, numeric: bool = False, timeout: float = 1.0) -> str | None:
    """AXValue 원문을 다시 읽는다(빈 값과 읽기 실패를 구분). 읽지 못하면 None."""
    m = ax()
    deadline, got = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        raw = m.attr(el, "AXValue")
        got = None if raw is None else raw if isinstance(raw, str) else m.value_text(raw)
        if got is not None and _same(got, want, numeric):
            return got
        time.sleep(0.05)
    return got


def focus(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    act = Act("focus", ref)
    live = prepare(ctx, ref, act, timeout_ms)
    if not act.ax(_io().set_attr(live.el, "AXFocused", True), "ax_focus"):
        raise act.fail("ACTION_NOT_SUPPORTED", f"{ref} does not accept focus")
    if _wait(lambda: (n := _read(live.el)) is not None and State.FOCUSED in n.states, 0.5):
        act.delivery = "delivered_verified"
    return _finish(act, live)


# --- 상태 ---
TOGGLE_BUTTONS = ("AXButton/AXToggle", "AXButton/AXToggleButton")


def _checked(live: Live) -> bool | None:
    """지금 켜졌나 — 상태를 실제로 읽었을 때만 True/False, 못 읽거나 혼합이면 None(꺼짐으로 치지 않는다)."""
    if live.node.role is Role.TAB:
        sel = ax().attr(live.el, "AXSelected")
        if isinstance(sel, bool):
            return sel
        raw_tab = ax().attr(live.el, "AXValue")  # 탭 버튼(AXRadioButton)은 AXValue 0/1
        return raw_tab == 1 if isinstance(raw_tab, int) and not isinstance(raw_tab, bool) else None
    raw = ax().attr(live.el, "AXValue")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return None if raw == 2 else raw == 1
    return None


def _set_checked(ctx: Ctx, ref: str, name: str, want: bool | None, timeout_ms: int) -> dict[str, Any]:
    act = Act(name, ref)
    live = prepare(ctx, ref, act, timeout_ms)
    toggle_button = live.entry.raw_role in TOGGLE_BUTTONS
    if live.node.role not in (Role.CHECKBOX, Role.SWITCH, Role.RADIO, Role.TAB) and not toggle_button:
        raise act.fail("ACTION_NOT_SUPPORTED", f"{ref} ({live.node.role.value}) has no checked state; use click")
    before = _checked(live)
    if before is None:  # 모르는 상태에서 누르면 원하는 쪽으로 갔는지 보장할 수 없다
        raise act.fail("ACTION_NOT_SUPPORTED", f"{name} needs a readable checked state (it is mixed or unreadable)",
                       kind="state_unreadable")  # fmt: skip
    target = (not before) if want is None else want
    if before == target:
        act.extra["already"] = True  # 이미 그 상태 — 아무것도 보내지 않았다
        return _finish(act, live)
    if not act.ax(_io().perform(live.el, "AXPress"), "ax_press"):  # 탭 버튼도 press(AXValue 쓰기는 Chromium을 죽인다)
        raise act.fail("ACTION_NOT_SUPPORTED", f"{ref} has no AXPress")
    if _wait(lambda: _checked(live) == target, 1.0):
        act.delivery = "delivered_verified"
        return _finish(act, live)
    raise act.fail("ACTION_FAILED", f"state did not become {'checked' if target else 'unchecked'}",
                   kind="post_action_verification", post_state={"checked": _checked(live)})  # fmt: skip


def toggle(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    return _set_checked(ctx, ref, "toggle", None, timeout_ms)


def check(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    return _set_checked(ctx, ref, "check", True, timeout_ms)


def uncheck(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    return _set_checked(ctx, ref, "uncheck", False, timeout_ms)


def _expanded(n: UIElementNode | None) -> bool | None:
    if n is None:
        return None
    return True if State.EXPANDED in n.states else False if State.COLLAPSED in n.states else None


def _set_expanded(ctx: Ctx, ref: str, name: str, want: bool, timeout_ms: int) -> dict[str, Any]:
    act = Act(name, ref)
    live = prepare(ctx, ref, act, timeout_ms)
    if _expanded(live.node) == want:
        act.extra["already"] = True
        return _finish(act, live)
    io = _io()
    if not act.ax(io.set_attr(live.el, "AXExpanded", want), "ax_set_expanded") and not act.ax(
        io.perform(live.el, "AXPress"),
        "ax_press",  # 디스클로저 삼각형 · 아코디언
    ):
        raise act.fail("ACTION_NOT_SUPPORTED", f"{ref} cannot {name}")
    if _wait(lambda: _expanded(_read(live.el)) == want, 1.0):
        act.delivery = "delivered_verified"
    elif _expanded(_read(live.el)) == (not want):  # 읽히는데 반대 상태 = 모순
        raise act.fail("ACTION_FAILED", f"still {'collapsed' if want else 'expanded'}", kind="post_action_verification")
    return _finish(act, live)


def expand(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    return _set_expanded(ctx, ref, "expand", True, timeout_ms)


def collapse(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    return _set_expanded(ctx, ref, "collapse", False, timeout_ms)


# --- select ---
def select(ctx: Ctx, ref: str, value: str, timeout_ms: int = 5000) -> dict[str, Any]:
    """목록 · 드롭다운 · 콤보박스에서 표시 글자로 고른다. 메뉴형은 열고 → 항목 → 값 확인."""
    act = Act("select", ref)
    live = prepare(ctx, ref, act, timeout_ms)
    io, m = _io(), ax()
    raw = live.entry.raw_role.split("/", 1)[0]
    want = value.strip()
    if raw in ("AXPopUpButton", "AXComboBox", "AXMenuButton"):
        io.raise_window(live.window, live.entry.pid) if ctx.headed else None
        item = _find_label(m, live.el, want)
        if item is None:
            opener = live.el
            if raw == "AXComboBox":
                opener = next(
                    (c for c in m.attr(live.el, "AXChildren") or () if m.attr(c, "AXRole") == "AXButton"), live.el
                )
            act.ax(io.perform(opener, "AXPress"), "open_menu")
            _wait(lambda: _find_label(m, live.el, want) is not None, 1.5)
            item = _find_label(m, live.el, want)
        if item is None and raw == "AXComboBox":
            io.set_attr(live.el, "AXExpanded", False)
            if not act.ax(io.set_attr(live.el, "AXValue", want), "ax_set_value"):
                raise act.fail("ELEMENT_NOT_FOUND", f"no option {value!r}", kind="option_absent")
            act.ax(io.perform(live.el, "AXConfirm"), "ax_confirm")
        elif item is None:  # 연 목록이 열린 채일 수 있다 — Escape를 보내지 않는다(다른 overlay를 닫을 수 있어)
            raise act.fail("ELEMENT_NOT_FOUND", f"no option {value!r}", kind="option_absent", menu_may_be_open=True)
        else:
            # 메뉴 항목은 AXPress, 콤보박스 목록 행은 AXSelected(AXPress가 0을 돌려줘도 반영되지 않았다, 2026-11-21)
            if m.attr(item, "AXRole") == "AXMenuItem":
                ok = act.ax(io.perform(item, "AXPress"), "choose_option")
            else:
                ok = act.ax(io.set_attr(item, "AXSelected", True), "choose_option") or act.ax(
                    io.perform(item, "AXPress"), "choose_option"
                )
            if raw == "AXComboBox":
                io.set_attr(live.el, "AXExpanded", False)
            if not ok:
                raise act.fail("ACTION_NOT_SUPPORTED", f"option {value!r} cannot be chosen", kind="option_inert")
        if _wait(lambda: (n := _read(live.el)) is not None and (n.value or n.name or "").strip() == want, 1.5):
            act.delivery = "delivered_verified"
            return _finish(act, live)
        now = _read(live.el)
        if now is not None and (now.value or now.name):
            hint = "" if ctx.headed else "; a background app may not open its list headless, try --headed"
            raise act.fail("ACTION_FAILED", f"value is {now.value or now.name!r}, not {value!r}{hint}",
                           kind="post_action_verification", post_state={"value": now.value})  # fmt: skip
        return _finish(act, live)
    item = _find_label(m, live.el, want, rows=True)  # 목록 · 표 · 개요: 행 고르기
    if item is None:
        raise act.fail("ELEMENT_NOT_FOUND", f"no item {value!r} in {ref}", kind="option_absent")
    if not act.ax(io.set_attr(item, "AXSelected", True), "ax_select_row") and not act.ax(
        io.perform(item, "AXPress"), "ax_press"
    ):
        raise act.fail("ACTION_NOT_SUPPORTED", f"{value!r} in {ref} cannot be selected", kind="option_inert")
    if _wait(lambda: m.attr(item, "AXSelected") is True, 1.0):
        act.delivery = "delivered_verified"
    return _finish(act, live)


def _find_label(m: Any, root: Any, want: str, rows: bool = False, limit: int = 3000) -> Any:
    """root 아래 너비 우선으로 이름(제목 · 설명 · 값)이 want인 요소. rows면 그 요소를 품은 행을 돌려준다."""
    queue = [(root, None)]
    while queue and limit > 0:
        el, row = queue.pop(0)
        limit -= 1
        for c in m.attr(el, "AXChildren") or ():
            role = m.attr(c, "AXRole")
            r = c if role in ("AXRow", "AXCell", "AXOutlineRow") and row is None else row
            labels = [m.text(m.attr(c, k)) for k in ("AXTitle", "AXDescription", "AXValue")]
            if want in labels:
                return (r or c) if rows else c
            queue.append((c, r))
    return None


# --- scroll ---
def scroll(ctx: Ctx, ref: str, direction: str = "down", amount: int = 3, timeout_ms: int = 5000) -> dict[str, Any]:
    if direction not in ("up", "down", "left", "right"):
        raise CuopError("INVALID_ARGS", "--direction must be up, down, left or right")
    if not 1 <= amount <= 1000:
        raise CuopError("INVALID_ARGS", "--amount must be 1-1000")
    act = Act("scroll", ref)
    live = prepare(ctx, ref, act, timeout_ms)
    io, m = _io(), ax()
    area = live.el if live.entry.raw_role.startswith("AXScrollArea") else _ancestor(m, live.el, "AXScrollArea")
    if ctx.headed:
        _headed_focus(live, act)
        x, y = _center(_read(area) or live.node) if area is not None else _center(live.node)
        dy = {"up": amount, "down": -amount}.get(direction, 0)
        dx = {"left": amount, "right": -amount}.get(direction, 0)
        io.mouse_wheel(x, y, dx, dy)
        act.step("pointer_wheel", True, "physical_synthetic")
        return _finish(act, live)
    if area is None:
        raise act.fail("ACTION_FAILED", f"{ref} has no scrollable ancestor", kind="scroll_into_view_unsupported",
                       retryable=False)  # fmt: skip
    vbar, hbar = io.scrollbars(area)
    bar = vbar if direction in ("up", "down") else hbar
    before = io.number(m.attr(bar, "AXValue")) if bar is not None else None
    if bar is None or before is None:
        names = {"up": "AXScrollUpByPage", "down": "AXScrollDownByPage", "left": "AXScrollLeftByPage",
                 "right": "AXScrollRightByPage"}  # fmt: skip
        pages = amount  # agent-desktop과 같게: 쪽 단위 수단이면 --amount만큼 쪽을 넘긴다
        for _ in range(pages):
            if not act.ax(io.perform(area, names[direction]), "ax_scroll_by_page"):
                raise act.fail("ACTION_NOT_SUPPORTED", "no headless scroll mechanism; use --headed",
                               kind="no_scroll_path")  # fmt: skip
        act.extra["pages"] = pages
        return _finish(act, live)
    sign = 1 if direction in ("down", "right") else -1
    target = min(
        1.0, max(0.0, before + sign * 0.05 * amount)
    )  # ponytail: 1 단위 = 문서의 5%, 줄 단위가 필요하면 바꾼다
    if not act.ax(io.set_attr(bar, "AXValue", target), "ax_scrollbar_value"):
        raise act.fail("ACTION_NOT_SUPPORTED", "the scroll bar does not accept a value; use --headed",
                       kind="no_scroll_path")  # fmt: skip
    after = io.number(m.attr(bar, "AXValue"))
    if after is not None and abs(after - before) > 1e-6:
        act.delivery = "delivered_verified"
    act.extra["post_state"] = {"position": after}
    return _finish(act, live)


def _ancestor(m: Any, el: Any, role: str, depth: int = 12) -> Any:
    for _ in range(depth):
        el = m.attr(el, "AXParent")
        if el is None:
            return None
        if m.attr(el, "AXRole") == role:
            return el
    return None


def scroll_to(ctx: Ctx, ref: str, timeout_ms: int = 5000) -> dict[str, Any]:
    act = Act("scroll-to", ref)
    _, _, entry, el = resolve_ref(ctx.ns, ref)
    _io().guard(entry.pid)
    if not act.ax(_io().perform(el, "AXScrollToVisible"), "ax_scroll_to_visible"):
        raise act.fail("ACTION_NOT_SUPPORTED", f"{ref} has no AXScrollToVisible", kind="scroll_into_view_unsupported")
    live = Live(entry, el, _read(el) or UIElementNode(id="?", role=Role.GENERIC, raw_role="?"), _window_el(entry))
    win = _read(live.window) if live.window is not None else None
    b, w = live.node.bounds, win.bounds if win else None
    if b and w and b.y >= w.y and b.y + b.h <= w.y + w.h:
        act.delivery = "delivered_verified"
    del timeout_ms
    return _finish(act, live)


# --- 키보드 · 마우스 (물리 입력) ---
def press(
    ctx: Ctx, combo: str, app: str | None = None, window_id: str | None = None, force: bool = False
) -> dict[str, Any]:
    io = _io()
    _, _, names = io.parse_combo(combo)
    if io.blocked(names) and not force:
        raise CuopError("POLICY_DENIED", f"{combo} quits or force-quits; use close-app, or --force",
                        details={"kind": "dangerous_combo"})  # fmt: skip
    m, pid, win = ax(), None, None
    if window_id is not None:
        win = m.window_by_id(int(window_id.removeprefix("w-")))
        if app is not None and win.pid != m.find_app(app).pid:
            raise CuopError("INVALID_ARGS", f"--window-id {window_id} does not belong to {app!r}")
        pid = win.pid
    elif app is not None:
        pid = m.find_app(app).pid
    act = Act("press")
    if pid is not None:
        io.guard(pid)
    if ctx.headed and pid is not None:
        ok = io.raise_window(win.el if win else None, pid)
        act.step("focus_window", ok)
        if not ok:
            raise not_front(act, pid, win.el if win else None, physical_delivery_started=False)
    if win is not None and m.window_number(m.attr(m.app_element(win.pid), "AXFocusedWindow")) != win.id:
        # 앱은 키를 자기 키 창으로 보낸다 — 지정한 창이 그 창이 아니면 형제 창이 받는다 → 보내지 않는다
        raise act.fail("STALE_REF", "the named window's exact focus could not be confirmed, so no key was sent",
                       check="window_active", cause="exact_window_focus_unconfirmed", physical_delivery_started=False,
                       window_id=window_id)  # fmt: skip
    io.key_combo(combo, pid)
    act.step("key_event", True, "physical_synthetic")
    act.extra["combo"] = combo
    return act.data()


def _need_headed(ctx: Ctx, name: str) -> None:
    if not ctx.headed:
        raise CuopError(
            "POLICY_DENIED", f"{name} moves the pointer; pass --headed", details={"kind": "headed_required"}
        )


def _xy(text: str) -> tuple[float, float]:
    try:
        x, y = (float(v) for v in text.split(","))
    except ValueError:
        raise CuopError("INVALID_ARGS", f"--xy must be X,Y, got {text!r}") from None
    return _finite(x, y)


def _finite(x: float, y: float) -> tuple[float, float]:
    if not (math.isfinite(x) and math.isfinite(y)):
        raise CuopError("INVALID_ARGS", "coordinates must be finite numbers")
    return x, y


def _point(ctx: Ctx, ref: str | None, xy: str | None, act: Act, timeout_ms: int) -> tuple[float, float]:
    if (ref is None) == (xy is None):
        raise CuopError("INVALID_ARGS", "give exactly one of a ref or --xy")
    if xy is not None:
        return _xy(xy)
    assert ref is not None
    live = prepare(ctx, ref, act, timeout_ms)
    _headed_focus(live, act)
    return _aim(live, act)


def hover(ctx: Ctx, ref: str | None = None, xy: str | None = None, duration: int | None = None,
          timeout_ms: int = 5000) -> dict[str, Any]:  # fmt: skip
    _need_headed(ctx, "hover")
    if duration:
        raise CuopError("INVALID_ARGS", "--duration is not supported; hover, then wait <ms>")
    act = Act("hover", ref)
    x, y = _point(ctx, ref, xy, act, timeout_ms)
    _io().mouse_move(x, y)
    act.step("pointer_move", True, "physical_synthetic")
    act.extra["target"] = {"kind": "pointer", "point": {"x": x, "y": y}}
    return act.data()


def drag(ctx: Ctx, from_: str | None = None, to: str | None = None, from_xy: str | None = None,
         to_xy: str | None = None, duration: int = 300, drop_delay: int = 500, timeout_ms: int = 5000,
         wait_for_scope: str = "to") -> dict[str, Any]:  # fmt: skip
    _need_headed(ctx, "drag")
    if (to is None) == (to_xy is None) or (from_ is None) == (from_xy is None):
        raise CuopError("INVALID_ARGS", "give exactly one of --from/--from-xy and one of --to/--to-xy")
    if duration < 0 or drop_delay < 0 or duration + drop_delay >= timeout_ms:
        raise CuopError("INVALID_ARGS", "--duration + --drop-delay must stay below --timeout-ms")
    act = Act("drag")
    a = _point(ctx, from_, from_xy, act, timeout_ms)
    b = _xy(to_xy) if to_xy is not None else _point_nofocus(ctx, to, act, timeout_ms)
    _io().drag(a, b, duration, drop_delay)
    act.step("pointer_drag", True, "physical_synthetic")
    pa, pb = {"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}
    act.extra.update(target={"kind": "drag", "from": pa, "to": pb}, **{"from": pa, "to": pb})
    del wait_for_scope
    return act.data()


def _point_nofocus(ctx: Ctx, ref: str | None, act: Act, timeout_ms: int) -> tuple[float, float]:
    if ref is None:
        raise CuopError("INVALID_ARGS", "give --to or --to-xy")
    return _center(prepare(ctx, ref, act, timeout_ms).node)  # 놓을 곳은 앞으로 가져오지 않는다(출발점을 덮을 수 있다)


def mouse_move(ctx: Ctx, xy: str) -> dict[str, Any]:
    _need_headed(ctx, "mouse-move")
    x, y = _xy(xy)
    _io().mouse_move(x, y)
    act = Act("mouse-move")
    act.step("pointer_move", True, "physical_synthetic")
    act.extra["target"] = {"kind": "pointer", "point": {"x": x, "y": y}}
    return act.data()


def mouse_click(
    ctx: Ctx, xy: str, button: str = "left", count: int = 1, modifiers: str | None = None
) -> dict[str, Any]:
    _need_headed(ctx, "mouse-click")
    if button not in ("left", "right", "middle") or not 1 <= count <= 3:
        raise CuopError("INVALID_ARGS", "--button left|right|middle, --count 1-3")
    x, y = _xy(xy)
    act = Act("mouse-click")
    owner, wid = _io().owner_at(x, y), _io().window_at(x, y)  # 활성화 전에 본 창 — 활성화 뒤에도 같은 창이어야 누른다
    if owner is None and os.environ.get("CUOP_ALLOW_APPS"):  # 허용 목록 세션: 주인을 모르는 점은 누르지 않는다
        raise CuopError("POLICY_DENIED", "no allowed app window is under this point",
                        details={"kind": "occluded_by_other_app"})  # fmt: skip
    if owner is not None:
        try:
            ax().allowed(owner)  # 허용 목록 밖의 앱 창은 좌표로도 누르지 않는다
        except CuopError as e:  # 대개 대상 앱이 뒤에 있어 다른 앱 창이 그 점을 덮은 것(창 스크린샷은 가려도 찍힌다)
            msg = "another app's window covers this point; bring the target window forward (focus-window), then retry"
            raise CuopError("POLICY_DENIED", msg, details={"kind": "occluded_by_other_app"}) from e
        if not ax().is_frontmost(owner):  # 뒤에 있는 앱은 첫 클릭을 활성화에만 쓴다(PowerPoint 개체 틀, Office 시험)
            ok = _io().raise_window(None, owner)
            act.step("focus_app", ok, "semantic_api", setup=True)
            if not ok or _io().owner_at(x, y) != owner:  # 앞으로 오는 사이 그 점의 주인이 바뀌면 누르지 않는다
                raise act.fail("ACTION_FAILED", "the app under the point did not come forward; nothing was clicked",
                               kind="focus_verification", physical_delivery_started=False)  # fmt: skip
    mods = modifiers.split("+") if modifiers else None
    _io().mouse_click(x, y, button, count, mods, expect=owner, expect_window=wid)
    act.step("pointer_click", True, "physical_synthetic", count=count)
    act.extra["target"] = {"kind": "pointer", "point": {"x": x, "y": y}}
    return act.data()


def mouse_wheel(ctx: Ctx, x: float, y: float, dx: int = 0, dy: int = 0, modifiers: str | None = None) -> dict[str, Any]:
    _need_headed(ctx, "mouse-wheel")
    x, y = _finite(x, y)
    _io().mouse_wheel(x, y, dx, dy, modifiers.split("+") if modifiers else None)
    act = Act("mouse-wheel")
    act.step("pointer_wheel", True, "physical_synthetic")
    act.extra["target"] = {"kind": "pointer", "point": {"x": x, "y": y}}
    return act.data()


def held(ctx: Ctx, name: str, combo: str | None = None, force: bool = False, xy: str | None = None,
         **_: Any) -> dict[str, Any]:  # fmt: skip
    """key-down · key-up · mouse-down · mouse-up: 무상태 프로세스는 누른 채로 둘 수 없다 → 항상 거부.
    거부 전에 agent-desktop처럼 인자를 검사한다(잘못된 단축키 · 위험 단축키 · 좌표는 그 오류로)."""
    if combo is not None:
        _, _, names = _io().parse_combo(combo)
        if _io().blocked(names) and not force:
            raise CuopError("POLICY_DENIED", f"{combo} quits or force-quits; use close-app, or --force",
                            details={"kind": "dangerous_combo"})  # fmt: skip
    if xy is not None:
        _xy(xy)
    if name.startswith("mouse") and not ctx.headed:
        raise CuopError("POLICY_DENIED", f"{name} needs --headed (and is not supported statelessly)")
    raise CuopError("ACTION_NOT_SUPPORTED", f"{name} cannot hold input in a stateless process; use press, "
                    "mouse-click or drag", details={"kind": "stateless_hold"})  # fmt: skip
