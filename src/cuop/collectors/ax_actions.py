# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""macOS 조작 원시 연산 — 의미 액션(AXPress · AXValue …)과 물리 입력(CGEvent). 정책 · 검증은 desktop/actions.py."""

import functools
import os
import subprocess
import time
from typing import Any

from AppKit import NSRunningApplication
from ApplicationServices import (
    AXUIElementCopyElementAtPosition,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
)
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventPostToPid,
    CGEventSetFlags,
    CGEventSetIntegerValueField,
    CGEventSetLocation,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskShift,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventOtherMouseDown,
    kCGEventOtherMouseUp,
    kCGEventRightMouseDown,
    kCGEventRightMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonCenter,
    kCGMouseButtonLeft,
    kCGMouseButtonRight,
    kCGMouseEventClickState,
    kCGScrollEventUnitLine,
)

from cuop.collectors.ax import allowed, attr, cg_windows, decode, frontmost_pid, is_frontmost, pid_of
from cuop.desktop.errors import CuopError

# ANSI 키코드 (kVK_*) — 단축키용. 글자 입력은 유니코드 문자열을 실어 보낸다(IME를 거치지 않음)
KEYS: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "tab": 48, "space": 49, "`": 50, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "left": 123, "right": 124,
    "down": 125, "up": 126, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}  # fmt: skip
ALIASES = {
    "comma": ",", "period": ".", "slash": "/", "semicolon": ";", "quote": "'", "leftbracket": "[",
    "rightbracket": "]", "backslash": "\\", "minus": "-", "equal": "=", "grave": "`",
}  # fmt: skip
MODS = {
    "cmd": kCGEventFlagMaskCommand, "command": kCGEventFlagMaskCommand, "meta": kCGEventFlagMaskCommand,
    "ctrl": kCGEventFlagMaskControl, "control": kCGEventFlagMaskControl, "alt": kCGEventFlagMaskAlternate,
    "option": kCGEventFlagMaskAlternate, "opt": kCGEventFlagMaskAlternate, "shift": kCGEventFlagMaskShift,
}  # fmt: skip
# 앱 종료 · 강제 종료 · 휴지통 비우기 — --force 없이는 거부(agent-desktop과 같은 목록)
BLOCKED = {
    frozenset({"cmd", "q"}), frozenset({"cmd", "shift", "q"}), frozenset({"cmd", "alt", "escape"}),
    frozenset({"ctrl", "cmd", "q"}), frozenset({"cmd", "shift", "delete"}), frozenset({"cmd", "alt", "shift", "q"}),
}  # fmt: skip
SETTLE = 0.15
# 없음이 증명된 AX 오류(속성 · 액션 미지원, 미구현)만 다음 수단으로. 그 밖(-25204 등)은 이미 반영됐을 수 있다
ABSENT = frozenset({-25205, -25206, -25208})


def blocked(names: frozenset[str]) -> bool:
    """위험 단축키에 수식키를 더 붙여도 막는다(상위 집합)."""
    return any(b <= names for b in BLOCKED)


@functools.cache
def host_pid() -> int | None:
    """이 프로세스를 띄운 GUI 앱(터미널 · tmux 서버를 띄운 앱 · IDE) — 부모를 따라 올라가 처음 만나는 앱."""
    pid = os.getpid()
    for _ in range(40):
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not out or (pid := int(out)) <= 1:
            return None
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if ra is not None and ra.bundleIdentifier():
            return pid
    return None


def guard(pid: int) -> None:
    """호스트 앱(이 에이전트를 돌리는 터미널 등)은 조작하지 않는다 — 입력이 에이전트 세션이나 셸로 들어가지 않게."""
    if pid == host_pid() or pid == os.getpid():
        raise CuopError(
            "POLICY_DENIED",
            "refusing to operate the app that runs this agent (host terminal)",
            details={"kind": "host_app", "pid": pid},
        )


# --- 의미 액션 ---
def perform(el: Any, action: str) -> int:
    return int(AXUIElementPerformAction(el, action))


def set_attr(el: Any, name: str, value: Any) -> int:
    return int(AXUIElementSetAttributeValue(el, name, value))


def raise_window(win: Any, pid: int) -> bool:
    """창을 맨 앞으로: 앱 AXFrontmost + 창 AXRaise · AXMain. 확인되면 True (headed 전용).

    macOS 14+의 협력적 활성화 때문에 백그라운드 프로세스의 AXFrontmost는 무시될 수 있다(tmux에서 띄운 세션에서 확인,
    2026-11-21) → LaunchServices(`open -b`)로 활성화를 한 번 더 요청한다. 창이 있는 상태라 새 창은 안 생긴다.
    """
    guard(pid)
    set_attr(AXUIElementCreateApplication(pid), "AXFrontmost", True)
    if win is not None:
        perform(win, "AXRaise")
        set_attr(win, "AXMain", True)

    def front() -> bool:
        return is_frontmost(pid) and (win is None or attr(win, "AXMain") is not False)

    if _until(front, 0.4):
        return True
    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if ra is not None and ra.bundleIdentifier():
        subprocess.run(["open", "-b", str(ra.bundleIdentifier())], capture_output=True, timeout=10)
        if win is not None:
            perform(win, "AXRaise")
    return _until(front, 2.0)


def _until(done: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.05)
    return bool(done())


def element_at(x: float, y: float) -> tuple[Any, int | None]:
    err, el = AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), x, y, None)
    return (el, pid_of(el)) if not err and el is not None else (None, None)


# --- 물리 입력 ---
def parse_combo(combo: str) -> tuple[int, int, frozenset[str]]:
    """`cmd+shift+s` → (키코드, 플래그, 이름 집합). 대문자 한 글자는 shift를 더한다."""
    parts = [p for p in combo.strip().split("+") if p] or [combo]
    *mods, key = parts
    if len(key) == 1 and key.isupper():
        mods, key = [*mods, "shift"], key.lower()
    key = ALIASES.get(key.lower(), key.lower())
    if key not in KEYS:
        raise CuopError("INVALID_ARGS", f"unknown key {key!r} in {combo!r}", details={"kind": "unknown_key"})
    flags, names = 0, {key}
    for m in mods:
        if m.lower() not in MODS:
            raise CuopError("INVALID_ARGS", f"unknown modifier {m!r}", details={"kind": "unknown_modifier"})
        flags |= MODS[m.lower()]
        names.add(
            {"command": "cmd", "meta": "cmd", "control": "ctrl", "option": "alt", "opt": "alt"}.get(
                m.lower(), m.lower()
            )
        )
    return KEYS[key], flags, frozenset(names | ({"escape"} if key == "esc" else set()))


def key_combo(combo: str, pid: int | None) -> None:
    """키 한 번(누름 + 뗌). pid를 주면 그 앱에만(포커스를 바꾸지 않음), 없으면 맨 앞 앱."""
    code, flags, _ = parse_combo(combo)
    target = pid if pid is not None else frontmost_pid()
    if target is None:
        raise CuopError("APP_NOT_FOUND", "no frontmost application to receive the key")
    guard(target)
    allowed(target)  # 대상 없이 맨 앞 앱으로 가는 키도 허용 목록(CUOP_ALLOW_APPS) 밖이면 보내지 않는다
    evs = [CGEventCreateKeyboardEvent(None, code, d) for d in (True, False)]
    for ev in evs:
        CGEventSetFlags(ev, flags)
    for ev in evs:
        CGEventPostToPid(target, ev)
    time.sleep(SETTLE)


def type_unicode(text: str, pid: int, interval_ms: int = 0, still_focused: Any = None) -> tuple[int, int]:
    """유니코드 문자열을 키 이벤트로(한글도 IME 없이) → (보낸 조각 수, 전체 조각 수). 코드 포인트 경계로 자른다.
    still_focused: 조각 사이마다 부르는 확인 함수 — False면 거기서 멈춘다(포커스가 다른 칸 · 창으로 옮겨 간 경우)."""
    guard(pid)
    chunks: list[str] = list(text) if interval_ms else [text[i : i + 20] for i in range(0, len(text), 20)]
    sent = 0
    for chunk in chunks:
        if still_focused is not None and not still_focused():  # 첫 조각 전에도
            break
        if chunk in ("\n", "\r"):
            evs = [CGEventCreateKeyboardEvent(None, KEYS["return"], d) for d in (True, False)]
        else:
            down, up = CGEventCreateKeyboardEvent(None, 0, True), CGEventCreateKeyboardEvent(None, 0, False)
            CGEventKeyboardSetUnicodeString(down, len(chunk.encode("utf-16-le")) // 2, chunk)  # 글자는 누름에만
            evs = [down, up]
        for ev in evs:
            CGEventPostToPid(pid, ev)
        sent += 1
        if interval_ms:
            time.sleep(interval_ms / 1000)
    time.sleep(SETTLE)
    return sent, len(chunks)


BUTTONS = {
    "left": (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft),
    "right": (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight),
    "middle": (kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter),
}


def owner_at(x: float, y: float) -> int | None:
    """그 점의 주인 앱: AX hit test, 실패하면 화면의 앞→뒤 창 목록에서 그 점을 품은 첫 창의 주인."""
    _, owner = element_at(x, y)
    if owner is not None:
        return owner
    for w in cg_windows(on_screen=True):
        b = w.get("kCGWindowBounds") or {}
        if b.get("X", 0) <= x < b.get("X", 0) + b.get("Width", 0) and b.get("Y", 0) <= y < b.get("Y", 0) + b.get(
            "Height", 0
        ):
            return int(w["kCGWindowOwnerPID"])
    return None


def window_at(x: float, y: float) -> int | None:
    """그 점을 품은 맨 앞 창의 CG 번호(앞→뒤 창 목록)."""
    for w in cg_windows(on_screen=True):
        b = w.get("kCGWindowBounds") or {}
        if b.get("X", 0) <= x < b.get("X", 0) + b.get("Width", 0) and b.get("Y", 0) <= y < b.get("Y", 0) + b.get(
            "Height", 0
        ):
            return int(w["kCGWindowNumber"])
    return None


def check_point(x: float, y: float) -> int | None:
    """그 점의 주인 앱 — 호스트 앱이면 거부(모든 물리 포인터 경로)."""
    owner = owner_at(x, y)
    if owner is not None:
        guard(owner)
    return owner


def mouse_click(
    x: float,
    y: float,
    button: str = "left",
    count: int = 1,
    modifiers: list[str] | None = None,
    expect: int | None = None,
    expect_window: int | None = None,
) -> None:
    """expect · expect_window: 호출한 쪽이 확인한 그 점의 주인 앱 · 맨 앞 창. 보내기 직전에 다시 보고 다르면
    (다른 앱 창이 덮었거나 같은 앱의 다른 창이 올라옴) 보내지 않는다(Codex Office 리뷰)."""
    owner = check_point(x, y)
    if (expect is not None and owner != expect) or (expect_window is not None and window_at(x, y) != expect_window):
        raise CuopError("ACTION_FAILED", "the window under the point changed; nothing was clicked",
                        details={"kind": "focus_verification", "physical_delivery_started": False})  # fmt: skip
    down_t, up_t, btn = BUTTONS[button]
    flags = _flags(modifiers)
    events = []  # 모두 먼저 만든 뒤 보낸다 — 누름만 보내고 뗌을 못 만드는 일이 없게
    for i in range(1, count + 1):
        for kind in (down_t, up_t):
            ev = CGEventCreateMouseEvent(None, kind, (x, y), btn)
            CGEventSetIntegerValueField(ev, kCGMouseEventClickState, i)
            if flags:
                CGEventSetFlags(ev, flags)
            events.append(ev)
    for ev in events:
        CGEventPost(kCGHIDEventTap, ev)
    time.sleep(SETTLE)


def _flags(modifiers: list[str] | None) -> int:
    flags = 0
    for m in modifiers or []:
        if m.lower() not in MODS:
            raise CuopError("INVALID_ARGS", f"unknown modifier {m!r}")
        flags |= MODS[m.lower()]
    return flags


def mouse_move(x: float, y: float) -> None:
    check_point(x, y)
    CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft))


def mouse_wheel(x: float, y: float, dx: int, dy: int, modifiers: list[str] | None = None) -> None:
    check_point(x, y)
    ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, dy, dx)
    CGEventSetLocation(ev, (x, y))  # 휠 이벤트 자체에 위치를 싣는다(따로 보낸 이동에 기대지 않음)
    if flags := _flags(modifiers):
        CGEventSetFlags(ev, flags)
    CGEventPost(kCGHIDEventTap, ev)
    time.sleep(SETTLE)


def drag(a: tuple[float, float], b: tuple[float, float], duration_ms: int, drop_delay_ms: int) -> None:
    """누르고 → 나눠 움직이고 → 놓기. 경로 위 점을 미리 모두 확인하고(호스트 앱 위를 지나지 않게),
    중간에 실패하면 마지막으로 도달한 점에서 뗀다(release guard)."""
    steps = max(4, duration_ms // 16)
    path = [(a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps) for i in range(steps + 1)]
    for p in path[:: max(1, steps // 8)] + [b]:
        check_point(*p)
    post = functools.partial(CGEventPost, kCGHIDEventTap)
    moves = [CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, p, kCGMouseButtonLeft) for p in path[1:]]
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, a, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, a, kCGMouseButtonLeft))
    last = a
    post(down)
    try:
        for p, ev in zip(path[1:], moves, strict=True):
            post(ev)
            last = p
            time.sleep(duration_ms / 1000 / steps)
        time.sleep(drop_delay_ms / 1000)
    finally:
        post(CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, last, kCGMouseButtonLeft))
    time.sleep(SETTLE)


# --- 열린 overlay ---
def open_surfaces(pid: int, window: Any) -> list[str]:
    """액션 뒤 열린 surface(menu · sheet · popover · alert) — 에이전트가 다음에 겨눌 곳."""
    out: list[str] = []
    app = AXUIElementCreateApplication(pid)
    if any(attr(c, "AXRole") == "AXMenu" for c in attr(app, "AXChildren") or ()):
        out.append("menu")
    bar = attr(app, "AXMenuBar")
    for item in attr(bar, "AXChildren") or () if bar is not None else ():
        if attr(item, "AXSelected") is True and "menu" not in out:
            out.append("menu")
    for c in attr(window, "AXChildren") or () if window is not None else ():
        role = attr(c, "AXRole")
        if role == "AXSheet" and "sheet" not in out:
            out.append("sheet")
        if role == "AXPopover" and "popover" not in out:
            out.append("popover")
    for w in attr(app, "AXWindows") or ():
        if attr(w, "AXSubrole") in ("AXDialog", "AXSystemDialog") and "alert" not in out:
            out.append("alert")
    return out


def scrollbars(el: Any) -> tuple[Any, Any]:
    return attr(el, "AXVerticalScrollBar"), attr(el, "AXHorizontalScrollBar")


def number(v: Any) -> float | None:
    v = decode(v)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None
