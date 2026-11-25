"""Fallback 판정 · Partial Vision 좌표계 — OS 없이."""

import pytest

from cuop.optimizer import fallback
from cuop.schema import Bounds, Role, UIElementNode, UISnapshot


def node(i: int, role: Role, name: str | None = None, bounds: Bounds | None = None, *kids: UIElementNode):
    return UIElementNode(id=f"e{i}", role=role, raw_role="?", name=name, bounds=bounds, children=list(kids))


def app(*buttons: str) -> UISnapshot:
    kids = [node(10 + i, Role.BUTTON, b, Bounds(x=110 + 20 * i, y=60, w=10, h=10)) for i, b in enumerate(buttons)]
    win = node(2, Role.WINDOW, "그림판", Bounds(x=100, y=50, w=400, h=300), node(3, Role.GROUP), *kids)
    root = node(1, Role.GENERIC, None, None, node(9, Role.MENUBAR), win)  # 메뉴 막대가 앞이어도 창을 고른다
    return UISnapshot(id="s1", platform="macos", app="paint", root=root)


def test_sparse_window_means_the_tree_tells_little() -> None:
    controls = app("CloseButton", "MinimizeButton", "FullScreenButton")  # 창 제어 버튼 + 캔버스 group뿐
    win = fallback.window(controls)
    assert win is not None and win.id == "e2" and fallback.sparse(win)
    full = fallback.window(app("CloseButton", "MinimizeButton", "FullScreenButton", "붓", "지우개"))
    assert full is not None and not fallback.sparse(full)


def test_web_document_is_its_own_window() -> None:
    view = Bounds(x=0, y=0, w=800, h=600)
    canvas = node(1, Role.DOCUMENT, "그림", view, node(2, Role.HEADING, "색 고르기"), node(3, Role.CANVAS))
    snap = UISnapshot(id="ws1", platform="web", app="http://127.0.0.1/canvas.html", root=canvas)
    assert fallback.window(snap) is canvas and fallback.sparse(canvas)
    assert fallback.region(snap, "window") == view
    unbounded = snap.model_copy(update={"root": canvas.model_copy(update={"bounds": None})})
    assert fallback.window(unbounded) is None  # 영역을 모르는 문서(녹화 fixture)는 판정하지 않는다


def test_region_is_the_window_or_an_element() -> None:
    snap = app("CloseButton", "붓")
    assert fallback.region(snap, "window") == Bounds(x=100, y=50, w=400, h=300)
    assert fallback.region(snap, "e11") == Bounds(x=130, y=60, w=10, h=10)
    with pytest.raises(LookupError):
        fallback.region(snap, "e3")  # bounds 없는 요소 (web 노드도 같은 경우)


def test_frame_keeps_one_pixel_per_point_and_maps_back() -> None:
    f = fallback.frame(Bounds(x=100, y=50, w=400, h=300))
    assert (f.width, f.height) == (400, 300) and fallback.image_tokens(f) == 160
    assert fallback.to_screen(f, 0, 0) == (100, 50) and fallback.to_screen(f, 399, 299) == (499, 349)
    with pytest.raises(ValueError):
        fallback.to_screen(f, 400, 0)  # 이미지 밖


def test_big_area_is_shrunk_below_the_model_limits() -> None:
    f = fallback.frame(Bounds(x=0, y=25, w=2560, h=1600))
    assert max(f.width, f.height) <= fallback.MAX_EDGE and f.width * f.height <= fallback.MAX_PIXELS
    assert fallback.to_screen(f, f.width // 2, f.height // 2) == pytest.approx((1280, 825), abs=2)
