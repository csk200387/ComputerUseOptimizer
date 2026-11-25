from pathlib import Path

from cuop.schema import State, UIElementNode, UISnapshot

CALCULATOR = Path(__file__).parent / "fixtures" / "calculator"  # 녹화: fixtures/record_calculator.py


def _named(snap: UISnapshot, name: str) -> UIElementNode:
    return next(n for n in snap.root.walk() if n.name == name)


def test_calculator_fixture() -> None:
    snaps = [UISnapshot.model_validate_json(p.read_bytes()) for p in sorted(CALCULATOR.glob("*.json"))]
    displays = [_named(s, "필드 편집").children[0] for s in snaps]
    assert len({d.id for d in displays}) == 1  # 값이 바뀌어도 같은 요소는 같은 ID — Diff의 전제
    assert [(d.name or "").replace("‎", "") for d in displays] == ["0", "7", "7+", "7+3", "10", "10"]
    # 열린 메뉴만 크기가 있다. AXExpanded는 열려도 false라 수집기가 메뉴 계열에는 collapsed를 붙이지 않는다
    view = [_named(s, "보기") for s in snaps]
    assert [State.SELECTED in v.states for v in view] == [False] * 5 + [True]
    assert [bool((b := v.children[0].bounds) and b.w) for v in view] == [False] * 5 + [True]
    assert not any(State.COLLAPSED in n.states for s in snaps for n in s.root.walk())
    assert not any(n.name == "Apple" for s in snaps for n in s.root.walk())  # 수집기가 Apple 메뉴를 뺀다


def test_web_fixture() -> None:
    files = sorted((Path(__file__).parent / "fixtures" / "web").glob("*.json"))  # 녹화: fixtures/record_web.py
    snaps = [UISnapshot.model_validate_json(p.read_bytes()) for p in files]
    assert all(s.platform == "web" and s.app == "benchmarks/pages/shop.html" for s in snaps)  # 사용자 경로 없음
    assert not any("/Users/" in p.read_text() for p in files)
    status = [next(n.name for n in s.root.walk() if (n.name or "").startswith("주문 ")) for s in snaps]
    assert status[-1] == "주문 완료: 2개, 포장 함, 받는 분 테스트"
    qty = [next(n for n in s.root.walk() if n.name == "수량") for s in snaps]
    assert len({q.id for q in qty}) == 1 and [q.value for q in qty] == ["1", "2", "2", "2", "2"]
