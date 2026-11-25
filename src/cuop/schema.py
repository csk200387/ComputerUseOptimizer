"""계층 간 공통 계약 — 모든 수집기는 네이티브 UI 트리를 이 스키마로 정규화한다."""

from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

Platform = Literal["macos", "web", "windows"]
Mode = Literal["screenshot", "full", "compact", "proposed"]  # 비교군 ①②③④
FallbackReason = Literal["empty_tree", "target_missing", "no_diff", "visual_region", "judge"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Role(StrEnum):
    """ARIA 기반 공통 role. 매핑할 수 없으면 GENERIC (원본은 raw_role에 보존)."""

    WINDOW = "window"
    DIALOG = "dialog"
    MENUBAR = "menubar"
    MENU = "menu"
    MENUITEM = "menuitem"
    TOOLBAR = "toolbar"
    TABLIST = "tablist"
    TAB = "tab"
    BUTTON = "button"
    LINK = "link"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    TEXTBOX = "textbox"
    COMBOBOX = "combobox"
    LISTBOX = "listbox"
    OPTION = "option"
    SLIDER = "slider"
    LIST = "list"
    LISTITEM = "listitem"
    TABLE = "table"
    ROW = "row"
    CELL = "cell"
    TREE = "tree"
    TREEITEM = "treeitem"
    HEADING = "heading"
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    CANVAS = "canvas"  # 캔버스 · 3D 뷰포트 → Fallback 판정 신호
    GROUP = "group"
    GENERIC = "generic"
    # agent-desktop 공통 role 어휘(2.6) 중 위에 없는 것 — desktop CLI 수집기(collectors/ax.py)가 쓴다.
    # 기존 값(text · textbox · radio · tree · generic)은 JSON 출력에서 AD_ROLE로 바꿔 쓴다.
    ALERT = "alert"
    ALERTDIALOG = "alertdialog"
    APPLICATION = "application"
    ARTICLE = "article"
    BANNER = "banner"
    BROWSER = "browser"
    COLORWELL = "colorwell"
    COLUMN = "column"
    COMPLEMENTARY = "complementary"
    CONTENTINFO = "contentinfo"
    DATEFIELD = "datefield"
    DEFINITION = "definition"
    DISCLOSURE = "disclosure"
    DOCKITEM = "dockitem"
    DRAWER = "drawer"
    FORM = "form"
    GRID = "grid"
    HANDLE = "handle"
    HELPTAG = "helptag"
    INCREMENTOR = "incrementor"
    LAYOUTITEM = "layoutitem"
    LEVELINDICATOR = "levelindicator"
    LOG = "log"
    MAIN = "main"
    MARQUEE = "marquee"
    MATH = "math"
    MATTE = "matte"
    MENUBUTTON = "menubutton"
    NAVIGATION = "navigation"
    NOTE = "note"
    PAGE = "page"
    PARAGRAPH = "paragraph"
    POPOVER = "popover"
    PROGRESSBAR = "progressbar"
    RADIOGROUP = "radiogroup"
    REGION = "region"
    RELEVANCEINDICATOR = "relevanceindicator"
    RULER = "ruler"
    RULERMARKER = "rulermarker"
    SCROLLAREA = "scrollarea"
    SCROLLBAR = "scrollbar"
    SEARCH = "search"
    SEPARATOR = "separator"
    SHEET = "sheet"
    SPLITTER = "splitter"
    STATUS = "status"
    SWITCH = "switch"
    TABPANEL = "tabpanel"
    TERM = "term"
    TIMEFIELD = "timefield"
    TIMER = "timer"
    TOOLTIP = "tooltip"
    WEBAREA = "webarea"


class State(StrEnum):
    FOCUSED = "focused"
    DISABLED = "disabled"
    SELECTED = "selected"
    CHECKED = "checked"
    EXPANDED = "expanded"
    COLLAPSED = "collapsed"
    EDITABLE = "editable"
    FOCUSABLE = "focusable"
    CLICKABLE = "clickable"
    OFFSCREEN = "offscreen"
    REQUIRED = "required"
    READONLY = "readonly"
    MODAL = "modal"
    # agent-desktop state 어휘 중 위에 없는 것
    BUSY = "busy"
    HASPOPUP = "haspopup"
    HIDDEN = "hidden"
    INDETERMINATE = "indeterminate"
    INVALID = "invalid"
    MULTISELECTABLE = "multiselectable"
    PRESSED = "pressed"
    SECURE = "secure"


def _sorted_states(states: frozenset[State]) -> list[State]:
    return sorted(states)


# frozenset 순회 순서는 실행마다 달라지는 해시를 따르므로, JSON에서는 정렬해 결정적으로 직렬화
States = Annotated[frozenset[State], PlainSerializer(_sorted_states, when_used="json")]


class Bounds(_Model):
    """macOS·Windows: 화면 point / Web: viewport CSS px."""

    x: int
    y: int
    w: int
    h: int


class UIElementNode(_Model):
    id: str  # "e42" — 수집기가 발급하는 세션 내 안정 ID
    role: Role
    raw_role: str  # "AXButton" / "button" / "Button" — 디버깅·Fallback용, LLM에는 보내지 않음
    name: str | None = None
    value: str | None = None  # 체크 여부 등은 states로
    description: str | None = None  # help · placeholder
    bounds: Bounds | None = None  # Diff 비교 제외(스크롤 노이즈), 좌표 Fallback에만 사용
    states: States = frozenset()
    children: list[UIElementNode] = []
    omitted: int = 0  # Task-aware Filter가 뺀 자손 수 — 직렬화 시 "… +N" 한 줄, Diff 비교 제외
    # desktop CLI(agent-desktop parity) 전용 — 기존 수집기 · fixture는 기본값
    ref: bool = False  # 이 스냅샷에서 ref(@sid:id)를 받은 노드 — 상호작용 role이거나 주 액션을 광고
    actions: tuple[str, ...] = ()  # available_actions (Click · SetValue …)
    native_id: str | None = None  # AXIdentifier / AXDOMIdentifier
    children_count: int | None = None  # 깊이 · skeleton으로 잘린 노드의 실제 자식 수
    truncated: bool = False  # 시간 예산이 다 돼 자손을 다 못 읽음(subtree_truncated)

    def walk(self) -> Iterator[UIElementNode]:
        """자신을 포함한 전위 순회."""
        yield self
        for child in self.children:
            yield from child.walk()


class UISnapshot(_Model):
    id: str  # "s7" — 단조 증가
    platform: Platform
    app: str  # bundle id / URL / exe
    window: str | None = None
    root: UIElementNode  # 앱(문서) 요소 — 직렬화 시 헤더로만 표시


class NodeChange(_Model):
    """에이전트가 이미 아는 노드의 변화. 바뀐 필드만 채운다."""

    id: str
    name: tuple[str | None, str | None] | None = None  # (before, after)
    value: tuple[str | None, str | None] | None = None
    states_on: States = frozenset()
    states_off: States = frozenset()
    omitted: tuple[int, int] | None = None  # 숨은 자식 수(`… +N`)가 바뀜 — 에이전트가 가진 +N이 낡지 않게


class AddedNode(_Model):
    parent_id: str | None  # 어디에 붙었는지 (예: 창 아래 새 dialog)
    node: UIElementNode  # 필터 적용된 서브트리 루트
    after: str | None = None  # 기존 형제 사이에 끼었을 때 바로 앞 형제 id("" = 맨 앞). 끝에 붙었으면 None


class Reorder(_Model):
    """같은 부모 아래 이미 아는 자식들의 순서가 바뀜(표 정렬 등) — 새 순서의 자식 id."""

    parent_id: str | None
    order: list[str]


class UIDiffResult(_Model):
    base_id: str  # 에이전트가 마지막으로 받은 뷰의 스냅샷 id
    snapshot_id: str
    added: list[AddedNode] = []  # 새로 생겼거나, goal 변경으로 새로 관련성이 생긴 서브트리
    removed: list[str] = []  # UI에서 실제로 사라진 노드 id (서브트리 루트만)
    changed: list[NodeChange] = []
    reordered: list[Reorder] = []  # 형제 순서 변화(시나리오 C1: 정렬이 (no change)로 보이던 문제)

    @property
    def is_empty(self) -> bool:  # Fallback 트리거 "조작 후 기대한 변화 없음"
        return not (self.added or self.removed or self.changed or self.reordered)


class VisionFrame(_Model):
    """에이전트에게 보낸 이미지의 좌표계 — 이미지 픽셀 → 플랫폼 좌표 역변환용 (Partial Vision)."""

    region: Bounds  # 찍은 영역 (플랫폼 좌표)
    width: int  # 이미지 픽셀
    height: int


class StepLog(_Model):
    """서버가 tool 호출마다 남기는 JSONL 한 줄 — benchmarks가 집계한다."""

    run_id: str
    step: int
    mode: Mode
    tool: str
    args: dict[str, str | int | bool | None]
    latency_ms: float
    raw_nodes: int  # 수집한 원본 노드 수
    sent_nodes: int  # 실제 전송한 노드 수
    out_chars: int
    out_tokens: int  # 관찰 텍스트의 tiktoken o200k 토큰 수 — 모드 간 비교용 추정치
    fallback: FallbackReason | None = None  # 이미지를 보냈거나 안내 한 줄을 붙인 사유
    image_tokens: int = 0  # 보낸 이미지의 토큰 추정(가로×세로/750). 0 = 이미지 없음
    error: str | None = None
