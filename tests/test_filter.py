from pathlib import Path

from cuop.optimizer.compress import render_snapshot
from cuop.optimizer.diff import diff
from cuop.optimizer.filter import prune, task_filter
from cuop.schema import Role, State, UIElementNode, UISnapshot

CALCULATOR = Path(__file__).parent / "fixtures" / "calculator"  # 0–4: 메뉴 모두 닫힘 · 5: 보기 메뉴 열림


def _load() -> list[UISnapshot]:
    return [UISnapshot.model_validate_json(p.read_bytes()) for p in sorted(CALCULATOR.glob("*.json"))]


def _named(node: UIElementNode, name: str) -> UIElementNode:
    return next(n for n in node.walk() if n.name == name)


def test_prune_folds_closed_menus_but_keeps_menu_bar_and_open_menu() -> None:
    snaps = [prune(s) for s in _load()]
    for s in snaps:
        menubar = next(n for n in s.root.walk() if n.role is Role.MENUBAR)
        assert [c.name for c in menubar.children] == ["계산기", "편집", "보기", "윈도우", "도움말"]
    assert not any(n.role is Role.MENU for s in snaps[:5] for n in s.root.walk())
    view_menu = _named(snaps[5].root, "보기").children[0]
    items = [c.name for c in view_menu.children]
    assert items[:3] == ["기본", "공학용", "프로그래머"] and None not in items  # 구분선 제거
    assert _named(view_menu, "소수점 이하").children == []  # 열린 메뉴 안의 닫힌 하위 메뉴는 접힘


def test_prune_only_removes_and_hoists_with_ids_kept() -> None:
    for raw in _load():
        before = {n.id: n.name for n in raw.root.walk()}
        after = {n.id: n.name for n in prune(raw).root.walk()}
        assert after.items() <= before.items()  # 새 노드나 이름 변경 없음 — Diff가 기대는 ID 그대로
        window = next(n for n in prune(raw).root.walk() if n.role is Role.WINDOW)
        assert window.children[0].id == "e6"  # 감싸기만 하던 group 3겹을 끌어올림


def test_prune_is_deterministic() -> None:
    a, b = _load(), _load()  # 따로 읽은 같은 입력
    assert [render_snapshot(prune(s)) for s in a] == [render_snapshot(prune(s)) for s in b]


def _node(i: int, role: Role, name: str | None = None, *states: State) -> UIElementNode:
    return UIElementNode(id=f"e{i}", role=role, raw_role="AX", name=name, states=frozenset(states))


def test_task_filter_ranks_focus_then_goal_then_order_within_budget() -> None:
    kids = [_node(i + 3, Role.BUTTON, f"b{i}") for i in range(10)]  # e3–e12
    kids += [_node(13, Role.TEXTBOX, None, State.FOCUSED), _node(14, Role.BUTTON, "저장…"), _node(15, Role.GENERIC)]
    window = UIElementNode(id="e2", role=Role.WINDOW, raw_role="AXWindow", children=kids)
    root = UIElementNode(id="e1", role=Role.GENERIC, raw_role="AXApplication", children=[window])
    view = task_filter(UISnapshot(id="s1", platform="macos", app="a", root=root), "저장 버튼 누르기", {"e9"}, budget=3)
    # keep(e9)은 예산 밖. 예산 3 = 포커스(e13) · goal 매칭(e14) · 문서 순서 첫 조작 요소(e3). 나머지 9개는 "… +9"
    assert render_snapshot(view).splitlines() == [
        "[s1] a", "e2 window", ' e3 button "b0"', ' e9 button "b6"',
        " e13 textbox [focused]", ' e14 button "저장…"', " … +9",
    ]  # fmt: skip


def test_task_filter_keeps_sent_and_hidden_nodes_so_only_goal_changes_add() -> None:
    v0, v1, v2 = (prune(s) for s in _load()[:3])

    def ids(v: UISnapshot) -> set[str]:
        return {n.id for n in v.root.walk()}

    first = task_filter(v0, "2 더하기 5", set(), budget=5)
    assert {"2", "더하기", "5"} <= {n.name for n in first.root.walk()}
    same = task_filter(v1, "2 더하기 5", ids(first), ids(v0), budget=5)
    d = diff(first, same)
    assert d.added == [] and d.removed == []  # 예산이 남아도 직전에 숨긴 노드는 새어 들어오지 않는다
    moved_on = task_filter(v2, "보기 메뉴", ids(same), set(), budget=2)  # goal이 바뀌면 서버가 seen을 비운다
    d = diff(same, moved_on)
    assert d.removed == []  # goal이 바뀌어 관련이 없어진 노드도 화면에 있으면 남는다
    added = {n.name for a in d.added for n in a.node.walk()} - {None}  # None = 맥락으로 붙은 조상(툴바 · 메뉴 막대)
    assert added == {"사이드바 보기", "보기"}  # 예산 2 = goal 단어 '보기'와 매칭된 노드가 먼저


def test_task_filter_does_not_trickle_hidden_matches_under_same_goal() -> None:
    kids = [_node(i + 3, Role.BUTTON, f"저장 {i}") for i in range(10)]  # 전부 goal과 매칭, 예산보다 많다
    root = UIElementNode(id="e1", role=Role.GENERIC, raw_role="AXApplication", children=kids)
    snap = UISnapshot(id="s1", platform="macos", app="a", root=root)
    first = task_filter(snap, "저장", set(), budget=3)
    again = task_filter(snap, "저장", {n.id for n in first.root.walk()}, {n.id for n in snap.root.walk()}, budget=3)
    assert diff(first, again).is_empty  # 숨긴 매칭 노드 7개가 다음 관찰 예산으로 들어오지 않는다


def test_task_filter_on_textedit_folds_ruler_and_keeps_format_bar() -> None:
    textedit = Path(__file__).parent / "fixtures" / "textedit"  # 빈 문서 · 볼드체 · 중앙 정렬 (record_textedit.py)
    snap = prune(UISnapshot.model_validate_json(sorted(textedit.glob("*.json"))[0].read_bytes()))
    for goal in ["볼드체 켜고 중앙 정렬", "cuop-fixture.rtf 문서에서 볼드체 켜기"]:  # 둘째: 창 제목이 goal에 있어도
        view = task_filter(snap, goal, set())
        assert {"볼드체", "중앙 정렬", "파일"} <= {n.name for n in view.root.walk()}
        assert not any(n.role is Role.GENERIC and n.value for n in view.root.walk())  # 눈금자 · 스크롤 막대 값
        assert "  … +18" in render_snapshot(view).splitlines()  # 텍스트 영역 옆에 접힌 자리 표시
