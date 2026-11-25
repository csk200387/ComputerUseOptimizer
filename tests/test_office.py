"""Office 시험(OFFICE_BRIEF.md)에서 고친 headed 입력 — OS 호출은 가짜로 바꾼다."""
# pyright: reportPrivateUsage=false

from types import SimpleNamespace
from typing import Any

import pytest

from cuop.desktop import actions as A
from cuop.desktop.errors import CuopError
from cuop.schema import Role, State, UIElementNode

TARGET, EDITOR, BUTTON, WINDOW, APP = object(), object(), object(), object(), object()


def _live(role: Role = Role.COMBOBOX) -> Any:
    node = UIElementNode(id="e1", role=role, raw_role="AXComboBox")
    return SimpleNamespace(el=TARGET, entry=SimpleNamespace(pid=42), node=node, window=None)


def _fake_ax(focused: object) -> Any:
    parents = {EDITOR: TARGET, BUTTON: TARGET, TARGET: WINDOW, WINDOW: APP}
    roles = {EDITOR: "AXTextField", BUTTON: "AXButton"}

    def attr(el: object, name: str) -> object:
        if name == "AXFocusedUIElement":
            return focused
        if name == "AXRole":
            return roles.get(el)
        return parents.get(el) if name == "AXParent" else None

    def app_element(pid: int) -> object:
        del pid
        return APP

    return SimpleNamespace(app_element=app_element, attr=attr)


def test_combobox_field_editor_counts_as_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Excel 이름 상자: 포커스는 자식으로 안 보이는 필드 편집기에, 콤보박스 자신은 AXFocused=false."""
    node = UIElementNode(id="e1", role=Role.COMBOBOX, raw_role="AXComboBox")
    focused = node.model_copy(update={"states": frozenset({State.FOCUSED})})
    current = [node]

    def read(el: object) -> UIElementNode:
        del el
        return current[0]

    monkeypatch.setattr(A, "_read", read)
    monkeypatch.setattr(A, "ax", lambda: _fake_ax(EDITOR))
    assert A._has_focus(_live())
    assert not A._has_focus(_live(Role.GROUP))  # 콤보박스가 아닌 컨테이너 아래 글자 칸은 아니다
    monkeypatch.setattr(A, "ax", lambda: _fake_ax(WINDOW))  # 포커스가 창으로 빠졌다
    assert not A._has_focus(_live())
    monkeypatch.setattr(A, "ax", lambda: _fake_ax(BUTTON))  # 대상 아래의 다른 조작 요소 — 글자 칸이 아니면 아니다
    assert not A._has_focus(_live())
    monkeypatch.setattr(A, "ax", lambda: _fake_ax(TARGET))  # AXFocused 속성이 없어도 앱 포커스가 바로 그것
    assert A._has_focus(_live())
    current[0] = focused
    assert A._has_focus(_live())


def test_headed_type_leaves_editability_to_focus_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """headless는 입력 칸만, headed는 포커스를 받는 요소면(PowerPoint 슬라이드 · Excel 격자) 키를 보낸다."""
    seen: list[bool] = []

    def prepare(ctx: Any, ref: str, act: Any, timeout_ms: int, need: str | None = None, editable: bool = False) -> Any:
        del ctx, ref, act, timeout_ms, need
        seen.append(editable)
        raise CuopError("STALE_REF", "stop here")

    monkeypatch.setattr(A, "prepare", prepare)
    for headed in (False, True):
        with pytest.raises(CuopError):
            A.type_(SimpleNamespace(headed=headed), "@sab1:e1", "x")  # pyright: ignore[reportArgumentType]
    assert seen == [True, False]


def test_ignored_headless_insert_is_reported_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Word 본문: AXSelectedText 쓰기가 0을 돌려주고 무시된다 → 값이 그대로면 value_unchanged로 알린다."""
    node = UIElementNode(id="e1", role=Role.TEXTBOX, raw_role="AXTextArea", value="본문")
    live = SimpleNamespace(el=TARGET, entry=SimpleNamespace(pid=42), node=node, window=None)

    def prepare(*args: Any, **kw: Any) -> Any:
        del args, kw
        return live

    def read(el: object) -> UIElementNode:
        del el
        return node

    io = SimpleNamespace(set_attr=lambda el, name, v: 0, open_surfaces=lambda pid, w: [], ABSENT=frozenset())  # pyright: ignore[reportUnknownLambdaType]
    monkeypatch.setattr(A, "prepare", prepare)
    monkeypatch.setattr(A, "_read", read)
    monkeypatch.setattr(A, "_io", lambda: io)
    monkeypatch.setattr(A, "ax", lambda: SimpleNamespace(attr=lambda el, name: "본문"))  # pyright: ignore[reportUnknownLambdaType]
    out = A.type_(SimpleNamespace(headed=False), "@sab1:e1", "추가")  # pyright: ignore[reportArgumentType]
    assert out["details"]["verification_reason"] == "value_unchanged"
    assert out["disposition"]["delivery"] == "delivered_unverified"


def test_mouse_click_refuses_point_covered_by_disallowed_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Office가 뒤에 있으면 그 좌표를 다른 앱(허용 목록 밖) 창이 덮는다 — 누르지 않고 앞으로 가져오라고 알린다."""
    clicks: list[tuple[float, float]] = []

    def allowed(pid: int) -> None:
        raise CuopError("POLICY_DENIED", f"{pid} not allowed")

    def owner_at(x: float, y: float) -> int:
        del x, y
        return 7

    def mouse_click(x: float, y: float, *rest: object) -> None:
        del rest
        clicks.append((x, y))

    io = SimpleNamespace(owner_at=owner_at, mouse_click=mouse_click, window_at=lambda x, y: 9)  # pyright: ignore[reportUnknownLambdaType]
    monkeypatch.setattr(A, "_io", lambda: io)
    monkeypatch.setattr(A, "ax", lambda: SimpleNamespace(allowed=allowed))
    with pytest.raises(CuopError) as e:
        A.mouse_click(SimpleNamespace(headed=True), "100,200")  # pyright: ignore[reportArgumentType]
    assert e.value.to_json()["details"]["kind"] == "occluded_by_other_app"
    assert not clicks


def _excel(focused_dialog: bool) -> Any:
    """문서 창 하나 + 앱 모달 대화상자(Excel '이동') 하나 — 대화상자는 문서 창의 자식이 아니라 앱의 다른 창."""
    doc = SimpleNamespace(el="doc", id=1, subrole="AXStandardWindow", minimized=False, focused=not focused_dialog,
                          on_screen=True)  # fmt: skip
    dlg = SimpleNamespace(el="dlg", id=2, subrole="AXDialog", minimized=False, focused=focused_dialog, on_screen=True)
    focus = dlg if focused_dialog else doc
    attrs = {
        ("app", "AXFocusedWindow"): focus.el,
        ("dlg", "AXSubrole"): "AXDialog",
        ("doc", "AXSubrole"): "AXStandardWindow",
    }

    def attr(el: object, name: str) -> object:
        return attrs.get(("app" if el is APP else str(el), name))

    def window_number(el: object) -> int | None:
        return {"doc": 1, "dlg": 2}.get(str(el))

    m = SimpleNamespace(
        find_app=lambda name: SimpleNamespace(pid=42, name=name, policy=0),  # pyright: ignore[reportUnknownLambdaType]
        AXUIElementCreateApplication=lambda pid: APP,  # pyright: ignore[reportUnknownLambdaType]
        app_element=lambda pid: APP,  # pyright: ignore[reportUnknownLambdaType]
        pick_window=lambda pid, name: focus,  # pyright: ignore[reportUnknownLambdaType]
        app_windows=lambda pid: [doc, dlg],  # pyright: ignore[reportUnknownLambdaType]
        window_number=window_number,
        attr=attr,
    )
    return m, doc, dlg


@pytest.mark.parametrize("focused_dialog", [False, True])
def test_alert_surface_finds_app_modal_dialog(monkeypatch: pytest.MonkeyPatch, focused_dialog: bool) -> None:
    """Office C회차: list-surfaces는 alert라는데 `snapshot --surface alert`는 'no open alert'였다."""
    from cuop.desktop import commands as C

    m, _, dlg = _excel(focused_dialog)
    monkeypatch.setattr(C, "ax", lambda: m)
    assert C._resolve_target("Excel", None, "alert").el == dlg.el


def test_focus_failure_names_the_dialog_holding_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    """대화상자가 포커스를 쥐면 문서 창을 앞으로 못 가져온다 — 에이전트가 닫을 수 있게 그 창 id를 알려 준다."""
    m, doc, _ = _excel(focused_dialog=True)
    monkeypatch.setattr(A, "ax", lambda: m)
    e = A.not_front(A.Act("press"), 42, doc.el)
    assert e.details["focused_window"] == "w-2" and "dialog" in e.message
    m, doc, _ = _excel(focused_dialog=False)
    monkeypatch.setattr(A, "ax", lambda: m)
    assert "focused_window" not in A.not_front(A.Act("press"), 42, doc.el).details


def test_slide_editor_pairs_layout_ref_through_hidden_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """AXChildren으로는 스크롤 영역 바로 아래인데 AXParent는 숨은 AXGroup을 거친다 — layoutitem ref로도 같은 쌍."""
    scroll, group, layout, other = "scroll", "group", "layout", "other"
    attrs: dict[tuple[str, str], object] = {
        (layout, "AXParent"): group, (group, "AXParent"): scroll, (group, "AXRole"): "AXGroup",
        (scroll, "AXRole"): "AXScrollArea", (scroll, "AXChildren"): [layout], (layout, "AXRole"): "AXLayoutArea",
        (other, "AXParent"): group,
    }  # fmt: skip

    def attr(el: object, name: str) -> object:
        return attrs.get((str(el), name))

    app = SimpleNamespace(bundle_id=A.POWERPOINT)
    monkeypatch.setattr(A, "ax", lambda: SimpleNamespace(attr=attr, app_by_pid=lambda pid: app))  # pyright: ignore[reportUnknownLambdaType]

    def live(el: str, raw: str) -> Any:
        node = UIElementNode(id="e1", role=Role.GROUP, raw_role=raw)
        return SimpleNamespace(el=el, entry=SimpleNamespace(pid=42), node=node, window=None)

    assert A._slide_editor(live(layout, "AXLayoutArea")) == (scroll, layout)
    assert A._slide_editor(live(scroll, "AXScrollArea")) == (scroll, layout)
    assert A._slide_editor(live(other, "AXLayoutArea")) is None  # 그 스크롤 영역의 자식이 아니면 쌍이 아니다


def test_window_screenshot_reports_origin(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """창 스크린샷 픽셀 → 화면 point를 바꾸려면 창 위치가 필요하다(E회차: 픽셀 ÷ 2를 그대로 눌러 33pt 위를 눌렀다)."""
    from cuop.desktop import system as S
    from cuop.schema import Bounds

    win = SimpleNamespace(id=5, ref_id="w-5", bounds=Bounds(x=0, y=33, w=1470, h=923))
    moved = SimpleNamespace(id=5, ref_id="w-5", bounds=Bounds(x=10, y=33, w=1470, h=923))
    later: list[Any] = []
    display: dict[str, Any] = {"bounds": {}, "scale": 2.0}
    m = SimpleNamespace(screen_recording=lambda: True, displays=lambda: [display])
    s = SimpleNamespace(capture=lambda out, **kw: None, png_size=lambda out: (2940, 1846))  # pyright: ignore[reportUnknownLambdaType]

    def window(app: object, wid: object) -> Any:
        del app
        return later.pop(0) if wid and later else win

    def scale_at(displays: object, bounds: object) -> float:
        del displays, bounds
        return 2.0

    monkeypatch.setattr(S, "ax", lambda: m)
    monkeypatch.setattr(S, "_sys", lambda: s)
    monkeypatch.setattr(S, "_window", window)
    monkeypatch.setattr(S, "_scale_at", scale_at)
    monkeypatch.delenv("CUOP_ALLOW_APPS", raising=False)
    data = S.screenshot(None, path=str(tmp_path / "w.png"), app="Excel")  # pyright: ignore[reportArgumentType]
    assert data["origin"] == {"x": 0, "y": 33} and data["scale_factor"] == 2.0
    later.append(moved)  # 찍는 사이 창이 움직였다 → origin을 주지 않는다
    data = S.screenshot(None, path=str(tmp_path / "m.png"), app="Excel")  # pyright: ignore[reportArgumentType]
    assert "origin" not in data


def test_mouse_click_rechecks_owner_right_before_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    """활성화 뒤에도 그 점의 주인이 바뀌면(다른 앱 창이 덮음) 이벤트를 보내지 않는다(Codex Office 리뷰 P1)."""
    from cuop.collectors import ax_actions as X

    posted: list[object] = []
    monkeypatch.setattr(X, "check_point", lambda x, y: 7)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(X, "CGEventPost", lambda tap, ev: posted.append(ev))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    with pytest.raises(CuopError):
        X.mouse_click(10, 10, expect=42)
    assert posted == []


def test_harness_dismisses_only_known_buttons_of_a_fixture_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """하네스 정리: 시험 문서만 열려 있고 그 앱의 포커스 대화상자일 때 '취소'만 누른다(확인은 무관한 알림일 수도)."""
    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    T: Any = importlib.import_module("tasks_office")

    pressed: list[str] = []

    def setup(doc: str | None, buttons: list[str]) -> None:
        wins = [doc] if doc else []
        attrs: dict[tuple[str, str], object] = {
            ("app", "AXWindows"): [*wins, "dlg"], ("app", "AXFocusedWindow"): "dlg", ("dlg", "AXSubrole"): "AXDialog",
            ("dlg", "AXChildren"): buttons, **{(b, "AXRole"): "AXButton" for b in buttons},
            **{(b, "AXTitle"): b for b in buttons}, **{(w, "AXSubrole"): "AXStandardWindow" for w in wins},
        }  # fmt: skip
        monkeypatch.setattr(T.ax, "attr", lambda el, name: attrs.get((str(el), name)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        monkeypatch.setattr(T.ax, "app_element", lambda pid: "app")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        monkeypatch.setattr(T, "_fixture", lambda w: w == "fixture")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        monkeypatch.setattr(T, "AXUIElementPerformAction", lambda el, a: pressed.append(str(el)) or 0)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        monkeypatch.setattr(T.time, "sleep", lambda s: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    for doc, buttons in (
        (None, ["취소"]),
        ("user-doc", ["취소"]),
        ("fixture", ["삭제"]),
        ("fixture", ["확인"]),
        ("fixture", ["확인", "삭제"]),
    ):
        setup(doc, buttons)
        T._dismiss_dialogs(1)
    assert pressed == []  # 문서 없음 · 사용자 문서 · 모르는 단추 · '확인'은 누르지 않는다
    setup("fixture", ["특수...", "확인", "취소"])
    T._dismiss_dialogs(1)
    assert pressed == ["취소"]


def test_fixture_writer_never_replaces_a_foreign_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """시험 문서 자리에 하네스가 만들지 않은 파일 · 링크가 있으면 지우지 않고 run을 뺀다(Codex Office 병합 판정)."""
    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    T: Any = importlib.import_module("tasks_office")
    monkeypatch.setattr(T, "DIR", tmp_path)
    T.make("xlsx")
    assert T._ours(tmp_path / "cuop-test.xlsx")
    T.make("xlsx")  # 자기 시험 문서는 갈아 끼운다
    (tmp_path / "cuop-test.docx").write_bytes(b"user data")
    with pytest.raises(RuntimeError):
        T.make("docx")
    assert (tmp_path / "cuop-test.docx").read_bytes() == b"user data"
    target = tmp_path / "elsewhere.pptx"
    target.write_bytes(b"keep")
    (tmp_path / "cuop-test.pptx").symlink_to(target)
    with pytest.raises(RuntimeError):
        T.make("pptx")
    assert target.read_bytes() == b"keep"


def test_cg_windows_keep_modal_panels(monkeypatch: pytest.MonkeyPatch) -> None:
    """샌드박스 앱(Word)의 열기 패널은 CG 레이어 8 — 일반 창과 함께 앱 창으로 본다(메뉴 · 상태 막대 레이어는 뺀다)."""
    from cuop.collectors import ax

    rows = [{"kCGWindowLayer": layer, "kCGWindowNumber": n} for n, layer in ((1, 0), (2, 8), (3, 25), (4, 101))]

    def listing(*_: object) -> list[dict[str, int]]:
        return rows

    monkeypatch.setattr(ax, "CGWindowListCopyWindowInfo", listing)
    assert [w["kCGWindowNumber"] for w in ax.cg_windows()] == [1, 2]


def test_pick_window_prefers_document_over_unfocused_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    from cuop.collectors import ax

    def win(i: int, sub: str) -> Any:
        return ax.Window(el=i, id=i, title=None, pid=1, bounds=None, minimized=False, focused=False, on_screen=True,
                         subrole=sub)  # fmt: skip

    def wins(_pid: int) -> list[Any]:
        return [win(1, "AXStandardWindow"), win(2, "AXDialog")]

    monkeypatch.setattr(ax, "app_windows", wins)
    pick = ax.pick_window(1, "Word")
    assert pick is not None and pick.id == 1


def test_allow_list_blocks_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    from cuop.collectors import ax

    monkeypatch.setenv("CUOP_ALLOW_APPS", "com.microsoft.Word")
    ax.allowed_bundle("com.microsoft.Word")
    with pytest.raises(CuopError):
        ax.allowed_bundle("com.apple.finder")


def test_inline_screenshot_size_is_points_and_capped() -> None:
    from cuop.desktop.system import inline_size

    assert inline_size(1600, 1000, 2.0) == (800, 500)  # Retina → 1px = 1pt
    assert inline_size(2940, 1846, 2.0) == (1353, 849)  # 1470×923pt는 1.15MP 상한으로 더 줄인다
    w, h = inline_size(5120, 2880, 1.0)  # 큰 1x 화면 → 긴 변 · 화소 상한
    assert w <= 1568 and w * h <= 1.15e6
    assert inline_size(400, 300, 1.0) == (400, 300)  # 이미 작으면 그대로


def test_untargeted_key_respects_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """press에 앱 · 창이 없으면 키는 맨 앞 앱으로 간다 — 그 앱이 허용 목록 밖이면 보내지 않는다(INSTRUCTIONS 리뷰)."""
    from cuop.collectors import ax_actions as X

    posted: list[int] = []
    monkeypatch.setattr(X, "frontmost_pid", lambda: 4242)

    def no_guard(_pid: int) -> None:
        return None

    def deny(_pid: int) -> None:
        raise CuopError("POLICY_DENIED", "outside")

    def post(pid: int, _ev: object) -> None:
        posted.append(pid)

    monkeypatch.setattr(X, "guard", no_guard)
    monkeypatch.setattr(X, "allowed", deny)
    monkeypatch.setattr(X, "CGEventPostToPid", post)
    with pytest.raises(CuopError):
        X.key_combo("return", None)
    assert posted == []
