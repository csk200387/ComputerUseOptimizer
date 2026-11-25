# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""macOS AX 어댑터 — desktop CLI(agent-desktop parity)용 트리 읽기 · 요소 재식별.

기존 collectors/macos.py(비교 실험 MCP 서버용)와 달리 명령마다 새 프로세스에서 돌아도 같은 요소를 되찾는다:
요소마다 AX 원격 토큰(비공개 `_AXUIElementRemoteTokenCreate`)을 만들어 refmap에 저장하고 다음 프로세스가
`_AXUIElementCreateWithRemoteToken`으로 복원한다(DECISIONS D3). 토큰 = pid + 요소 id라 같은 요소면 같은 바이트.
"""

import ctypes
import os
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import objc
from AppKit import NSRunningApplication
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyMultipleAttributeValues,
    AXUIElementCopyParameterizedAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementIsAttributeSettable,
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
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionAll,
    kCGWindowListOptionOnScreenOnly,
)

from cuop.desktop.errors import CuopError
from cuop.schema import Bounds, Role, State, UIElementNode

_AX = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
try:
    _AX._AXUIElementRemoteTokenCreate.restype = ctypes.c_void_p
    _AX._AXUIElementRemoteTokenCreate.argtypes = [ctypes.c_void_p]
    _AX._AXUIElementCreateWithRemoteToken.restype = ctypes.c_void_p
    _AX._AXUIElementCreateWithRemoteToken.argtypes = [ctypes.c_void_p]
    HAS_TOKENS = True
except AttributeError:  # 비공개 API가 사라진 macOS — capture proof 경로만 남는다
    HAS_TOKENS = False
_AX._AXUIElementGetWindow.restype = ctypes.c_int32
_AX._AXUIElementGetWindow.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")

ATTRS = (
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXHelp", "AXPlaceholderValue",
    "AXEnabled", "AXFocused", "AXSelected", "AXExpanded", "AXPosition", "AXSize", "AXIdentifier",
    "AXDOMIdentifier", "AXRequired", "AXChildren",
)  # fmt: skip
MAX_NODES = 20000  # ponytail: 고정 상한 — 넘으면 truncated, 더 큰 트리는 --root로
PRIVATE_MENUS = frozenset(
    {"최근 사용 열기", "서비스", "공유", "Open Recent", "Services", "Share"}
)  # 하위 = 사용자 데이터

ROLES: dict[str, Role] = {
    "AXApplication": Role.APPLICATION, "AXButton": Role.BUTTON, "AXMenuButton": Role.MENUBUTTON,
    "AXTextField": Role.TEXTBOX, "AXTextArea": Role.TEXTBOX, "AXSearchField": Role.TEXTBOX,
    "AXSecureTextField": Role.TEXTBOX, "AXCheckBox": Role.CHECKBOX, "AXSwitch": Role.SWITCH, "AXToggle": Role.SWITCH,
    "AXLink": Role.LINK, "AXMenuItem": Role.MENUITEM, "AXMenuBarItem": Role.MENUITEM, "AXRadioButton": Role.RADIO,
    "AXTab": Role.TAB, "AXTabGroup": Role.TABLIST, "AXSlider": Role.SLIDER, "AXValueIndicator": Role.HANDLE,
    "AXComboBox": Role.COMBOBOX, "AXPopUpButton": Role.COMBOBOX, "AXOutlineRow": Role.TREEITEM, "AXRow": Role.ROW,
    "AXCell": Role.CELL, "AXColumn": Role.COLUMN, "AXWindow": Role.WINDOW, "AXSheet": Role.SHEET,
    "AXDialog": Role.DIALOG, "AXGroup": Role.GROUP, "AXGenericElement": Role.GROUP, "AXSplitGroup": Role.GROUP,
    "AXRadioGroup": Role.RADIOGROUP, "AXToolbar": Role.TOOLBAR, "AXStaticText": Role.TEXT, "AXImage": Role.IMAGE,
    "AXTable": Role.TABLE, "AXList": Role.LIST, "AXOutline": Role.TREE, "AXScrollArea": Role.SCROLLAREA,
    "AXScrollBar": Role.SCROLLBAR, "AXSplitter": Role.SPLITTER, "AXSeparator": Role.SEPARATOR, "AXMenu": Role.MENU,
    "AXMenuBar": Role.MENUBAR, "AXIncrementor": Role.INCREMENTOR, "AXStepper": Role.INCREMENTOR,
    "AXDisclosureTriangle": Role.DISCLOSURE, "AXProgressIndicator": Role.PROGRESSBAR,
    "AXBusyIndicator": Role.PROGRESSBAR, "AXColorWell": Role.COLORWELL, "AXWebArea": Role.WEBAREA,
    "AXBrowser": Role.BROWSER, "AXGrid": Role.GRID, "AXHandle": Role.HANDLE, "AXPopover": Role.POPOVER,
    "AXDockItem": Role.DOCKITEM, "AXRuler": Role.RULER, "AXRulerMarker": Role.RULERMARKER,
    "AXTimeField": Role.TIMEFIELD, "AXDateField": Role.DATEFIELD, "AXHelpTag": Role.HELPTAG, "AXMatte": Role.MATTE,
    "AXDrawer": Role.DRAWER, "AXLayoutArea": Role.LAYOUTITEM, "AXLayoutItem": Role.LAYOUTITEM,
    "AXLevelIndicator": Role.LEVELINDICATOR, "AXRelevanceIndicator": Role.RELEVANCEINDICATOR,
    "AXDocument": Role.DOCUMENT, "AXPage": Role.PAGE, "AXHeading": Role.HEADING, "AXParagraph": Role.PARAGRAPH,
    "AXStatus": Role.STATUS, "AXToolTip": Role.TOOLTIP,
}  # fmt: skip
SUBROLES: dict[str, Role] = {
    "AXApplicationAlert": Role.ALERT, "AXApplicationAlertDialog": Role.ALERTDIALOG,
    "AXApplicationDialog": Role.DIALOG, "AXApplicationLog": Role.LOG, "AXApplicationMarquee": Role.MARQUEE,
    "AXApplicationStatus": Role.STATUS, "AXApplicationTimer": Role.TIMER, "AXDocumentArticle": Role.ARTICLE,
    "AXDocumentMath": Role.MATH, "AXDocumentNote": Role.NOTE, "AXDocumentRegion": Role.REGION,
    "AXLandmarkBanner": Role.BANNER, "AXLandmarkComplementary": Role.COMPLEMENTARY,
    "AXLandmarkContentInfo": Role.CONTENTINFO, "AXLandmarkForm": Role.FORM, "AXLandmarkMain": Role.MAIN,
    "AXLandmarkNavigation": Role.NAVIGATION, "AXLandmarkSearch": Role.SEARCH, "AXDefinition": Role.DEFINITION,
    "AXTerm": Role.TERM, "AXTabPanel": Role.TABPANEL, "AXUserInterfaceTooltip": Role.TOOLTIP,
    "AXOutlineRow": Role.TREEITEM, "AXTableRow": Role.ROW, "AXDialog": Role.DIALOG, "AXSystemDialog": Role.DIALOG,
    "AXToggle": Role.SWITCH, "AXSwitch": Role.SWITCH, "AXContentList": Role.LIST, "AXDefinitionList": Role.LIST,
    "AXDescriptionList": Role.LIST, "AXSeparatorDockItem": Role.SEPARATOR, "AXTabButton": Role.TAB,
}  # fmt: skip
WINDOW_BUTTONS = {
    "AXCloseButton": "Close", "AXMinimizeButton": "Minimize", "AXZoomButton": "Zoom",
    "AXFullScreenButton": "Full Screen",
}  # fmt: skip
# agent-desktop INTERACTIVE_ROLES (+ scrollarea · disclosure: scroll · expand에 ref가 필요)
INTERACTIVE = frozenset({
    Role.BUTTON, Role.CELL, Role.CHECKBOX, Role.COLORWELL, Role.COMBOBOX, Role.DOCKITEM, Role.INCREMENTOR,
    Role.LINK, Role.LISTBOX, Role.MENUBUTTON, Role.MENUITEM, Role.OPTION, Role.RADIO, Role.SLIDER, Role.SWITCH,
    Role.TAB, Role.TEXTBOX, Role.TREEITEM, Role.SCROLLAREA, Role.DISCLOSURE, Role.DATEFIELD, Role.TIMEFIELD,
})  # fmt: skip
PRIMARY = ("AXPress", "AXOpen", "AXConfirm")
EXPANDABLE = frozenset({Role.COMBOBOX, Role.DISCLOSURE, Role.TREEITEM, Role.ROW, Role.MENUBUTTON, Role.CELL})
TEXT_ROLES = frozenset({Role.TEXTBOX, Role.COMBOBOX, Role.DATEFIELD, Role.TIMEFIELD})
# 액션 이름(AXPress 등)을 읽을 가치가 있는 role — 그 밖의 노드는 IPC를 아끼려 건너뛴다
ACTION_PROBE = INTERACTIVE | {Role.ROW, Role.IMAGE, Role.TEXT, Role.GROUP, Role.LISTITEM, Role.HEADING, Role.TABLIST}


def plain(s: str) -> str:
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")


def decode(v: Any) -> Any:
    """AXValueRef: 누락 속성(에러 값)은 None, CGPoint/CGSize는 튜플."""
    if not isinstance(v, AXValueRef):
        return v
    kind = AXValueGetType(v)
    if kind not in (kAXValueCGPointType, kAXValueCGSizeType):
        return None
    _, p = AXValueGetValue(v, kind, None)
    return (p.x, p.y) if kind == kAXValueCGPointType else (p.width, p.height)


def attr(el: Any, name: str) -> Any:
    err, v = AXUIElementCopyAttributeValue(el, name, None)
    return None if err else decode(v)


def text(v: Any) -> str | None:
    if isinstance(v, str):
        s = plain(v).strip()
        return s or None
    return None


def value_text(v: Any) -> str | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return f"{v:g}" if isinstance(v, float) else str(v)
    return text(v)


# --- 토큰 · 창 번호 ---
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CF.CFRelease.argtypes = [ctypes.c_void_p]


def _owned(ptr: int) -> Any:
    """Create 함수가 돌려준 +1 참조 → PyObjC 프록시(자기 참조를 잡는다)를 만들고 원래 참조는 놓는다."""
    obj = objc.objc_object(c_void_p=ptr)
    _CF.CFRelease(ptr)
    return obj


def token(el: Any) -> str | None:
    if not HAS_TOKENS:
        return None
    ptr = _AX._AXUIElementRemoteTokenCreate(objc.pyobjc_id(el))
    return bytes(_owned(ptr)).hex() if ptr else None


def from_token(hexstr: str) -> Any:
    if not HAS_TOKENS:
        return None
    from Foundation import NSData

    raw = bytes.fromhex(hexstr)
    data = NSData.dataWithBytes_length_(raw, len(raw))  # 호출이 끝날 때까지 살아 있게 변수로 잡는다
    ptr = _AX._AXUIElementCreateWithRemoteToken(objc.pyobjc_id(data))
    return _owned(ptr) if ptr else None


class _BSDInfo(ctypes.Structure):  # struct proc_bsdinfo (sys/proc_info.h) — 시작 시각만 쓴다
    _fields_ = [("pad", ctypes.c_uint32 * 30), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def process_start(pid: int) -> int | None:
    """프로세스 세대 토큰(시작 시각 ms) — 재사용된 pid가 다른 앱을 가리키지 않게."""
    info = _BSDInfo()
    n = _LIBPROC.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))  # PROC_PIDTBSDINFO
    return info.start_sec * 1000 + info.start_usec // 1000 if n == ctypes.sizeof(info) else None


def window_number(el: Any) -> int | None:
    if el is None:
        return None
    wid = ctypes.c_uint32(0)
    return wid.value if _AX._AXUIElementGetWindow(objc.pyobjc_id(el), ctypes.byref(wid)) == 0 and wid.value else None


def pid_of(el: Any) -> int:
    return int(AXUIElementGetPid(el, None)[1])


def trusted() -> bool:
    return bool(AXIsProcessTrusted())


def screen_recording() -> bool:
    """권한 창을 띄우지 않고 확인만 한다."""
    from Quartz import CGPreflightScreenCaptureAccess

    return bool(CGPreflightScreenCaptureAccess())


def displays() -> list[dict[str, Any]]:
    """주 디스플레이 먼저. scale = 기기 픽셀 / point (Retina 2.0)."""
    from Quartz import (
        CGDisplayBounds,
        CGDisplayCopyDisplayMode,
        CGDisplayModeGetPixelWidth,
        CGGetActiveDisplayList,
        CGMainDisplayID,
    )

    _, ids, _ = CGGetActiveDisplayList(16, None, None)
    main = CGMainDisplayID()
    rows = []
    for d in sorted(ids or (), key=lambda d: d != main):
        r = CGDisplayBounds(d)
        mode = CGDisplayCopyDisplayMode(d)
        sc = CGDisplayModeGetPixelWidth(mode) / r.size.width if mode and r.size.width else 1.0
        rows.append({
            "id": int(d), "is_primary": d == main, "scale": round(sc, 2),
            "bounds": {"x": round(r.origin.x), "y": round(r.origin.y),
                       "width": round(r.size.width), "height": round(r.size.height)},
        })  # fmt: skip
    return rows


def set_timeout(seconds: float) -> None:
    AXUIElementSetMessagingTimeout(AXUIElementCreateSystemWide(), seconds)


# --- 앱 · 창 목록 ---
@dataclass
class App:
    pid: int
    name: str
    bundle_id: str | None
    policy: int  # 0 regular · 1 accessory · 2 prohibited
    path: str | None
    executable: str | None

    @property
    def presentation(self) -> str | None:
        return {0: "foreground", 1: "background"}.get(self.policy)

    def matches(self, query: str) -> bool:
        q = query.strip().lower()
        names = {self.name, self.bundle_id or "", (self.bundle_id or "").rsplit(".", 1)[-1], self.executable or ""}
        if self.path:
            names.add(Path(self.path).stem)
        return q in {n.lower() for n in names if n}


def all_pids() -> list[int]:
    n = _LIBPROC.proc_listallpids(None, 0)
    buf = (ctypes.c_int * (n + 64))()
    got = _LIBPROC.proc_listallpids(buf, ctypes.sizeof(buf))
    return [p for p in buf[:got] if p > 0]


def running_apps() -> list[App]:
    """LaunchServices에 등록된 GUI 앱(regular · accessory). NSWorkspace 목록은 런루프 없는 프로세스에서 갱신되지
    않아(CLAUDE.md §5) pid마다 NSRunningApplication을 새로 묻는다."""
    out: list[App] = []
    for pid in all_pids():
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if ra is None or ra.isTerminated():
            continue
        policy = int(ra.activationPolicy())
        url, exe = ra.bundleURL(), ra.executableURL()
        out.append(
            App(
                pid=pid,
                name=str(ra.localizedName() or (exe.lastPathComponent() if exe else pid)),
                bundle_id=str(ra.bundleIdentifier()) if ra.bundleIdentifier() else None,
                policy=policy,
                path=str(url.path()) if url else None,
                executable=str(exe.lastPathComponent()) if exe else None,
            )
        )
    return sorted(out, key=lambda a: (a.policy, a.name.lower(), a.pid))


def _permitted(pid: int) -> bool:
    try:
        allowed(pid)
    except CuopError:
        return False
    return True


def find_app(query: str) -> App:
    # 허용 목록 밖의 앱은 후보에도 넣지 않는다 — AMBIGUOUS_TARGET 후보 이름 · 창 제목으로 새지 않게(Codex 리뷰)
    hits = [a for a in running_apps() if a.matches(query) and _permitted(a.pid)]
    if not hits:
        raise CuopError("APP_NOT_FOUND", f"no running application matches {query!r}", details={"app": query})
    regular = [a for a in hits if a.policy == 0] or hits
    if len(regular) > 1 and len({a.bundle_id for a in regular}) == 1:
        return min(
            regular, key=lambda a: a.pid
        )  # ponytail: 같은 앱 여러 인스턴스 → 가장 오래된 것, --window-id로 고른다
    if len(regular) > 1:
        raise CuopError(
            "AMBIGUOUS_TARGET",
            f"{len(regular)} applications match {query!r}",
            details={"candidates": [{"name": a.name, "pid": a.pid, "bundle_id": a.bundle_id} for a in regular]},
        )
    return regular[0]


def app_by_pid(pid: int) -> App | None:
    return next((a for a in running_apps() if a.pid == pid), None)


WINDOW_LAYERS = (0, 8)


def cg_windows(on_screen: bool = False) -> list[dict[str, Any]]:
    opts = (
        kCGWindowListOptionOnScreenOnly if on_screen else kCGWindowListOptionAll
    ) | kCGWindowListExcludeDesktopElements
    # 일반 창(0) + 모달 패널(8, kCGModalPanelWindowLevel): 샌드박스 앱(Word)의 열기 · 저장 패널은 레이어 8이라 빠지면
    # AX에는 있는데 '화면에 없음'이 되어 window_id · 캡처 · 기본 창 선택이 모두 못 찾았다(CODEX_CU 시험 run 65334bc1)
    return [
        dict(w)
        for w in CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or ()
        if w.get("kCGWindowLayer") in WINDOW_LAYERS
    ]


def frontmost_pid() -> int | None:
    """맨 앞 앱 = 화면에 보이는 layer-0 창 중 첫 창의 주인 중 일반 앱(앞→뒤 순, CLAUDE.md §5).
    액세서리 앱의 투명 오버레이 창(화면 녹화 · 자동화 도구)이 맨 앞에 있어도 건너뛴다."""
    for w in cg_windows(on_screen=True):
        if w.get("kCGWindowLayer") != 0:  # 모달 패널(8)은 그 앱의 창 판정에만 — 맨 앞 앱은 일반 창으로
            continue
        pid = int(w["kCGWindowOwnerPID"])
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if ra is not None and int(ra.activationPolicy()) == 0:
            return pid
    return None


def allowed(pid: int) -> None:
    """하네스 허용 목록(`CUOP_ALLOW_APPS` = 번들 ID 쉼표 목록): 정해 두면 그 밖의 앱은 읽지도 조작하지도 않는다 —
    실험 중 화면의 다른 앱(메신저 등)이 관찰로 모델에 가지 않게(M9 Codex 리뷰 #1). 비어 있으면 제한 없음."""
    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    allowed_bundle(str(ra.bundleIdentifier() or "") if ra is not None else "")


def allow_list() -> set[str] | None:
    raw = os.environ.get("CUOP_ALLOW_APPS", "").strip()
    return {x.strip() for x in raw.split(",")} if raw else None


def allowed_bundle(bid: str | None) -> None:
    allow = allow_list()
    if allow is not None and (bid or "") not in allow:
        raise CuopError("POLICY_DENIED", "this app is outside the allowed apps for this session",
                        details={"kind": "app_not_allowed"})  # fmt: skip


def app_element(pid: int) -> Any:
    return AXUIElementCreateApplication(pid)


def is_frontmost(pid: int) -> bool:
    return attr(AXUIElementCreateApplication(pid), "AXFrontmost") is True


@dataclass
class Window:
    el: Any
    id: int | None
    title: str | None
    pid: int
    bounds: Bounds | None
    minimized: bool
    focused: bool
    on_screen: bool
    subrole: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ref_id(self) -> str | None:
        return f"w-{self.id}" if self.id else None


def bounds_of(pos: Any, size: Any) -> Bounds | None:
    import math

    if isinstance(pos, tuple) and isinstance(size, tuple) and all(math.isfinite(v) for v in (*pos, *size)):
        return Bounds(x=round(pos[0]), y=round(pos[1]), w=round(size[0]), h=round(size[1]))
    return None


def app_windows(pid: int) -> list[Window]:
    app = AXUIElementCreateApplication(pid)
    wins = attr(app, "AXWindows") or ()
    focused = attr(app, "AXFocusedWindow")
    onscreen = {int(w["kCGWindowNumber"]) for w in cg_windows(on_screen=True) if int(w["kCGWindowOwnerPID"]) == pid}
    out: list[Window] = []
    for w in wins:
        err, vals = AXUIElementCopyMultipleAttributeValues(
            w, ("AXTitle", "AXPosition", "AXSize", "AXMinimized", "AXSubrole"), 0, None
        )
        title, pos, size, mini, sub = [decode(v) for v in vals] if not err else [None] * 5
        wid = window_number(w)
        out.append(
            Window(
                el=w,
                id=wid,
                title=text(title),
                pid=pid,
                bounds=bounds_of(pos, size),
                minimized=mini is True,
                focused=focused is not None and focused == w,
                on_screen=wid in onscreen,
                subrole=sub if isinstance(sub, str) else None,
            )
        )
    return out


def pick_window(pid: int, app_name: str) -> Window | None:
    """창 하나 고르기: 보이는 창이 하나면 그것, 아니면 포커스된 창, 둘 다 안 되면 AMBIGUOUS_TARGET. 창이 없으면 None."""
    wins = app_windows(pid)
    visible = [w for w in wins if w.on_screen and not w.minimized]
    if len(visible) == 1:
        return visible[0]
    if focused := [w for w in visible if w.focused]:
        return focused[0]
    if not visible:
        return None
    if len(docs := [w for w in visible if w.subrole == "AXStandardWindow"]) == 1:
        return docs[0]  # 포커스를 모를 때: 대화상자(모달 패널) 말고 문서 창이 하나뿐이면 그 창
    raise CuopError(
        "AMBIGUOUS_TARGET",
        f"{app_name} has {len(visible)} visible windows; pass --window-id",
        details={"candidates": [{"id": w.ref_id, "title": w.title} for w in visible]},
    )


def window_by_id(wid: int, pid: int | None = None) -> Window:
    pids = [pid] if pid else sorted({int(w["kCGWindowOwnerPID"]) for w in cg_windows() if w["kCGWindowNumber"] == wid})
    for p in pids:
        allowed(p)  # 허용 목록 밖 창은 AX로 읽기 전에 거부
        for w in app_windows(p):
            if w.id == wid:
                return w
    raise CuopError("WINDOW_NOT_FOUND", f"no accessible window w-{wid}", details={"window_id": f"w-{wid}"})


# --- Chromium / Electron ---
def renderer(app: App) -> str | None:
    """번들 안 프레임워크로 판별: Electron · CEF, 또는 'X Framework'에 Chromium 렌더러 도우미(`… (Renderer).app`)가
    있으면 'chromium'(Chrome · Edge · ChatGPT 등). 없다고 네이티브라는 증거는 아니다."""
    if not app.path:
        return None
    fw = Path(app.path) / "Contents" / "Frameworks"
    try:
        names = [p.name for p in fw.iterdir()]
    except OSError:
        return None
    if any(n.startswith(("Electron Framework", "Chromium Embedded Framework")) for n in names):
        return "chromium"
    for n in names:
        if n.endswith(" Framework.framework") and any((fw / n / "Versions/Current/Helpers").glob("*Renderer*.app")):
            return "chromium"
    return None


def enable_web_accessibility(pid: int) -> str | None:
    """Electron은 AXManualAccessibility, Chrome 계열은 AXEnhancedUserInterface를 켠다(CLAUDE.md §5). 켠 속성 이름."""
    app = AXUIElementCreateApplication(pid)
    for name in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        if attr(app, name) is True:
            return name
        if AXUIElementSetAttributeValue(app, name, True) == 0:
            return name
    return None


# --- 트리 읽기 ---
@dataclass
class Walk:
    """한 번의 트리 읽기 상태. nodes는 전위 순서의 (노드 원본, AX 요소) — ref 발급 · 토큰 계산에 쓴다."""

    deadline: float
    max_depth: int
    count: int = 0
    complete: bool = True
    seen: set[str] = field(default_factory=lambda: set[str]())  # 토큰 — 순환 트리 방지
    elements: list[Any] = field(default_factory=list)  # UIElementNode.id 순서(임시 id = 인덱스)와 같은 순서
    tokens: list[str | None] = field(default_factory=list)


def _actions(el: Any) -> list[str]:
    err, names = AXUIElementCopyActionNames(el, None)
    return [str(n) for n in names or ()] if not err else []


def _advertises(el: Any, name: str) -> bool:
    err, names = AXUIElementCopyAttributeNames(el, None)
    return bool(err) or name in (names or ())  # 못 읽으면 내놓은 것으로(앱 값을 그대로 믿는다)


def _settable(el: Any, name: str) -> bool:
    err, ok = AXUIElementIsAttributeSettable(el, name, None)
    return not err and bool(ok)


def capabilities(role: Role, ax_actions: list[str], el: Any, value: Any) -> list[str]:
    """agent-desktop capability 어휘(Click · RightClick · Scroll · SetValue · Select · Expand · Collapse …)."""
    caps: list[str] = []
    if any(a in ax_actions for a in PRIMARY):
        caps.append("Click")
        if role in (Role.CHECKBOX, Role.SWITCH, Role.RADIO):
            caps.append("Toggle")
    if "AXShowMenu" in ax_actions and role not in (Role.TEXT, Role.GROUP):
        caps.append("RightClick")
    if "AXScrollToVisible" in ax_actions:
        caps.append("ScrollTo")
    if role is Role.SCROLLAREA:
        caps.append("Scroll")
    settable = role in TEXT_ROLES | {Role.SLIDER, Role.INCREMENTOR} and _settable(el, "AXValue")
    if "AXIncrement" in ax_actions or "AXDecrement" in ax_actions or settable:
        caps.append("SetValue")
    if role in (Role.COMBOBOX, Role.LISTBOX, Role.LIST, Role.TABLE, Role.TREE):
        caps.append("Select")
    if role in TEXT_ROLES and _settable(el, "AXFocused"):
        caps.append("SetFocus")
    if role in (Role.DISCLOSURE, Role.TREEITEM, Role.ROW) and _settable(el, "AXExpanded"):
        caps += ["Expand", "Collapse"]
    del value
    return caps


def _transparent(role: Role, name: str | None, value: str | None, native: str | None, caps: list[str]) -> bool:
    """Web wrapper: 이름 · 값 · 식별자 · (scroll-to · 메뉴 말고) 액션이 없는 group — 논리 깊이를 쓰지 않는다."""
    return role in (Role.GROUP, Role.GENERIC) and not (
        name or value or native or set(caps) - {"ScrollTo", "RightClick"}
    )


def read_tree(root: Any, *, max_depth: int = 10, timeout_ms: int = 3000) -> tuple[UIElementNode, Walk]:
    """root부터 전위 순회. 노드 id는 임시 "#i"(Walk.elements 인덱스) — 호출자가 안정 번호로 바꾼다."""
    walk = Walk(deadline=time.monotonic() + timeout_ms / 1000, max_depth=max_depth)
    set_timeout(min(1.0, max(0.05, timeout_ms / 1000)))  # 무응답 앱의 IPC 하나가 예산을 다 먹지 않게(전역)
    node = _walk(root, 0, walk, 0)
    assert node is not None
    return node, walk


MAX_PHYSICAL = 200  # wrapper는 논리 깊이를 쓰지 않으니 실제 깊이에도 상한


def _walk(el: Any, depth: int, w: Walk, physical: int) -> UIElementNode | None:
    err, values = AXUIElementCopyMultipleAttributeValues(el, ATTRS, 0, None)
    vals = [decode(v) for v in values] if not err and values else [None] * len(ATTRS)
    if err and err != -25212:  # -25212 kAXErrorNoValue만 '없음', 나머지(응답 없음 · 사라짐)는 다 못 읽은 것
        w.complete = False
    (
        raw,
        sub,
        title,
        desc,
        value,
        help_,
        ph,
        enabled,
        focused,
        selected,
        expanded,
        pos,
        size,
        ident,
        dom_id,
        req,
        kids,
    ) = vals
    if raw == "AXMenuBarItem" and title == "Apple":  # 시스템 메뉴 · 사용자 이름 — 모든 모드에서 뺀다(CLAUDE.md §5)
        return None
    role = SUBROLES[sub] if isinstance(sub, str) and sub in SUBROLES else ROLES.get(str(raw), Role.GENERIC)
    if raw == "AXButton" and role is Role.GENERIC:
        role = Role.BUTTON
    secure = "AXSecureTextField" in (raw, sub)
    name = text(title) or text(desc)
    if role is Role.BUTTON and not name and isinstance(sub, str):
        name = WINDOW_BUTTONS.get(sub)
    val = None if secure else value_text(value)
    if role is Role.TEXT and not name:
        name, val = val, None
    # ponytail: 짧고 숫자가 든 글자만 서식을 본다(노드마다 IPC 1회) — 계산기 2¹⁰이 접근성 텍스트로는 '210'이라
    # 에이전트가 xʸ가 안 눌린 줄 알았다(2026-11-03 · M9 시험 scicalc 33턴, Codex 분석) → 작은 글씨 구간을 ^(…)로
    if role is Role.TEXT and name and len(name) <= 32 and any(ch.isdigit() for ch in name):
        name = superscripts(el, name) or name
    states: set[State] = set()
    if enabled is False:
        states.add(State.DISABLED)
    if focused is True:
        states.add(State.FOCUSED)
    if selected is True:
        states.add(State.SELECTED if role is not Role.TAB else State.SELECTED)
    # Chromium은 거의 모든 노드에 AXExpanded=false를 단다 → collapsed는 펼칠 수 있는 role에만(잡음 방지)
    if expanded is True and role not in (Role.MENUBAR, Role.MENU, Role.MENUITEM):
        states.add(State.EXPANDED)
    elif expanded is False and role in EXPANDABLE:
        states.add(State.COLLAPSED)
    if (
        role in (Role.CHECKBOX, Role.SWITCH, Role.RADIO, Role.TAB)
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        if value == 1:
            states.add(State.SELECTED if role is Role.TAB else State.CHECKED)
        elif value == 2:
            states.add(State.INDETERMINATE)
        val = None
    elif role in (Role.CHECKBOX, Role.SWITCH, Role.RADIO) and value is True:
        states.add(State.CHECKED)
    if raw == "AXButton" and sub in ("AXToggle", "AXToggleButton"):
        role = Role.BUTTON  # 토글 버튼은 button + pressed (agent-desktop macOS 매핑)
        if value in (1, True):
            states.add(State.PRESSED)
    if secure:
        states.add(State.SECURE)
    if req is True:
        states.add(State.REQUIRED)
    native = text(ident) or text(dom_id)
    ax_actions = _actions(el) if role in ACTION_PROBE else []
    caps = capabilities(role, ax_actions, el, value) if ax_actions or role in INTERACTIVE else []
    if (
        State.DISABLED in states
        and (
            (raw == "AXTextArea" and "SetValue" in caps and "SetFocus" in caps)
            or (raw == "AXLayoutArea" and _settable(el, "AXFocused"))
        )
        and not _advertises(el, "AXEnabled")
    ):  # Office 문서 영역(Word 본문 · PowerPoint 슬라이드): 내놓지 않은 AXEnabled를 false로 주면서 입력은 받는다(D15)
        states.discard(State.DISABLED)
    tok = token(el)
    if tok is not None and tok in w.seen:
        return None  # 이미 본 요소(순환) — 다시 내려가지 않는다
    if tok is not None:
        w.seen.add(tok)
    w.count += 1
    idx = len(w.elements)
    w.elements.append(el)
    w.tokens.append(tok)
    children: list[UIElementNode] = []
    child_list = list(kids or ()) if not (raw == "AXMenuItem" and title in PRIVATE_MENUS) else []
    logical = depth if _transparent(role, name, val, native, caps) else depth + 1
    truncated, count = False, None
    if child_list and (logical > w.max_depth or physical >= MAX_PHYSICAL):
        count = len(child_list)  # 깊이 제한 — 몇 개를 건너뛰었는지 안다
    else:
        for child in child_list:
            if time.monotonic() > w.deadline or w.count >= MAX_NODES:
                truncated, w.complete = True, False
                break
            if (node := _walk(child, logical, w, physical + 1)) is not None:
                children.append(node)
    if err and err != -25212:
        truncated = True
    return UIElementNode(
        id=f"#{idx}",
        role=role,
        raw_role=str(raw or "?") + (f"/{sub}" if isinstance(sub, str) and sub != "AXUnknown" else ""),
        name=name,
        value=val,
        description=text(ph) or text(help_) if role is not Role.TEXT else None,
        bounds=bounds_of(pos, size),
        states=frozenset(states),
        children=children,
        ref=role in INTERACTIVE or any(a in ax_actions for a in PRIMARY),
        actions=tuple(caps),
        native_id=native,
        children_count=count,
        truncated=truncated,
    )


# --- 재식별 ---
def mark_superscripts(runs: list[tuple[str, float]]) -> str | None:
    """글자 구간 중 눈에 띄게 작은 글씨(가장 큰 글씨의 85% 미만)를 위첨자로 보고 ^(…)로. 구간이 하나면 None."""
    runs = [(seg, size) for seg, size in runs if seg]
    if len(runs) < 2:
        return None
    big = max(size for _, size in runs)
    small = [0 < size < big * 0.85 for _, size in runs]
    return "".join(f"^({seg})" if sm else seg for (seg, _), sm in zip(runs, small, strict=True)) if any(small) else None


def superscripts(el: Any, label: str) -> str | None:
    raw = attr(el, "AXValue")  # 서식 문자(LRM)가 든 원문 — label은 그것을 뺀 글자라 짧아 뒤 구간(지수)이 잘린다
    n = len((raw if isinstance(raw, str) else label).encode("utf-16-le")) // 2  # 범위는 UTF-16 단위
    err, s = AXUIElementCopyParameterizedAttributeValue(el, "AXAttributedStringForRange", AXValueCreate(
        kAXValueCFRangeType, (0, n)), None)  # fmt: skip
    runs: list[tuple[str, float]] = []
    i = 0
    while not err and s is not None and i < s.length():
        attrs, r = s.attributesAtIndex_effectiveRange_(i, None)
        seg = plain(str(s.string().substringWithRange_(r)))  # 오프셋은 UTF-16 단위 — 파이썬 글자로 자르지 않는다
        runs.append((seg, float((attrs.get("AXFont") or {}).get("AXFontSize") or 0)))
        i = r.location + r.length
    return mark_superscripts(runs)


def resolve(tok: str | None, pid: int, raw_role: str, native_id: str | None) -> Any:
    """저장한 토큰으로 요소를 되찾고 pid · role · identifier가 그대로인지 확인한다 — 다르면 STALE_REF."""
    if tok is None:
        return None
    el = from_token(tok)
    if el is None:
        raise CuopError("STALE_REF", "the element's recorded address is gone", details=_stale("dead"))
    err, vals = AXUIElementCopyMultipleAttributeValues(
        el, ("AXRole", "AXSubrole", "AXIdentifier", "AXDOMIdentifier"), 0, None
    )
    role, sub, ident, dom = [decode(v) for v in vals] if not err and vals else [None] * 4
    if role is None:
        raise CuopError("STALE_REF", "the element's recorded address is dead", details=_stale("dead"))
    if pid_of(el) != pid:
        raise CuopError("STALE_REF", "the element now belongs to another process", details=_stale("pid"))
    if str(role) != raw_role.split("/", 1)[0]:
        raise CuopError("STALE_REF", f"the element's role changed to {role}", details=_stale("role"))
    if native_id is not None and native_id not in (text(ident), text(dom)):
        raise CuopError("STALE_REF", "the element's identifier changed", details=_stale("native_identifier"))
    del sub
    return el


def _stale(reason: str) -> dict[str, Any]:
    return {"kind": "retained_object_stale", "reason": reason, "retryable": False}
