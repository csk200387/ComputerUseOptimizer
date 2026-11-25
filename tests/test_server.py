from pathlib import Path

import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent

from cuop.collectors.replay import ReplayCollector
from cuop.mcp_server.server import build_server
from cuop.schema import Bounds, Role, StepLog, UIElementNode, UISnapshot


class FakeCollector:
    """버튼 하나짜리 앱. 클릭하면 이름이 바뀐다."""

    def __init__(self) -> None:
        self.clicked: list[str] = []
        self.actions: list[tuple[str, ...]] = []
        self.owners: dict[str, str] = {}  # 요소 id → 앱 (액션 뒤 관찰 대상)

    async def snapshot(self, app: str | None = None) -> UISnapshot:
        button = UIElementNode(
            id="e2", role=Role.BUTTON, raw_role="AXButton", name="저장됨" if self.clicked else "저장"
        )
        root = UIElementNode(id="e1", role=Role.GENERIC, raw_role="AXApplication", children=[button])
        return UISnapshot(id=f"s{len(self.clicked) + 1}", platform="macos", app=app or "com.example.app", root=root)

    async def click(self, element_id: str) -> None:
        self.clicked.append(element_id)

    async def type_text(self, element_id: str | None, text: str, submit: bool = False) -> None:
        self.actions.append(("type", element_id or "", text, str(submit)))

    async def select(self, element_id: str, option: str) -> None:
        self.actions.append(("select", element_id, option))

    async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        raise NotImplementedError

    async def click_at(self, x: int, y: int) -> None:
        raise NotImplementedError

    async def owner(self, element_id: str) -> str | None:
        return self.owners.get(element_id)

    async def display(self) -> Bounds:
        return Bounds(x=0, y=0, w=1470, h=956)

    async def type_keys(self, text: str, submit: bool = False) -> None:
        self.actions.append(("keys", text, str(submit)))


@pytest.mark.anyio
async def test_tool_contract() -> None:
    async with Client(build_server(FakeCollector()), raise_exceptions=True) as c:
        tools = (await c.list_tools()).tools
    assert sorted(t.name for t in tools) == ["click", "observe", "select", "type"]
    assert all(t.output_schema is None for t in tools)  # structured_output=False → 텍스트 채널 하나


@pytest.mark.anyio
async def test_action_returns_updated_view_and_logs_steps(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "steps.jsonl"
    fake = FakeCollector()
    async with Client(build_server(fake, "full", log), raise_exceptions=True) as c:
        first = await c.call_tool("observe", {"goal": "save the file", "app": "TextEdit"})
        second = await c.call_tool("click", {"target": "e2"})
    texts = [r.content[0] for r in (first, second)]
    assert all(isinstance(t, TextContent) for t in texts)
    assert [t.text for t in texts if isinstance(t, TextContent)] == [
        '[s1] TextEdit\ne2 button "저장"',
        '[s2] TextEdit\ne2 button "저장됨"',  # 액션 후 같은 앱을 다시 관찰
    ]
    assert first.structured_content is None
    assert fake.clicked == ["e2"]
    steps = [StepLog.model_validate_json(line) for line in log.read_text().splitlines()]
    assert [(s.tool, s.raw_nodes, s.sent_nodes) for s in steps] == [("observe", 2, 1), ("click", 2, 1)]
    assert all(0 < s.out_tokens < s.out_chars for s in steps)


@pytest.mark.anyio
async def test_compact_mode_folds_closed_menus(tmp_path: Path) -> None:
    log = tmp_path / "steps.jsonl"
    replay = ReplayCollector((Path(__file__).parent / "fixtures" / "calculator").glob("*.json"))
    async with Client(build_server(replay, "compact", log), raise_exceptions=True) as c:
        result = await c.call_tool("observe", {"goal": "add two numbers"})
    text = result.content[0]
    assert isinstance(text, TextContent) and '"보기"' in text.text and "계산기 종료" not in text.text
    (step,) = [StepLog.model_validate_json(line) for line in log.read_text().splitlines()]
    assert step.mode == "compact" and step.sent_nodes < step.raw_nodes // 4


@pytest.mark.anyio
async def test_proposed_mode_sends_changes_and_full_on_request() -> None:
    async with Client(build_server(FakeCollector(), "proposed"), raise_exceptions=True) as c:
        results = [
            await c.call_tool("observe", {"goal": "save", "app": "TextEdit"}),
            await c.call_tool("click", {"target": "e2"}),
            await c.call_tool("click", {"target": "e2"}),
            await c.call_tool("observe", {"goal": "save", "app": "TextEdit", "full": True}),
        ]
    assert [r.content[0].text for r in results if isinstance(r.content[0], TextContent)] == [
        '[s1] TextEdit\ne2 button "저장"',
        '[s2 since s1]\n~ e2 "저장됨"',
        '[s3 since s2]\n(no change)\nhint: no change; observe(vision="window") shows the screen',
        '[s3] TextEdit\ne2 button "저장됨"',  # full=True → Diff 대신 전체 뷰
    ]


@pytest.mark.anyio
async def test_type_and_select_reach_collector_but_typed_text_is_not_logged(tmp_path: Path) -> None:
    log = tmp_path / "steps.jsonl"
    fake = FakeCollector()
    async with Client(build_server(fake, "full", log), raise_exceptions=True) as c:
        await c.call_tool("type", {"target": "e2", "text": "secret", "submit": True})
        await c.call_tool("select", {"target": "e2", "option": "18"})
    assert fake.actions == [("type", "e2", "secret", "True"), ("select", "e2", "18")]
    assert "secret" not in log.read_text()  # 입력 텍스트는 길이만 남긴다
    assert StepLog.model_validate_json(log.read_text().splitlines()[0]).args["text"] == "<6 chars>"


class FakeBrowser(FakeCollector):
    """보이는 탭 하나. ID는 w 접두어, app은 탭 주소."""

    def __init__(self) -> None:
        super().__init__()
        self.url = "about:blank"

    def handles(self, app: str) -> bool:
        return app.lower() == "edge"

    @property
    def os_app(self) -> str:
        return "com.example.browser"

    async def open(self, url: str) -> None:
        self.url = url

    async def snapshot(self, app: str | None = None) -> UISnapshot:
        link = UIElementNode(id="w2", role=Role.LINK, raw_role="link", name="다음" if self.clicked else "처음")
        root = UIElementNode(id="w1", role=Role.DOCUMENT, raw_role="RootWebArea", children=[link])
        return UISnapshot(id=f"ws{len(self.clicked) + 1}", platform="web", app=self.url, root=root)


@pytest.mark.anyio
async def test_routes_by_url_and_id_prefix_and_keeps_diff_state_per_app() -> None:
    mac, web = FakeCollector(), FakeBrowser()
    async with Client(build_server(mac, "proposed", web=web), raise_exceptions=True) as c:
        texts = [
            (await c.call_tool(tool, args)).content[0]
            for tool, args in [
                ("observe", {"goal": "save", "app": "TextEdit"}),
                ("observe", {"goal": "save", "url": "file:///order.html"}),
                ("click", {"target": "w2"}),  # w → web, 다시 관찰도 web
                ("click", {"target": "e2"}),  # e → macOS, 처음 받은 TextEdit 뷰와의 Diff
                ("observe", {"goal": "save", "app": "Edge"}),  # 브라우저 이름 → web, 탭 상태 유지
            ]
        ]
    assert [t.text for t in texts if isinstance(t, TextContent)] == [
        '[s1] TextEdit\ne2 button "저장"',
        '[ws1] file:///order.html\nw2 link "처음"',
        '[ws2 since ws1]\n~ w2 "다음"',
        '[s2 since s1]\n~ e2 "저장됨"',
        "[ws2 since ws2]\n(no change)",
    ]
    assert (mac.clicked, web.clicked) == (["e2"], ["w2"])


class Canvas(FakeCollector):
    """그림판 창 하나짜리 앱 — 이름 있는 요소가 `named`개뿐(트리로는 알 수 있는 것이 거의 없다)."""

    def __init__(self, named: int = 1) -> None:
        super().__init__()
        self.named, self.captured, self.clicked_at = named, list[tuple[Bounds, int, int]](), list[tuple[int, int]]()

    async def snapshot(self, app: str | None = None) -> UISnapshot:
        buttons = [
            UIElementNode(id=f"e{3 + i}", role=Role.BUTTON, raw_role="AXButton", name=f"붓{i}",
                          bounds=Bounds(x=110 + 20 * i, y=60, w=10, h=10))
            for i in range(self.named)
        ]  # fmt: skip
        win = UIElementNode(
            id="e2", role=Role.WINDOW, raw_role="AXWindow", name="그림판",
            bounds=Bounds(x=100, y=50, w=400, h=300), children=buttons,
        )  # fmt: skip
        root = UIElementNode(id="e1", role=Role.GENERIC, raw_role="AXApplication", children=[win])
        return UISnapshot(id=f"s{len(self.clicked_at) + 1}", platform="macos", app="paint", window="그림판", root=root)

    async def click(self, element_id: str) -> None:
        if element_id != "e3":
            raise LookupError(f"unknown element id {element_id!r}")

    async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        self.captured.append((region, width, height))
        return b"\x89PNG"

    async def click_at(self, x: int, y: int) -> None:
        self.clicked_at.append((x, y))


def _steps(log: Path) -> list[StepLog]:
    return [StepLog.model_validate_json(line) for line in log.read_text().splitlines()]


@pytest.mark.anyio
async def test_sparse_window_sends_an_image_and_xy_clicks_map_to_the_screen(tmp_path: Path) -> None:
    log, canvas = tmp_path / "steps.jsonl", Canvas()
    async with Client(build_server(canvas, "proposed", log), raise_exceptions=True) as c:
        first = await c.call_tool("observe", {"goal": "선 긋기"})
        await c.call_tool("click", {"x": 200, "y": 100})
    text, image = first.content
    assert isinstance(text, TextContent) and isinstance(image, ImageContent)
    assert text.text.splitlines()[-1] == (
        "image e2 400x300 (tree has few named elements): click x, y are pixels of this image"
    )
    assert canvas.captured[0] == (Bounds(x=100, y=50, w=400, h=300), 400, 300)  # 1px = 1pt
    assert canvas.clicked_at == [(300, 150)]  # 창 원점 (100, 50) + 이미지 픽셀
    # 클릭 뒤에도 트리가 비어 있으니 다시 자동 이미지
    assert [(s.fallback, s.image_tokens) for s in _steps(log)] == [("empty_tree", 160), ("empty_tree", 160)]


@pytest.mark.anyio
async def test_vision_on_request_and_xy_only_right_after_an_image(tmp_path: Path) -> None:
    log, app = tmp_path / "steps.jsonl", Canvas(named=5)  # 트리가 충분 → 자동 이미지 없음
    async with Client(build_server(app, "full", log), raise_exceptions=True) as c:
        plain = await c.call_tool("observe", {"goal": "붓 색 확인"})
        early = await c.call_tool("click", {"x": 1, "y": 1})
        seen = await c.call_tool("observe", {"goal": "붓 색 확인", "vision": "e3"})
        await c.call_tool("click", {"x": 5, "y": 5})
        late = await c.call_tool("click", {"x": 5, "y": 5})  # 직전 관찰이 이미지가 아니다
    assert len(plain.content) == 1 and len(seen.content) == 2
    assert app.captured == [(Bounds(x=110, y=60, w=10, h=10), 10, 10)] and app.clicked_at == [(115, 65)]
    for r in (early, late):
        assert r.is_error and isinstance(t := r.content[0], TextContent) and "need an image" in t.text
    assert [s.fallback for s in _steps(log)] == [None, None, "visual_region", "no_diff", None]


@pytest.mark.anyio
async def test_hints_for_no_change_and_missing_target(tmp_path: Path) -> None:
    log = tmp_path / "steps.jsonl"
    async with Client(build_server(Canvas(named=5), "compact", log), raise_exceptions=True) as c:
        await c.call_tool("observe", {"goal": "붓 고르기"})
        same = await c.call_tool("click", {"target": "e3"})  # Canvas는 클릭해도 그대로
        gone = await c.call_tool("click", {"target": "e9"})
    assert isinstance(t := same.content[0], TextContent)
    assert t.text.endswith('hint: no change; observe(vision="window") shows the screen')
    assert gone.is_error and isinstance(t := gone.content[0], TextContent)
    assert t.text.endswith("LookupError: unknown element id 'e9'; observe(vision=\"window\") shows the screen")
    assert [(s.fallback, s.image_tokens) for s in _steps(log)] == [(None, 0), ("no_diff", 0), ("target_missing", 0)]


@pytest.mark.anyio
async def test_failed_vision_request_leaves_the_diff_base_alone() -> None:
    class NoScreen(Canvas):
        async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
            raise PermissionError("Screen Recording is off")

    app = NoScreen(named=5)
    async with Client(build_server(app, "proposed"), raise_exceptions=True) as c:
        await c.call_tool("observe", {"goal": "붓"})
        app.named = 6  # 새 버튼이 생겼지만
        failed = await c.call_tool("observe", {"goal": "붓", "vision": "window"})  # 이 뷰는 에이전트가 못 받았다
        again = await c.call_tool("observe", {"goal": "붓"})
    assert failed.is_error and isinstance(t := failed.content[0], TextContent) and "Screen Recording" in t.text
    assert isinstance(t := again.content[0], TextContent) and '+ e2\n e8 button "붓5"' in t.text


@pytest.mark.anyio
async def test_screenshot_mode_sends_the_whole_screen_and_acts_by_coordinates(tmp_path: Path) -> None:
    log, app = tmp_path / "steps.jsonl", Canvas(named=5)
    async with Client(build_server(app, "screenshot", log), raise_exceptions=True) as c:
        first = await c.call_tool("observe", {"goal": "붓 고르기", "app": "paint"})
        await c.call_tool("click", {"x": 665, "y": 432})
        await c.call_tool("type", {"text": "안녕", "submit": True})
        by_id = await c.call_tool("click", {"target": "e3"})
        chosen = await c.call_tool("select", {"target": "e3", "option": "1"})
    text, image = first.content
    assert isinstance(text, TextContent) and isinstance(image, ImageContent)
    assert text.text == "[s1] paint screen 1330x865: click x, y are pixels of this image"  # 트리는 보내지 않는다
    assert app.captured[0] == (Bounds(x=0, y=0, w=1470, h=956), 1330, 865)  # 전체 화면, 모델 상한에 맞춰 축소
    assert app.clicked_at == [(735, 477)]  # 이미지 픽셀 → 화면 point (배율 1470/1330)
    assert app.actions == [("keys", "안녕", "True")]  # 요소 id 없이 키보드 이벤트
    for r in (by_id, chosen):
        assert r.is_error and isinstance(t := r.content[0], TextContent) and "no element ids" in t.text
    steps = _steps(log)
    assert all(s.image_tokens == 1534 for s in steps if not s.error) and [s.error is None for s in steps] == [
        True, True, True, False, False,
    ]  # fmt: skip


@pytest.mark.anyio
async def test_action_observes_the_app_that_owns_the_element() -> None:
    """두 앱을 오갈 때: 텍스트 편집기를 본 뒤 계산기 버튼을 누르면 결과는 계산기 화면이어야 한다."""
    fake = FakeCollector()
    fake.owners = {"e2": "calculator"}
    async with Client(build_server(fake, "full"), raise_exceptions=True) as c:
        await c.call_tool("observe", {"goal": "문서", "app": "TextEdit"})
        clicked = await c.call_tool("click", {"target": "e2"})
    assert isinstance(t := clicked.content[0], TextContent) and t.text.startswith("[s2] calculator")


class CanvasPage(FakeBrowser):
    """캔버스만 있는 탭 — 문서가 곧 보이는 영역(뷰포트 CSS px)."""

    def __init__(self) -> None:
        super().__init__()
        self.captured, self.clicked_at = list[tuple[Bounds, int, int]](), list[tuple[int, int]]()

    async def snapshot(self, app: str | None = None) -> UISnapshot:
        canvas = UIElementNode(id="w2", role=Role.CANVAS, raw_role="Canvas", bounds=Bounds(x=40, y=120, w=300, h=200))
        view = Bounds(x=0, y=0, w=800, h=600)
        root = UIElementNode(id="w1", role=Role.DOCUMENT, raw_role="RootWebArea", name="색", bounds=view,
                             children=[canvas])  # fmt: skip
        return UISnapshot(id=f"ws{len(self.clicked_at) + 1}", platform="web", app=self.url, root=root)

    async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        self.captured.append((region, width, height))
        return b"\x89PNG"

    async def click_at(self, x: int, y: int) -> None:
        self.clicked_at.append((x, y))


@pytest.mark.anyio
async def test_web_page_with_little_tree_sends_an_image_and_xy_clicks_the_tab(tmp_path: Path) -> None:
    log, mac, web = tmp_path / "steps.jsonl", Canvas(named=5), CanvasPage()
    async with Client(build_server(mac, "proposed", log, web), raise_exceptions=True) as c:
        first = await c.call_tool("observe", {"goal": "파랑", "url": "http://127.0.0.1/canvas.html"})
        await c.call_tool("click", {"x": 190, "y": 220})  # 문서 이미지 → 뷰포트 좌표 그대로
        await c.call_tool("observe", {"goal": "파랑", "app": "Edge", "vision": "w2"})
        await c.call_tool("click", {"x": 10, "y": 20})  # 캔버스 이미지 → 캔버스 원점 (40, 120) 더하기
    assert isinstance(t := first.content[-2], TextContent) and t.text.endswith(
        "image w1 800x600 (tree has few named elements): click x, y are pixels of this image"
    )
    page, canvas = (Bounds(x=0, y=0, w=800, h=600), 800, 600), (Bounds(x=40, y=120, w=300, h=200), 300, 200)
    assert web.captured == [page, page, canvas, page]  # 클릭 뒤에도 트리가 비어 있어 다시 자동 이미지
    assert web.clicked_at == [(190, 220), (50, 140)] and mac.clicked_at == []
    assert [s.fallback for s in _steps(log)] == ["empty_tree", "empty_tree", "visual_region", "empty_tree"]


@pytest.mark.anyio
async def test_observe_without_app_stays_on_the_tab_and_vision_ids_route_by_prefix() -> None:
    """webcanvas 시험 run: 웹 탭을 본 뒤 app 없이 observe(vision="w10")이 맨 앞 앱(OS)으로 가 실패했다(2026-11-04)."""
    mac, web = Canvas(named=5), CanvasPage()
    async with Client(build_server(mac, "proposed", web=web), raise_exceptions=True) as c:
        heads = [
            (await c.call_tool("observe", {"goal": "좌석", **args})).content[0]
            for args in [
                {"url": "http://127.0.0.1/seats.html"},
                {"vision": "w2"},  # 요소 id → 그 수집기
                {},  # app 생략 → 마지막으로 본 탭
                {"app": "paint"},
                {},  # OS는 지금처럼 맨 앞 앱(여기선 paint)
            ]
        ]
    snaps = [t.text.split()[0].strip("[]") for t in heads if isinstance(t, TextContent)]
    assert snaps == ["ws1", "ws1", "ws1", "s1", "s1"]  # ws = web 탭, s = OS 앱
    assert web.captured[1] == (Bounds(x=40, y=120, w=300, h=200), 300, 200)
