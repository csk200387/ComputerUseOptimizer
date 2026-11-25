# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""macOS 수집기 — Accessibility API(AXUIElement) via PyObjC.

AX 호출은 블로킹 IPC라 전용 워커 스레드 1개에서 직렬로 실행한다.
"""

import asyncio
import functools
import os
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from AppKit import NSRunningApplication
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCopyElementAtPosition,
    AXUIElementCopyMultipleAttributeValues,
    AXUIElementCopyParameterizedAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
    AXValueCreate,
    AXValueGetType,
    AXValueGetValue,
    AXValueRef,
    kAXValueCFRangeType,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from Quartz import (
    CGDisplayBounds,
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventPostToPid,
    CGMainDisplayID,
    CGPreflightScreenCaptureAccess,
    CGWindowListCopyWindowInfo,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGNullWindowID,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionAll,
    kCGWindowListOptionOnScreenOnly,
)

from cuop.schema import Bounds, Role, State, UIElementNode, UISnapshot

# 노드마다 IPC 1회로 일괄 조회. 순서 = _node()의 언패킹 순서
ATTRS = (
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXHelp", "AXPlaceholderValue",
    "AXEnabled", "AXFocused", "AXSelected", "AXExpanded", "AXPosition", "AXSize", "AXChildren",
)  # fmt: skip
MAX_NODES = 5000  # ponytail: 고정 상한, 대형 앱에서 잘리면 창 단위 수집·지연 펼침으로 전환
MAX_DEPTH = 60
KEY_RETURN = 36  # kVK_Return
# 하위 메뉴에 사용자 데이터가 있는 메뉴 항목(최근 파일 · 설치 앱 · 연락처 제안) — 항목은 남기고 하위는 수집하지 않는다
# (2026-11-03 사용자 결정, 모든 모드). ponytail: 현지화 제목으로 판별 — 다른 언어 · 앱이 나오면 추가
PRIVATE_MENUS = frozenset({"최근 사용 열기", "서비스", "공유", "Open Recent", "Services", "Share"})
SETTLE = 0.3  # 마우스 이벤트 반영 대기(초)

ROLES = {
    "AXWindow": Role.WINDOW,
    "AXSheet": Role.DIALOG,
    "AXMenuBar": Role.MENUBAR,
    "AXMenu": Role.MENU,
    "AXMenuBarItem": Role.MENUITEM,
    "AXMenuItem": Role.MENUITEM,
    "AXToolbar": Role.TOOLBAR,
    "AXTabGroup": Role.TABLIST,
    "AXButton": Role.BUTTON,
    "AXMenuButton": Role.BUTTON,
    "AXPopUpButton": Role.COMBOBOX,
    "AXComboBox": Role.COMBOBOX,
    "AXLink": Role.LINK,
    "AXCheckBox": Role.CHECKBOX,
    "AXRadioButton": Role.RADIO,
    "AXTextField": Role.TEXTBOX,
    "AXTextArea": Role.TEXTBOX,
    "AXList": Role.LIST,
    "AXTable": Role.TABLE,
    "AXOutline": Role.TREE,
    "AXRow": Role.ROW,
    "AXCell": Role.CELL,
    "AXSlider": Role.SLIDER,
    "AXHeading": Role.HEADING,
    "AXStaticText": Role.TEXT,
    "AXImage": Role.IMAGE,
    "AXWebArea": Role.DOCUMENT,
    "AXGroup": Role.GROUP,
    "AXScrollArea": Role.GROUP,
    "AXSplitGroup": Role.GROUP,
}
SUBROLES = {
    "AXTabButton": Role.TAB,
    "AXDialog": Role.DIALOG,
    "AXSystemDialog": Role.DIALOG,
    "AXOutlineRow": Role.TREEITEM,
}


def _decode(v):
    """AXValueRef 해석: 누락 속성(kAXValueAXErrorType)은 None, CGPoint/CGSize는 (a, b) 튜플."""
    if not isinstance(v, AXValueRef):
        return v
    kind = AXValueGetType(v)
    if kind not in (kAXValueCGPointType, kAXValueCGSizeType):
        return None
    _, p = AXValueGetValue(v, kind, None)
    return (p.x, p.y) if kind == kAXValueCGPointType else (p.width, p.height)


def _str(v) -> str | None:
    return str(v) if isinstance(v, str) and v.strip() else None


def _value(v) -> str | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):  # 슬라이더·스테퍼
        return str(v)
    return _str(v)


def _plain(s: str) -> str:
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")  # LRM 등 — 직렬화기와 같은 규칙


def _label(el) -> str | None:
    err, (title, desc, value) = AXUIElementCopyMultipleAttributeValues(
        el, ("AXTitle", "AXDescription", "AXValue"), 0, None
    )
    return None if err else next((_plain(v) for v in (title, desc, value) if _str(v)), None)


def _find_option(el, option: str, limit: int = 2000):
    """el 아래에서 이름(제목 · 설명 · 값)이 option인 첫 요소 — 너비 우선, 최대 limit개."""
    queue, want = [el], _plain(option).strip()
    while queue and limit > 0:
        node = queue.pop(0)
        limit -= 1
        err, (children,) = AXUIElementCopyMultipleAttributeValues(node, ("AXChildren",), 0, None)
        for child in () if err else (_decode(children) or ()):  # 자식 속성이 없으면 에러 값(AXValueRef)
            if (_label(child) or "").strip() == want:
                return child
            queue.append(child)
    return None


def _attr(el, name: str) -> Any:  # PyObjC 값 — 스텁이 없다
    err, v = AXUIElementCopyAttributeValue(el, name, None)
    return None if err else _decode(v)


def _mark_superscripts(runs: list[tuple[str, float]]) -> str | None:
    """글자 구간들 중 눈에 띄게 작은 글씨(가장 큰 글씨의 85% 미만)를 위첨자로 보고 ^(…)로 표시한다.

    계산기 공학용은 2¹⁰을 밑수 30.6pt · 지수 21.4pt로 그리는데 접근성 텍스트는 '210'이라
    트리를 보는 에이전트가 xʸ가 안 눌린 줄 알고 다시 입력했다(2026-11-03 하네스). 구간이 하나면 그대로(None).
    """
    runs = [(seg, size) for seg, size in runs if seg]
    if len(runs) < 2:
        return None
    big = max(size for _, size in runs)
    small = [0 < size < big * 0.85 for _, size in runs]
    return "".join(f"^({seg})" if s else seg for (seg, _), s in zip(runs, small, strict=True)) if any(small) else None


def _superscripts(el, text: str) -> str | None:
    n = len(text.encode("utf-16-le")) // 2  # 범위는 UTF-16 단위
    rng = AXValueCreate(kAXValueCFRangeType, (0, n))
    err, s = AXUIElementCopyParameterizedAttributeValue(el, "AXAttributedStringForRange", rng, None)
    runs, i = [], 0
    while not err and s is not None and i < s.length():
        attrs, r = s.attributesAtIndex_effectiveRange_(i, None)
        seg = _plain(str(s.string())[r.location : r.location + r.length])
        runs.append((seg, float((attrs.get("AXFont") or {}).get("AXFontSize") or 0)))
        i = r.location + r.length
    return _mark_superscripts(runs)


def _value_of(el) -> object:
    return _attr(el, "AXValue")


def _ancestor(el, role: str, depth: int = 6):
    for _ in range(depth):
        if (el := _attr(el, "AXParent")) is None or _attr(el, "AXRole") == role:
            return el
    return None


def _choose(item) -> int:
    """누르거나 고른다: AXPress가 없는 목록 행(콤보박스 목록 · 표)은 AXSelected = True, 입력 칸은 AXFocused = True.

    콤보박스 목록은 AXSelected로 골라도 열린 채라(마우스로 고르면 닫힌다) 콤보박스의 AXExpanded를 끈다.
    """
    if not (err := AXUIElementPerformAction(item, "AXPress")):
        return 0
    if AXUIElementSetAttributeValue(item, "AXSelected", True):
        # 텍스트 영역 · 입력 칸은 AXPress · AXSelected가 없다(-25206) → click = 포커스 (2026-11-03 하네스)
        return 0 if not AXUIElementSetAttributeValue(item, "AXFocused", True) else err
    if (combo := _ancestor(item, "AXComboBox")) is not None:
        AXUIElementSetAttributeValue(combo, "AXExpanded", False)
    return 0


def _size_zero(el) -> bool:
    wh = _attr(el, "AXSize") if el is not None else None
    return not (isinstance(wh, tuple) and wh[0] and wh[1])


def _menu_closed(el) -> bool:
    """팝업 아래 메뉴가 없거나 크기 0 — 닫히는 중인 메뉴는 아직 크기가 있어 관찰에 섞인다."""
    err, (children,) = AXUIElementCopyMultipleAttributeValues(el, ("AXChildren",), 0, None)
    for child in () if err else (_decode(children) or ()):  # 자식 속성이 없으면 에러 값(AXValueRef)
        err, (size,) = AXUIElementCopyMultipleAttributeValues(child, ("AXSize",), 0, None)
        if not err and (wh := _decode(size)) and wh[0] and wh[1]:
            return False
    return True


def _wait(done: Callable[[], bool], timeout: float = 1.0) -> None:
    """비동기로 반영되는 액션(팝업 항목 · 키 이벤트) 뒤 결과를 기다린다 — 바로 관찰하면 바뀌기 전 화면을 본다."""
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        time.sleep(0.05)


@functools.cache
def _host_pid() -> int | None:
    """이 서버를 띄운 GUI 앱(터미널 · IDE · Claude 앱) — 프로세스 부모를 따라 올라가 처음 만나는 앱."""
    pid = os.getpid()
    for _ in range(30):
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not out or (pid := int(out)) <= 1:
            return None
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if running is not None and running.bundleIdentifier():
            return pid
    return None


def _guard(pid: int) -> None:
    """호스트 앱은 조작하지 않는다 — 클릭 · 키 입력이 이 에이전트 세션이나 셸에 들어가지 않게(모든 모드)."""
    if pid == _host_pid():
        raise PermissionError("refusing to operate the app that runs this agent; observe another app")


def _find_app(app: str | None) -> tuple[int, str]:
    """화면에 창이 있는 앱 중 맨 앞(app=None) 또는 이름·번들 ID가 일치하는 앱의 (pid, 번들 ID).

    창 목록은 호출마다 창 서버에서 새로 읽는다: NSWorkspace는 런루프 없는 프로세스에서 갱신되지 않고,
    시스템 전역 AXFocusedApplication은 -25204로 실패하는 환경이 있다. 창 소유자 이름은 현지화됨('터미널').
    이름을 준 앱은 다른 데스크톱(Space)에 있어도 찾는다 — 현재 화면에 없을 뿐 AX로는 창 · 메뉴가 읽히고
    버튼 AXPress도 된다(2026-11-03 하네스 run: 사용자가 터미널 데스크톱으로 돌아가자 계산기를 못 찾음).
    """
    allow = {x.strip() for x in os.environ.get("CUOP_ALLOW_APPS", "").split(",") if x.strip()}
    on_screen = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    scopes = [on_screen] if app is None else [on_screen, kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements]
    for options in scopes:
        for w in CGWindowListCopyWindowInfo(options, kCGNullWindowID):
            if w.get("kCGWindowLayer") != 0:  # 메뉴바 · Dock · 오버레이 제외
                continue
            pid = int(w["kCGWindowOwnerPID"])
            running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            bundle = str(running.bundleIdentifier() or "") if running else ""
            owner = str(w.get("kCGWindowOwnerName", ""))
            if allow and bundle not in allow:  # 하네스 허용 목록 밖의 앱은 찾지도 않는다(맨 앞이어도)
                continue
            if app is None or app.lower() in {owner.lower(), bundle.lower(), bundle.rsplit(".", 1)[-1].lower()}:
                return pid, bundle or owner
    if app is not None:  # 창이 없는 앱(제어 센터 등 메뉴 막대 앱): 실행 중인 앱에서 이름 · 번들 ID로
        from AppKit import NSWorkspace

        for ra in NSWorkspace.sharedWorkspace().runningApplications():
            bundle, name = str(ra.bundleIdentifier() or ""), str(ra.localizedName() or "")
            keys = {name.lower(), bundle.lower(), bundle.rsplit(".", 1)[-1].lower()}
            if (not allow or bundle in allow) and app.lower().replace(" ", "") in {k.replace(" ", "") for k in keys}:
                return int(ra.processIdentifier()), bundle or name
    raise LookupError(f"no app with a window matches {app!r} (use its name or bundle id)")


def _capture_windows(pid: int, r: Bounds, out: Path) -> None:
    """영역 r(point) 안에서 pid의 창 + 메뉴 막대 창만 합성해 PNG로(기기 픽셀). 다른 창 자리는 비어 있다."""
    import Quartz as Q

    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if ra is None or int(ra.activationPolicy()) != 0:  # 메뉴 막대 앱(제어 센터 등): 프로세스 창 = 여러 패널이 섞인다
        raise PermissionError("menu-bar apps cannot be captured without other panels; use the accessibility tree")
    wins = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID) or []
    ids = [
        int(w["kCGWindowNumber"])
        for w in wins
        if int(w.get("kCGWindowOwnerPID", -1)) == pid
        or (w.get("kCGWindowOwnerName") == "Window Server" and w.get("kCGWindowName") == "Menubar")
    ]
    rect = Q.CGRectMake(r.x, r.y, r.w, r.h)
    img = Q.CGWindowListCreateImageFromArray(rect, ids, Q.kCGWindowImageDefault)
    if img is None:
        raise RuntimeError("window capture failed")
    url = Q.CFURLCreateWithFileSystemPath(None, str(out), Q.kCFURLPOSIXPathStyle, False)
    dest = Q.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Q.CGImageDestinationAddImage(dest, img, None)
    if not Q.CGImageDestinationFinalize(dest):
        raise RuntimeError("PNG write failed")


class MacCollector:
    """세션 동안 AXUIElement ↔ element_id를 유지한다. AXUIElement는 CFEqual/CFHash로 비교되므로
    따로 가져온 프록시라도 같은 요소면 같은 dict 키 → 스냅샷이 바뀌어도 같은 ID."""

    def __init__(self) -> None:
        self._ids: dict[object, str] = {}  # ponytail: 세션 동안 계속 커짐, 장시간 실행 시 미관측 ID 정리
        self._els: dict[str, object] = {}
        self._seq = 0
        self._count = 0
        self._app = None  # 마지막으로 수집한 앱 요소 — type의 기본 대상(포커스된 요소)을 찾는다
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ax")
        AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), 1.0)  # 무응답 앱에 막히지 않게 (초, 전역)

    async def _run[T](self, fn: Callable[..., T], *args: object) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    async def snapshot(self, app: str | None = None) -> UISnapshot:
        return await self._run(self._snapshot, app)

    async def click(self, element_id: str) -> None:
        await self._run(self._press, element_id)

    async def type_text(self, element_id: str | None, text: str, submit: bool = False) -> None:
        await self._run(self._type, element_id, text, submit)

    async def select(self, element_id: str, option: str) -> None:
        await self._run(self._select, element_id, option)

    async def window_title(self, app: str) -> str | None:
        """앱의 맨 앞 창 제목 — web 수집기가 보이는 탭을 고를 때 쓴다 (CDP에는 활성 탭 정보가 없다)."""
        return await self._run(self._window_title, app)

    async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        return await self._run(self._capture, region, width, height, only_target)

    async def click_at(self, x: int, y: int) -> None:
        await self._run(self._click_at, x, y)

    async def owner(self, element_id: str) -> str | None:
        return await self._run(self._owner, element_id)

    def _owner(self, element_id: str) -> str | None:
        _, pid = AXUIElementGetPid(self._el(element_id), None)
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return str(running.bundleIdentifier()) if running is not None and running.bundleIdentifier() else None

    async def display(self) -> Bounds:
        r = CGDisplayBounds(CGMainDisplayID())  # 전역 point 좌표 — AX와 같은 좌표계
        return Bounds(x=round(r.origin.x), y=round(r.origin.y), w=round(r.size.width), h=round(r.size.height))

    async def type_keys(self, text: str, submit: bool = False) -> None:
        await self._run(self._type_keys, text, submit)

    def _snapshot(self, app: str | None) -> UISnapshot:
        pid, label = _find_app(app)
        self._count = 0
        self._app = AXUIElementCreateApplication(pid)
        root = self._walk(self._app, 0)
        assert root is not None  # 앱 요소 자체는 걸러지지 않는다
        self._seq += 1
        window = next((c.name for c in root.children if c.role in (Role.WINDOW, Role.DIALOG)), None)
        return UISnapshot(id=f"s{self._seq}", platform="macos", app=label, window=window, root=root)

    def _window_title(self, app: str) -> str | None:
        try:
            pid, _ = _find_app(app)
        except LookupError:
            return None
        for attr in ("AXMainWindow", "AXFocusedWindow"):
            err, win = AXUIElementCopyAttributeValue(AXUIElementCreateApplication(pid), attr, None)
            if not err and win is not None:
                err, title = AXUIElementCopyAttributeValue(win, "AXTitle", None)
                return None if err else _str(title)
        return None

    def _front(self, el=None) -> int:
        """요소(기본: 마지막으로 관찰한 앱)의 앱을 맨 앞으로. 좌표 클릭 · 캡처는 이미지가 실제 화면과 같아야 하고,
        메뉴 명령은 활성 창이 대상이라 앱이 뒤에 있으면 항목이 모두 disabled다(2026-11-03 실측)."""
        if (el := el or self._app) is None:
            raise LookupError("call observe first")
        _, pid = AXUIElementGetPid(el, None)
        _guard(pid)
        AXUIElementSetAttributeValue(AXUIElementCreateApplication(pid), "AXFrontmost", True)

        def front() -> bool:  # 허용 목록 밖의 앱이 맨 앞이면 _find_app(None)이 못 찾는다 — '아직 아님'으로
            try:
                return _find_app(None)[0] == pid
            except LookupError:
                return False

        _wait(front)  # 메뉴 막대 앱(제어 센터)은 맨 앞이 되지 않는다 — 1초 기다리고 그대로 진행
        return pid

    def _capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        """screencapture로 영역(point)을 찍고 sips로 width×height로 줄인다 — OS 기본 도구, 새 의존성 없음.
        only_target: 대상 앱의 창(메뉴 · 팝업 포함)과 메뉴 막대만 합성하고 나머지는 비운다 — 화면에 떠 있는 다른 앱
        (메신저 등)이 모델로 가지 않게(2026-11-21 M9 하네스 ① screenshot 모드, DECISIONS D13)."""
        if not CGPreflightScreenCaptureAccess():  # 권한 창을 띄우지 않고 확인만
            raise PermissionError(
                "Screen Recording is off for the app running this server (System Settings > Privacy & Security)"
            )
        pid = self._front()
        r = region
        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / "v.png"
            # stdout은 JSON-RPC 전용 → 하위 프로세스 출력은 모두 잡는다
            run = functools.partial(subprocess.run, check=True, capture_output=True, timeout=10)
            if only_target:
                _capture_windows(pid, r, png)
            else:
                run(["screencapture", "-x", "-t", "png", f"-R{r.x},{r.y},{r.w},{r.h}", str(png)])
            run(["sips", "-z", str(height), str(width), str(png)])
            return png.read_bytes()

    def _type_keys(self, text: str, submit: bool) -> None:
        """대상 앱에만 키보드 이벤트 — 유니코드 문자열을 실어 보내 한글도 입력 기관(IME)을 거치지 않는다."""
        pid = self._front()
        units = text.encode("utf-16-le")
        for i in range(0, len(units), 40):  # 이벤트 하나에 UTF-16 20자까지
            chunk = units[i : i + 40].decode("utf-16-le")
            for down in (True, False):
                event = CGEventCreateKeyboardEvent(None, 0, down)
                CGEventKeyboardSetUnicodeString(event, len(chunk.encode("utf-16-le")) // 2, chunk)
                CGEventPostToPid(pid, event)
        if submit:
            for down in (True, False):
                CGEventPostToPid(pid, CGEventCreateKeyboardEvent(None, KEY_RETURN, down))
        time.sleep(SETTLE)  # ponytail: 고정 대기 — 키 이벤트는 비동기로 반영된다

    def _click_at(self, x: int, y: int) -> None:
        """그 점이 대상 앱의 것일 때만 실제 마우스 클릭 — 다른 앱이 덮고 있으면 누르지 않는다."""
        pid = self._front()
        err, el = AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), x, y, None)
        owner = AXUIElementGetPid(el, None)[1] if not err and el is not None else None
        if owner != pid:
            raise RuntimeError(f"point ({x}, {y}) is covered by another app; nothing clicked")
        for kind in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
            CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kind, (x, y), kCGMouseButtonLeft))
        time.sleep(SETTLE)  # ponytail: 고정 대기 — 반영을 알 수 있는 조건이 생기면 _wait로

    def _walk(self, el, depth: int) -> UIElementNode | None:
        err, values = AXUIElementCopyMultipleAttributeValues(el, ATTRS, 0, None)
        vals = [_decode(v) for v in values] if not err and values else [None] * len(ATTRS)
        # Apple 메뉴는 앱이 아닌 시스템 메뉴이고 최근 사용 항목 · 사용자 이름이 들어 있어 모든 모드에서 뺀다
        if vals[0] == "AXMenuBarItem" and vals[2] == "Apple":
            return None
        eid = self._id(el)  # 전위 순서로 발급 → 첫 관찰에서 ID가 위→아래로 증가
        self._count += 1
        children = []
        private = vals[0] == "AXMenuItem" and vals[2] in PRIVATE_MENUS
        for child in (vals[-1] or ()) if depth < MAX_DEPTH and not private else ():
            if self._count >= MAX_NODES:
                break
            if (node := self._walk(child, depth + 1)) is not None:
                children.append(node)
        node = self._node(eid, vals, children)
        # ponytail: 짧고 숫자가 든 글자만 서식을 본다(노드마다 IPC 1회 추가) — 수식 표시 외의 위첨자가 필요하면 넓힌다
        if node.role is Role.TEXT and node.name and len(node.name) <= 32 and any(ch.isdigit() for ch in node.name):
            if (marked := _superscripts(el, node.name)) is not None:
                node = node.model_copy(update={"name": marked})
        return node

    def _node(self, eid: str, vals, children: list[UIElementNode]) -> UIElementNode:
        raw, sub, title, desc, value, help_, placeholder, enabled, focused, selected, expanded, pos, size, _ = vals
        role = SUBROLES.get(sub) or ROLES.get(raw, Role.GENERIC)
        states: set[State] = set()
        if enabled is False:
            states.add(State.DISABLED)
        if focused is True:
            states.add(State.FOCUSED)
        if selected is True:
            states.add(State.SELECTED)
        # 메뉴의 AXExpanded는 열려 있어도 false(틀린 정보) → 메뉴 계열엔 옮기지 않음. 열림은 크기·selected로 보인다
        if isinstance(expanded, bool) and role not in (Role.MENUBAR, Role.MENU, Role.MENUITEM):
            states.add(State.EXPANDED if expanded else State.COLLAPSED)
        name, text = _str(title) or _str(desc), None
        if role is Role.BUTTON and not name and isinstance(sub, str) and sub != "AXUnknown":
            name = sub.removeprefix("AX")  # 창 제어 버튼(AXCloseButton 등)은 이름이 없다 → "CloseButton"
        if role in (Role.CHECKBOX, Role.RADIO, Role.TAB):
            if value == 1:
                states.add(State.SELECTED if role is Role.TAB else State.CHECKED)
        elif role is Role.TEXT:
            name = name or _str(value)  # 정적 텍스트의 내용은 AXValue에 있음
        else:
            text = _value(value)
        bounds = None
        if pos and size:
            bounds = Bounds(x=round(pos[0]), y=round(pos[1]), w=round(size[0]), h=round(size[1]))
        return UIElementNode(
            id=eid,
            role=role,
            raw_role=str(raw or "?"),
            name=name,
            value=text,
            description=_str(placeholder) or _str(help_),
            bounds=bounds,
            states=frozenset(states),
            children=children,
        )

    def _id(self, el) -> str:
        if (eid := self._ids.get(el)) is None:
            eid = self._ids[el] = f"e{len(self._ids) + 1}"
            self._els[eid] = el
        return eid

    def _el(self, element_id: str):
        if (el := self._els.get(element_id)) is None:
            raise LookupError(f"unknown element id {element_id!r}; call observe first")
        return el

    def _press(self, element_id: str) -> None:
        el = self._el(element_id)
        _guard(AXUIElementGetPid(el, None)[1])
        role = _attr(el, "AXRole")
        if role in ("AXMenuBarItem", "AXMenuItem"):
            self._front(el)  # 메뉴만 앞으로 (2026-11-03 사용자 결정) — 버튼 · 체크박스는 뒤에서도 눌린다
        if err := _choose(el):
            raise RuntimeError(f"cannot click {element_id} (AXError {err})")
        if role == "AXMenuItem":  # 메뉴는 누른 뒤 ~0.4초에 닫힌다 — 바로 관찰하면 열린 메뉴를 본다
            menu = _attr(el, "AXParent")
            _wait(lambda: _size_zero(menu))

    def _type(self, element_id: str | None, text: str, submit: bool) -> None:
        """AXValue를 바꾼다(기존 내용 대체). 키 입력이 아니라 포커스 · 앱 순서가 바뀌지 않는다.

        ponytail: AXValue를 못 바꾸는 요소(웹뷰 · 일부 커스텀 컨트롤)는 에러로 알린다. 그런 앱이 나오면
        CGEventKeyboardSetUnicodeString + CGEventPostToPid 키 입력으로 넘어가는 경로를 추가한다.
        """
        if element_id is None:
            err, el = AXUIElementCopyAttributeValue(self._app, "AXFocusedUIElement", None) if self._app else (1, None)
            if err or el is None:
                raise LookupError("no focused element; pass target")
        else:
            el = self._el(element_id)
            _guard(AXUIElementGetPid(el, None)[1])
            if _attr(el, "AXRole") == "AXComboBox":  # 값 + AXConfirm은 칸 글자만 바뀐다 → select와 같은 경로
                self._select(element_id, text)  # 목록에 있으면 항목을 고르고, 없으면 직접 입력 (2026-11-03 하네스)
                return
        if err := AXUIElementSetAttributeValue(el, "AXValue", text):
            raise RuntimeError(f"cannot set text of {element_id or 'focused element'} (AXError {err})")
        if submit and AXUIElementPerformAction(el, "AXConfirm"):  # 텍스트 필드 · 콤보박스는 AXConfirm = Enter
            _, pid = AXUIElementGetPid(el, None)  # 그 외(텍스트 영역 등)는 Return 키를 그 앱에만 보낸다
            for down in (True, False):
                CGEventPostToPid(pid, CGEventCreateKeyboardEvent(None, KEY_RETURN, down))
            _wait(lambda: _value_of(el) != text)

    def _select(self, element_id: str, option: str) -> None:
        """목록을 열어(팝업은 자신, 콤보박스는 펼침 버튼을 AXPress) 항목을 고른다.

        콤보박스에 값만 넣고 AXConfirm하면 칸 글자만 바뀌고 앱에 반영되지 않는 경우가 있다(텍스트 편집기 서체 크기,
        2026-11-03 실측: 칸 18 · 글자 12) → 목록에 있는 값은 항목으로 고르고, 목록에 없는 값만 직접 입력한다.
        """
        el = self._el(element_id)
        self._front(el)  # 팝업 · 콤보박스 목록도 메뉴처럼 앱이 뒤에 있으면 열리지 않는다(2026-11-03 실측)
        combo = _attr(el, "AXRole") == "AXComboBox"
        item = _find_option(el, option)
        if item is None:
            opener = next((c for c in _attr(el, "AXChildren") or () if _attr(c, "AXRole") == "AXButton"), None)
            if not AXUIElementPerformAction(opener if combo and opener is not None else el, "AXPress"):
                _wait(lambda: _find_option(el, option) is not None)
                item = _find_option(el, option)
        if item is None and combo:  # 목록에 없는 값 — 직접 입력
            AXUIElementSetAttributeValue(el, "AXExpanded", False)
            if err := AXUIElementSetAttributeValue(el, "AXValue", option) or AXUIElementPerformAction(el, "AXConfirm"):
                raise RuntimeError(f"cannot set {element_id} to {option!r} (AXError {err})")
            return
        if item is None:
            raise LookupError(f"no option {option!r} in {element_id}")
        if err := _choose(item):
            raise RuntimeError(f"cannot choose option {option!r} (AXError {err})")
        closed = (lambda: not _attr(el, "AXExpanded")) if combo else (lambda: _menu_closed(el))
        _wait(lambda: _plain(str(_value_of(el) or "")) == _plain(option) and closed())  # 값 반영 · 목록 닫힘
