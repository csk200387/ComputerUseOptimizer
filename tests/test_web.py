"""web 수집기의 CDP 접근성 트리 변환 — 브라우저 없이. 노드 모양은 Edge 156 스파이크 출력(2026-11-02)을 줄인 것."""

import itertools
from typing import Any

from cuop.collectors.web import boxes, build_tree, unit
from cuop.optimizer.compress import render_tree
from cuop.schema import Bounds, Role, State


def ax(nid: int, role: str, name: str = "", kids: tuple[int, ...] = (), **extra: Any) -> dict[str, Any]:
    props = [{"name": k, "value": {"value": v}} for k, v in extra.pop("props", {}).items()]
    node = {"nodeId": str(nid), "role": {"value": role}, "name": {"value": name}, "childIds": [str(k) for k in kids]}
    return node | {"backendDOMNodeId": nid, "properties": props} | extra


def page(status: str, password: str = "") -> list[dict[str, Any]]:
    return [
        ax(1, "RootWebArea", "주문", (2,), props={"focused": True}),
        ax(2, "none", "", (3, 6, 9, 12, 14), ignored=True),  # ignored → 자식을 부모에 붙인다
        ax(3, "textbox", "이름", (4,), value={"value": "홍"}),
        ax(4, "generic", "", (5,)),  # 입력 칸 안쪽 편집기 → 뺀다
        ax(5, "StaticText", "홍"),
        ax(6, "textbox", "비밀번호", (7,), value={"value": password} if password else {}),
        ax(7, "generic", "", (8,)),
        ax(8, "StaticText", password),
        ax(9, "combobox", "수량", (10,), value={"value": "1"}, props={"expanded": False}),
        ax(10, "MenuListPopup", "", (11,)),
        ax(11, "option", "1", props={"selected": True}),
        ax(12, "checkbox", "포장", props={"checked": "true"}),
        ax(14, "paragraph", "", (15,)),
        ax(15, "StaticText", status, (16,)),
        {"nodeId": "16", "role": {"value": "InlineTextBox"}, "name": {"value": status}},  # 글자 배치 → 뺀다
    ]


def test_build_tree_normalizes_and_hides_passwords() -> None:
    ids: dict[int, str] = {}
    root = build_tree(page("대기", "••••••••"), ids, itertools.count(1))
    assert render_tree(root) == [
        'w1 document "주문" [focused]',
        ' w2 textbox "이름" ="홍"',
        ' w3 textbox "비밀번호" ="<hidden>"',  # 브라우저가 •로 가린 값도 길이째 숨긴다
        ' w4 combobox "수량" ="1" [collapsed]',
        "  w5 listbox",
        '   w6 option "1" [selected]',
        ' w7 checkbox "포장" [checked]',
        " w8 generic",
        '  w9 text "대기"',
    ]
    assert root.children[2].role is Role.COMBOBOX and State.CHECKED in root.children[3].states


def test_ids_follow_dom_nodes_and_never_collide_across_tabs() -> None:
    seq, tab_a, tab_b = itertools.count(1), dict[int, str](), dict[int, str]()
    first = build_tree(page("대기"), tab_a, seq)
    again = build_tree(page("눌림"), tab_a, seq)  # 같은 DOM 노드 → 같은 ID, 바뀐 건 이름뿐
    assert [n.id for n in first.walk()] == [n.id for n in again.walk()]
    other = build_tree(page("대기"), tab_b, seq)  # 다른 탭은 backendDOMNodeId가 같아도 새 ID
    assert not {n.id for n in other.walk()} & {n.id for n in first.walk()}


def test_prune_drops_text_that_repeats_a_name() -> None:
    from cuop.optimizer.filter import prune
    from cuop.schema import UISnapshot

    label = [ax(20, "LabelText", "", (21, 22)), ax(21, "StaticText", "수량 "), ax(22, "combobox", "수량")]
    button = [ax(23, "button", "주문하기", (24,)), ax(24, "StaticText", "주문하기")]
    nodes = [ax(1, "RootWebArea", "주문", (20, 23, 25)), *label, *button, ax(25, "StaticText", "대기")]
    root = build_tree(nodes, {}, itertools.count(1))
    view = prune(UISnapshot(id="ws1", platform="web", app="shop.html", root=root))
    assert render_tree(view.root) == [
        'w1 document "주문"',
        ' w4 combobox "수량"',  # 라벨 글자가 빠져 감싸던 LabelText도 끌어올려짐
        ' w5 button "주문하기"',
        ' w7 text "대기"',  # 아무 이름도 되풀이하지 않는 글자는 남는다
    ]


def test_prune_drops_decorative_leaves() -> None:
    from cuop.optimizer.filter import prune
    from cuop.schema import UISnapshot

    item = [ax(30, "listitem", "", (31, 32)), ax(31, "ListMarker", "• "), ax(32, "link", "노트")]
    nodes = [ax(1, "RootWebArea", "분류", (30, 33)), *item, ax(33, "StaticText", " ")]
    view = prune(UISnapshot(id="ws1", platform="web", app="a", root=build_tree(nodes, {}, itertools.count(1))))
    assert render_tree(view.root) == ['w1 document "분류"', " w2 listitem", '  w4 link "노트"']


def test_boxes_are_viewport_pixels_of_the_visible_part() -> None:
    """DOMSnapshot 영역은 문서 좌표 → 스크롤을 빼고 보이는 부분만 (2026-11-04 스파이크 값)."""
    layout = {
        "documents": [{
            "scrollOffsetX": 0, "scrollOffsetY": 700,
            "nodes": {"backendNodeId": [1, 3, 12, 15]},
            "layout": {"nodeIndex": [1, 2, 3], "bounds": [[100, 900, 120, 40], [50, 1250, 300, 150], [0, 0, 80, 20]]},
        }]
    }  # fmt: skip
    areas = boxes(layout, Bounds(x=0, y=0, w=800, h=600))
    assert areas == {
        3: Bounds(x=100, y=200, w=120, h=40),
        12: Bounds(x=50, y=550, w=300, h=50),  # 아래가 잘린 캔버스
    }  # 15는 스크롤로 화면 위로 지나감 → 없음
    root = build_tree(page("대기") + [ax(13, "Canvas")], {}, itertools.count(1), areas)
    assert root.children[0].bounds == areas[3] and root.children[1].bounds is None


def test_boxes_on_retina_are_device_pixels() -> None:
    """화면 Chrome은 DOMSnapshot을 기기 픽셀로 준다 — 캔버스 CSS (40, 120) 300×200, 50 스크롤 (스파이크 값)."""
    layout = {
        "documents": [{
            "scrollOffsetX": 0, "scrollOffsetY": 100, "contentWidth": 2560,
            "nodes": {"backendNodeId": [1, 7]},
            "layout": {"nodeIndex": [1], "bounds": [[80, 240, 600, 400]]},
        }]
    }  # fmt: skip
    k = unit(layout, 1280)
    assert k == 2 and boxes(layout, Bounds(x=0, y=0, w=1280, h=720), k) == {7: Bounds(x=40, y=70, w=300, h=200)}


def test_canvas_role_is_recognized() -> None:
    nodes = [ax(1, "RootWebArea", "그림", (2,)), ax(2, "Canvas")]
    assert build_tree(nodes, {}, itertools.count(1)).children[0].role is Role.CANVAS
