"""Web 수집기 — 사용자의 Chromium 계열 브라우저(기본 프로필)에 CDP로 붙어 보이는 탭의 접근성 트리를 읽는다.

연결: 브라우저의 `<채널>://inspect/#remote-debugging`에서 원격 디버깅을 켜면 DevToolsActivePort가 생기고,
Playwright `connect_over_cdp(채널)`이 그것을 읽는다. 브라우저는 연결마다 '원격 디버깅을 허용하시겠습니까?'를
묻고 사용자가 허용할 때까지 응답하지 않는다 → 연결은 수집기당 한 번, 그 창은 절대 대신 누르지 않는다.
브라우저를 띄우거나 닫지 않는다 — 연결만 끊는다.
관찰 · 조작 범위: 보이는 탭과 서버가 url로 연 탭. 비밀번호 칸의 값은 보내지 않는다.
"""

import asyncio
import base64
import itertools
import struct
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast

from playwright.async_api import Browser, CDPSession, Page, Playwright, async_playwright

from cuop.schema import Bounds, Role, State, UIElementNode, UISnapshot

MAX_NODES = 5000  # ponytail: 고정 상한(macOS와 같음), 긴 페이지에서 잘리면 화면 안 노드만 수집으로 전환
CONNECT_TIMEOUT = 60  # 초 — 사용자가 브라우저의 허용 창을 누를 시간
SETTLE = 0.3  # 액션 후 페이지가 반응할 시간(초) — ponytail: 고정 대기, 느린 페이지는 DOM 변화 대기로
CAPTURE_TIMEOUT = 10  # 초 — 그리지 않는 탭(백그라운드)이면 캡처가 끝나지 않는다
APPS = {  # 채널 → (macOS 번들 ID, app 인자로 이 브라우저를 가리키는 이름 — 소문자)
    "msedge": ("com.microsoft.edgemac", {"msedge", "edge", "microsoft edge", "com.microsoft.edgemac"}),
    "chrome": ("com.google.Chrome", {"chrome", "google chrome", "com.google.chrome"}),
    # 하네스 전용: 서버가 새 프로필로 직접 띄우는 Playwright Chromium (`playwright install chromium`)
    "launch": ("com.google.chrome.for.testing", {"chrome for testing", "google chrome for testing", "chromium"}),
}

ROLES = {
    "RootWebArea": Role.DOCUMENT,
    "dialog": Role.DIALOG,
    "alertdialog": Role.DIALOG,
    "menubar": Role.MENUBAR,
    "menu": Role.MENU,
    "menuitem": Role.MENUITEM,
    "menuitemcheckbox": Role.MENUITEM,
    "menuitemradio": Role.MENUITEM,
    "toolbar": Role.TOOLBAR,
    "tablist": Role.TABLIST,
    "tab": Role.TAB,
    "button": Role.BUTTON,
    "link": Role.LINK,
    "checkbox": Role.CHECKBOX,
    "switch": Role.CHECKBOX,
    "radio": Role.RADIO,
    "textbox": Role.TEXTBOX,
    "searchbox": Role.TEXTBOX,
    "spinbutton": Role.TEXTBOX,
    "combobox": Role.COMBOBOX,
    "listbox": Role.LISTBOX,
    "MenuListPopup": Role.LISTBOX,
    "option": Role.OPTION,
    "MenuListOption": Role.OPTION,
    "slider": Role.SLIDER,
    "list": Role.LIST,
    "listitem": Role.LISTITEM,
    "table": Role.TABLE,
    "grid": Role.TABLE,
    "row": Role.ROW,
    "cell": Role.CELL,
    "gridcell": Role.CELL,
    "columnheader": Role.CELL,
    "rowheader": Role.CELL,
    "tree": Role.TREE,
    "treeitem": Role.TREEITEM,
    "heading": Role.HEADING,
    "StaticText": Role.TEXT,
    "image": Role.IMAGE,
    "Canvas": Role.CANVAS,  # Chrome 내부 이름은 대문자(2026-11-04 스파이크)
    "group": Role.GROUP,
    "form": Role.GROUP,
    "region": Role.GROUP,
    "main": Role.GROUP,
    "navigation": Role.GROUP,
    "banner": Role.GROUP,
    "contentinfo": Role.GROUP,
    "complementary": Role.GROUP,
    "search": Role.GROUP,
}
STATES = {"focused": State.FOCUSED, "disabled": State.DISABLED, "selected": State.SELECTED,
          "required": State.REQUIRED, "readonly": State.READONLY, "modal": State.MODAL}  # fmt: skip
SKIP = {"InlineTextBox", "LineBreak"}  # 글자 배치용 — 내용은 부모 StaticText에 있다
# 입력 칸 안쪽(user-agent shadow DOM의 편집기 · 글자 노드)은 값과 같은 내용이라 뺀다(모든 모드)
# ponytail: contenteditable 리치 에디터의 문단 구조도 빠진다 — 그런 페이지가 나오면 textbox 자식 중 문단은 남긴다
LEAF = {"textbox", "searchbox", "spinbutton"}


async def _send(cdp: CDPSession, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return cast(dict[str, Any], await cdp.send(method, params))  # pyright: ignore[reportUnknownMemberType]


def _v(field: dict[str, Any] | None) -> Any:
    return (field or {}).get("value")


def _str(x: Any) -> str | None:
    return str(x) if x not in (None, "") else None


def unit(snap: dict[str, Any], css_width: float) -> float:
    """DOMSnapshot 좌표 ÷ CSS px — 같은 문서 너비를 두 단위로 재서 나눈다.

    화면 Chrome은 Retina에서 기기 픽셀(2)로 준다(DPR을 1로 흉내 내도 2), headless는 CSS px(1) — 2026-11-04 스파이크."""
    width = snap["documents"][0].get("contentWidth", 0)
    return width / css_width if width and css_width else 1.0


def boxes(snap: dict[str, Any], view: Bounds, k: float = 1.0) -> dict[int, Bounds]:
    """DOMSnapshot.captureSnapshot → backendDOMNodeId별 뷰포트 영역(CSS px, 보이는 부분만, 화면 밖은 빠짐).

    layout bounds는 문서 좌표라 스크롤을 뺀다. k = `unit`. iframe 문서는 안 본다(AX 트리도 같음)."""
    doc = snap["documents"][0]
    bids, layout = doc["nodes"]["backendNodeId"], doc["layout"]
    sx, sy = doc.get("scrollOffsetX", 0), doc.get("scrollOffsetY", 0)
    out: dict[int, Bounds] = {}
    for i, (x, y, w, h) in zip(layout["nodeIndex"], layout["bounds"], strict=True):
        x0, y0 = max(round((x - sx) / k), 0), max(round((y - sy) / k), 0)
        x1, y1 = min(round((x - sx + w) / k), view.w), min(round((y - sy + h) / k), view.h)
        if x1 > x0 and y1 > y0:
            out[bids[i]] = Bounds(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
    return out


def build_tree(
    nodes: list[dict[str, Any]], ids: dict[int, str], seq: Iterator[int], areas: dict[int, Bounds] | None = None
) -> UIElementNode:
    """Accessibility.getFullAXTree의 평탄한 목록 → 공통 트리. ignored 노드는 빼고 자식을 부모에 붙인다.

    ids: backendDOMNodeId → w42 (호출 측이 탭마다 유지, 새 노드는 여기서 발급).
    seq: 번호 — 모든 탭이 하나를 같이 써서 탭이 달라도 ID가 겹치지 않는다.
    areas: backendDOMNodeId → 뷰포트 영역 (`boxes`) — Partial Vision용, 직렬화되지 않는다.
    """
    areas = areas or {}
    by = {n["nodeId"]: n for n in nodes}
    count = 0

    def wid(n: dict[str, Any]) -> str:
        key = n.get("backendDOMNodeId") or -int(n["nodeId"])  # DOM이 없는 노드(드묾)는 AX nodeId로
        if key not in ids:
            ids[key] = f"w{next(seq)}"
        return ids[key]

    def kids(n: dict[str, Any]) -> list[UIElementNode]:
        out: list[UIElementNode] = []
        for cid in n.get("childIds", []):
            c = by.get(cid)
            if c is None or _v(c.get("role")) in SKIP:
                continue
            if c.get("ignored"):
                out += kids(c)
            elif (node := walk(c)) is not None:
                out.append(node)
        return out

    def walk(n: dict[str, Any]) -> UIElementNode | None:
        nonlocal count
        if count >= MAX_NODES:
            return None
        count += 1
        eid = wid(n)  # 전위 순서로 발급 → 첫 관찰에서 ID가 위→아래로 증가
        raw = str(_v(n.get("role")) or "?")
        props = {p["name"]: _v(p.get("value")) for p in n.get("properties", [])}
        states = {s for k, s in STATES.items() if props.get(k) is True}
        if props.get("checked") in ("true", "mixed") or props.get("pressed") in ("true", "mixed"):
            states.add(State.CHECKED)
        if isinstance(props.get("expanded"), bool):
            states.add(State.EXPANDED if props["expanded"] else State.COLLAPSED)
        value = _str(_v(n.get("value")))
        if value and not value.strip("•"):
            value = "<hidden>"  # 비밀번호 칸: 브라우저가 이미 •로 가려 준다(스파이크 확인) → 길이도 보내지 않는다
        return UIElementNode(
            id=eid,
            role=ROLES.get(raw, Role.GENERIC),
            raw_role=raw,
            name=_str(_v(n.get("name"))),
            value=value,
            description=_str(_v(n.get("description"))),
            states=frozenset(states),
            bounds=areas.get(n.get("backendDOMNodeId", 0)),
            children=[] if raw in LEAF else kids(n),
        )

    root = walk(nodes[0])  # 첫 노드가 RootWebArea
    assert root is not None
    return root


class WebCollector:
    """element_id(w42) ↔ (탭, backendDOMNodeId). backendDOMNodeId는 문서가 살아 있는 동안 같은 DOM 노드를 가리킨다
    → 스냅샷이 바뀌어도 같은 ID. 페이지를 이동하면 노드가 모두 새로 만들어져 새 ID."""

    def __init__(
        self, channel: str = "msedge", front_title: Callable[[str], Awaitable[str | None]] | None = None
    ) -> None:
        """front_title: 앱(번들 ID)의 맨 앞 창 제목을 돌려주는 OS 수집기 함수 — 보이는 탭을 고르는 데 쓴다."""
        self._channel = channel
        self._front_title = front_title
        self._targets: CDPSession | None = None  # 브라우저 수준 세션 (탭 목록)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._opened: list[Page] = []  # open()으로 연 탭 — reset()이 닫는다
        self._cdp: dict[Page, CDPSession] = {}
        self._reset()

    def _reset(self) -> None:
        self._page: Page | None = None  # 마지막으로 관찰한 탭
        self._fresh = False  # 다음 관찰은 self._page — 방금 url로 연 탭(창 제목이 늦게 바뀜) · 방금 조작한 탭
        self._ids: dict[Page, dict[int, str]] = {}  # ponytail: 닫힌 탭 · 지나간 문서의 ID도 세션 동안 남는다
        self._els: dict[str, tuple[Page, int]] = {}
        self._next = itertools.count(1)
        self._seq = 0
        self._unit = 1.0  # 마지막 DOMSnapshot의 기기 배율 (`unit`)

    def handles(self, app: str) -> bool:
        return app.lower() in APPS.get(self._channel, ("", {self._channel}))[1]

    @property
    def os_app(self) -> str:
        """OS 수집기가 이 브라우저 창을 찾을 이름(번들 ID) — 비교군 ①은 웹도 화면 픽셀로 본다."""
        return APPS.get(self._channel, (self._channel, set()))[0]

    async def _connect(self) -> Browser:
        if self._browser is None or not self._browser.is_connected():
            self._pw = self._pw or await async_playwright().start()
            if self._channel == "launch":  # 하네스 전용 — 허용 창 없음, 사용자 브라우저 · 프로필과 분리
                self._browser = await self._pw.chromium.launch(headless=False)
                self._targets = await self._browser.new_browser_cdp_session()
                return self._browser
            try:  # Playwright의 timeout은 허용 대기를 끊지 못한다(스파이크에서 2분 넘게 멈춤)
                connecting = self._pw.chromium.connect_over_cdp(self._channel)
                self._browser = await asyncio.wait_for(connecting, CONNECT_TIMEOUT)
            except TimeoutError:
                raise TimeoutError(
                    "browser did not accept the connection; the user must allow remote debugging"
                ) from None
            self._targets = await self._browser.new_browser_cdp_session()
        return self._browser

    async def _session(self, page: Page) -> CDPSession:
        if page not in self._cdp:
            self._cdp[page] = await page.context.new_cdp_session(page)
        return self._cdp[page]

    async def _visible(self) -> Page:
        """보이는 탭 = 브라우저 맨 앞 창의 제목을 제목으로 가진 탭 (마지막으로 본 탭 우선).

        CDP에는 활성 탭 정보가 없다: 연결된 Edge 156의 탭 5개가 모두 visibilityState=visible · hasFocus · rAF 동작,
        Target.getTargets 순서도 활성 순이 아니었다(2026-11-02 스파이크) → OS 접근성 API의 창 제목으로 고른다.
        고를 수 없으면 엉뚱한 탭을 읽지 않도록 에러."""
        browser = await self._connect()
        pages = [p for c in browser.contexts for p in c.pages]
        if (self._fresh or not self._front_title) and self._page in pages:
            self._fresh = False
            return self._page  # 방금 url로 연 탭 · 창 제목을 알 수 없는 환경(OS 수집기 없음)이면 마지막으로 본 탭
        bundle = APPS.get(self._channel, (self._channel, set()))[0]
        title = await self._front_title(bundle) if self._front_title else None
        if title and self._targets:
            infos = (await _send(self._targets, "Target.getTargets"))["targetInfos"]
            urls = {t["url"] for t in infos if t["type"] == "page" and t["title"] and t["title"] in title}
            matches = [p for p in pages if p.url in urls]
            if matches:
                return self._page if self._page in matches else matches[0]
        raise RuntimeError("cannot tell which browser tab is visible; pass url to open the page")

    async def open(self, url: str) -> None:
        browser = await self._connect()
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        self._page = await context.new_page()
        self._opened.append(self._page)
        self._fresh = True
        await self._page.goto(url)

    async def reset(self) -> None:
        """open()으로 연 탭을 닫고 ID · 탭 상태를 비운다. 연결(사용자 허용)은 유지 — 측정 run마다 같은 ID로 시작."""
        for page in self._opened:
            if not page.is_closed():
                await page.close()
        self._opened.clear()
        self._reset()

    async def close(self) -> None:
        """reset() 후 연결을 끊는다 — 브라우저와 사용자 탭은 그대로."""
        await self.reset()
        if self._pw:
            await self._pw.stop()
        self._pw = self._browser = self._targets = None

    async def snapshot(self, app: str | None = None) -> UISnapshot:
        """app은 쓰지 않는다 — 브라우저 하나에 연결되어 있고 대상은 보이는 탭."""
        page = self._page = await self._visible()
        cdp = await self._session(page)
        nodes = (await _send(cdp, "Accessibility.getFullAXTree"))["nodes"]
        metrics = await _send(cdp, "Page.getLayoutMetrics")
        vp = metrics["cssVisualViewport"]
        view = Bounds(x=0, y=0, w=int(vp["clientWidth"]), h=int(vp["clientHeight"]))
        # ponytail: 관찰마다 CDP 호출 하나 더(로컬 페이지 0.4–1.4 ms) — 큰 페이지에서 느리면 vision 요청 때만
        layout = await _send(cdp, "DOMSnapshot.captureSnapshot", {"computedStyles": []})
        self._unit = unit(layout, metrics["cssContentSize"]["width"])
        ids = self._ids.setdefault(page, {})
        before = len(ids)
        root = build_tree(nodes, ids, self._next, boxes(layout, view, self._unit))
        root = root.model_copy(update={"bounds": view})  # 문서 = 보이는 영역 — vision="window" · 자동 판정의 '창'
        for key, eid in list(ids.items())[before:]:  # 새로 발급된 ID (dict는 삽입 순서)
            self._els[eid] = (page, key)
        self._seq += 1
        return UISnapshot(id=f"ws{self._seq}", platform="web", app=page.url, window=root.name, root=root)

    def _el(self, element_id: str) -> tuple[Page, int]:
        if (el := self._els.get(element_id)) is None:
            raise LookupError(f"unknown element id {element_id!r}; call observe first")
        return el

    async def click(self, element_id: str) -> None:
        """요소 가운데에 실제 마우스 이벤트 — 페이지 스크립트가 보기엔 사용자 클릭과 같다."""
        page, bid = self._el(element_id)
        cdp = await self._session(page)
        await _send(cdp, "DOM.scrollIntoViewIfNeeded", {"backendNodeId": bid})
        quad = (await _send(cdp, "DOM.getContentQuads", {"backendNodeId": bid}))["quads"][0]
        await self._mouse(page, sum(quad[0::2]) / 4, sum(quad[1::2]) / 4)

    async def _mouse(self, page: Page, x: float, y: float) -> None:
        cdp = await self._session(page)
        for kind in ("mousePressed", "mouseReleased"):
            await _send(
                cdp, "Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
            )
        await self._settle(page)

    async def type_text(self, element_id: str | None, text: str, submit: bool = False) -> None:
        """포커스(기본: 이미 포커스된 요소) → 전체 선택 → 입력으로 대체. submit은 Enter 키."""
        page, bid = self._el(element_id) if element_id else (await self._visible(), None)
        cdp = await self._session(page)
        if bid is not None:
            await _send(cdp, "DOM.focus", {"backendNodeId": bid})
        await self._key(cdp, commands=["selectAll"] if text else ["selectAll", "deleteBackward"])
        if text:
            await _send(cdp, "Input.insertText", {"text": text})
        if submit:
            await self._key(cdp, key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r")
        await self._settle(page)

    async def select(self, element_id: str, option: str) -> None:
        """네이티브 <select>: 이름이 option인 항목을 고르고 input · change 이벤트를 보낸다.

        ponytail: ARIA 콤보박스(커스텀 목록)는 아직 — 에이전트가 click으로 열고 항목을 누른다."""
        page, bid = self._el(element_id)
        cdp = await self._session(page)
        obj = (await _send(cdp, "DOM.resolveNode", {"backendNodeId": bid}))["object"]["objectId"]
        fn = """function (label) {
            if (!(this instanceof HTMLSelectElement)) return "not a <select>; click the option instead";
            const o = [...this.options].find(o => o.label.trim() === label || o.value === label);
            if (!o) return "no option " + JSON.stringify(label);
            this.value = o.value;
            for (const t of ["input", "change"]) this.dispatchEvent(new Event(t, {bubbles: true}));
            return "";
        }"""
        call = {"objectId": obj, "functionDeclaration": fn, "arguments": [{"value": option}], "returnByValue": True}
        res = await _send(cdp, "Runtime.callFunctionOn", call)
        if err := res["result"].get("value"):
            raise RuntimeError(err)
        await self._settle(page)

    async def _key(self, cdp: CDPSession, **event: Any) -> None:
        await _send(cdp, "Input.dispatchKeyEvent", {"type": "keyDown" if "text" in event else "rawKeyDown", **event})
        await _send(
            cdp, "Input.dispatchKeyEvent", {"type": "keyUp", **{k: v for k, v in event.items() if k != "commands"}}
        )

    async def _settle(self, page: Page) -> None:
        await asyncio.sleep(SETTLE)
        await page.wait_for_load_state()  # 클릭이 페이지 이동을 일으켰으면 새 문서가 뜰 때까지
        # 액션 뒤 관찰은 조작한 탭 — 그사이 사용자가 다른 탭을 보고 있어도 그 탭을 읽지 않는다(2026-11-03)
        self._page, self._fresh = page, True

    def _shown(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise LookupError("call observe first")
        return self._page

    async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        """마지막으로 관찰한 탭이 그린 페이지만 찍는다 — 화면 기록 권한 · 앞으로 가져오기가 필요 없고
        다른 창 · 주소창이 들어가지 않는다(2026-11-04 사용자 결정). region은 뷰포트 CSS px."""
        cdp = await self._session(page := self._shown())
        vp = (await _send(cdp, "Page.getLayoutMetrics"))["cssVisualViewport"]
        # clip은 문서 좌표(스파이크) → 지금 스크롤을 더한다
        clip = {"x": region.x + vp["pageX"], "y": region.y + vp["pageY"], "width": region.w, "height": region.h}
        # clip scale 1 = 기기 픽셀(Retina 2배) — DOMSnapshot과 같은 배율(스파이크). 어긋나면 한 번 고쳐 다시 찍는다
        scale, png = width / region.w / self._unit, b""
        for _ in range(2):
            shot = _send(cdp, "Page.captureScreenshot", {"format": "png", "clip": clip | {"scale": scale}})
            try:
                png = base64.b64decode((await asyncio.wait_for(shot, CAPTURE_TIMEOUT))["data"])
            except TimeoutError:
                raise TimeoutError(f"tab did not render {page.url}; it may be in the background") from None
            got = struct.unpack(">I", png[16:20])[0]  # PNG IHDR 너비
            if abs(got - width) <= 1:
                break
            scale *= width / got
        return png

    async def click_at(self, x: int, y: int) -> None:
        """마지막으로 관찰한(이미지를 찍은) 탭의 뷰포트 좌표에 실제 마우스 이벤트 — 다른 앱에 닿을 수 없다."""
        await self._mouse(self._shown(), x, y)

    async def owner(self, element_id: str) -> str | None:
        return None  # web은 조작한 탭을 수집기가 직접 고정한다 · replay는 녹화 순서대로

    async def display(self) -> Bounds:
        raise NotImplementedError("screenshot mode is not supported for web pages yet")

    async def type_keys(self, text: str, submit: bool = False) -> None:
        raise NotImplementedError("screenshot mode is not supported for web pages yet")
