from pathlib import Path

from cuop.optimizer.compress import render_diff
from cuop.optimizer.diff import diff
from cuop.optimizer.filter import prune
from cuop.schema import Role, State, UIElementNode, UISnapshot

CALCULATOR = Path(__file__).parent / "fixtures" / "calculator"  # 시작 · 7 · + · 3 · = · 보기 메뉴 열기
DISPLAY, AC, VIEW = "e8", "e10", "e158"  # 디스플레이 텍스트 · AC 버튼 · 보기 메뉴 막대 항목


def _views() -> list[UISnapshot]:
    return [prune(UISnapshot.model_validate_json(p.read_bytes())) for p in sorted(CALCULATOR.glob("*.json"))]


def test_calculator_transitions() -> None:
    v = _views()
    d = [diff(a, b) for a, b in zip(v, v[1:], strict=False)]
    clean = str.maketrans("", "", "‎")  # LRM
    assert [[(c.id, (c.name[1] or "").translate(clean)) for c in x.changed if c.name] for x in d] == [
        [(DISPLAY, "7"), (AC, "지우기")],
        [(DISPLAY, "7+")],
        [(DISPLAY, "7+3")],
        [(DISPLAY, "10"), (AC, "모두 지우기")],
        [],
    ]
    assert [x.removed for x in d] == [[]] * 5
    assert [[(a.parent_id, a.node.name or a.node.role) for a in x.added] for x in d] == [
        [], [], [], [("e6", "최근 표현식")], [(VIEW, Role.MENU)]
    ]  # fmt: skip
    assert [(c.id, c.states_on) for c in d[4].changed] == [(VIEW, {State.SELECTED})]
    closed = diff(v[5], v[4])  # 메뉴를 닫으면 메뉴 서브트리 루트 하나만 삭제
    assert closed.removed == [d[4].added[0].node.id] and closed.changed[0].states_off == {State.SELECTED}
    assert diff(v[0], v[0]).is_empty


def test_render_diff() -> None:
    v = _views()
    assert render_diff(v[4], diff(v[3], v[4])).translate(str.maketrans("", "", "‎")) == "\n".join([
        '[s6 since s5] "계산기"',
        '~ e8 "10"',
        '~ e10 "모두 지우기"',
        "+ e6 first",  # 기존 형제들 앞에 끼었다
        ' e241 group "최근 표현식"',
        '  e242 text "7+3"',
    ])  # fmt: skip
    assert render_diff(v[0], diff(v[0], v[0])) == '[s2 since s2] "계산기"\n(no change)'


def _n(eid: str, *children: UIElementNode, name: str | None = None) -> UIElementNode:
    return UIElementNode(id=eid, role=Role.GROUP, raw_role="AXGroup", name=name, children=list(children))


def _snap(sid: str, *top: UIElementNode) -> UISnapshot:
    return UISnapshot(id=sid, platform="macos", app="a", root=_n("e1", *top))


def test_reparented_node_is_removed_and_readded() -> None:
    # 감싸던 group e3이 사라지고 e4가 e2 바로 아래로 올라옴 → e3 삭제(서브트리째) + e4를 e2 아래 다시 추가
    leaf = _n("e5", name="leaf")
    base = _snap("s1", _n("e2", _n("e3", _n("e4", leaf, name="kept"), name="wrapper")))
    view = _snap("s2", _n("e2", _n("e4", leaf, name="kept")))
    d = diff(base, view)
    assert d.removed == ["e3"] and [(a.parent_id, a.node.id) for a in d.added] == [("e2", "e4")] and not d.changed


def test_render_diff_groups_siblings_under_one_header() -> None:
    base = _snap("s1", _n("e2", name="list"))
    view = _snap("s2", _n("e2", _n("e3", name="a"), _n("e4", name="b"), name="list"))
    assert render_diff(view, diff(base, view)) == '[s2 since s1]\n+ e2\n e3 group "a"\n e4 group "b"'


def test_sibling_reorder_is_reported() -> None:
    """정렬처럼 같은 행들이 순서만 바뀌면 (no change)가 아니라 ^ 줄로 새 순서를 보낸다 (시나리오 C1)."""
    from cuop.optimizer.compress import render_diff
    from cuop.schema import Role, UIElementNode, UISnapshot

    def snap(sid: str, order: list[str]) -> UISnapshot:
        rows = [UIElementNode(id=i, role=Role.ROW, raw_role="AXRow", name=i) for i in order]
        table = UIElementNode(id="t", role=Role.TABLE, raw_role="AXTable", children=rows)
        return UISnapshot(
            id=sid,
            platform="web",
            app="x",
            root=UIElementNode(id="r", role=Role.WINDOW, raw_role="w", children=[table]),
        )

    d = diff(snap("s1", ["a", "b", "c"]), snap("s2", ["c", "a", "b"]))
    assert not d.is_empty and d.reordered[0].parent_id == "t" and d.reordered[0].order == ["c", "a", "b"]
    assert "^ t: c a b" in render_diff(snap("s2", ["c", "a", "b"]), d)
    assert diff(snap("s1", ["a", "b"]), snap("s2", ["a", "b"])).is_empty


def test_insertion_and_move_are_unambiguous() -> None:
    """[A, B] → [A, C, B]는 C의 위치를, 부모를 옮긴 노드는 삭제를 먼저 적는다 (Codex M5 #16 · #17)."""
    from cuop.optimizer.compress import render_diff
    from cuop.schema import Role, UIElementNode, UISnapshot

    def node(i: str, *kids: UIElementNode) -> UIElementNode:
        return UIElementNode(id=i, role=Role.GROUP, raw_role="g", name=i, children=list(kids))

    def snap(sid: str, root: UIElementNode) -> UISnapshot:
        return UISnapshot(id=sid, platform="web", app="x", root=root)

    d = diff(snap("s1", node("r", node("a"), node("b"))), snap("s2", node("r", node("a"), node("c"), node("b"))))
    assert render_diff(snap("s2", node("r")), d).splitlines()[1] == "+ top after a"
    before = snap("s1", node("r", node("p", node("m")), node("q")))
    after = snap("s2", node("r", node("p"), node("q", node("m"))))
    moved = render_diff(after, diff(before, after))
    assert moved.index("- m") < moved.index("+ q")


def test_reorder_with_insertion_keeps_new_node_and_omission_changes_show() -> None:
    """감사 #11: 정렬 + 삽입이면 `^` 순서에 새 노드도 · 숨은 자식 수(`… +N`)가 바뀌면 `~ id … +N`."""
    from cuop.optimizer.compress import render_diff
    from cuop.schema import Role, UIElementNode, UISnapshot

    def snap(sid: str, order: list[str], hidden: int = 0) -> UISnapshot:
        rows = [UIElementNode(id=i, role=Role.ROW, raw_role="AXRow", name=i) for i in order]
        table = UIElementNode(id="t", role=Role.TABLE, raw_role="AXTable", children=rows, omitted=hidden)
        return UISnapshot(id=sid, platform="web", app="x",
                          root=UIElementNode(id="r", role=Role.WINDOW, raw_role="w", children=[table]))  # fmt: skip

    d = diff(snap("s1", ["a", "b"]), snap("s2", ["b", "n", "a"]))
    assert d.reordered[0].order == ["b", "n", "a"]
    text = render_diff(snap("s2", ["b", "n", "a"]), diff(snap("s1", ["a"], 4), snap("s2", ["a"], 7)))
    assert text.splitlines()[1] == "~ t … +7"
