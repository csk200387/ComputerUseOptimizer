"""관찰 대상 노드 선별.

- prune: task와 무관한 구조적 가지치기 (비교군 ③ compact, ④의 1단계)
  닫힌 메뉴 · 구분선 · 이름을 되풀이하는 글자 · 장식용 글자 · 빈 group
- task_filter: goal 기반 선별 (④의 2단계, prune 결과에 적용)
"""

import re

from cuop.optimizer.compress import plain
from cuop.schema import Role, State, UIElementNode, UISnapshot

BUDGET = 30  # ponytail: 관찰 1회에 새로 보낼 노드 상한(고정). 화면 크기별 조정이나 Jev 점수로 바꿀 자리
ACTIONABLE = frozenset({
    Role.BUTTON, Role.LINK, Role.CHECKBOX, Role.RADIO, Role.TEXTBOX, Role.COMBOBOX, Role.LISTBOX, Role.OPTION,
    Role.SLIDER, Role.MENUITEM, Role.TAB, Role.TREEITEM, Role.ROW, Role.CELL, Role.LISTITEM, Role.TEXT, Role.HEADING,
})  # fmt: skip


def _closed_menu(node: UIElementNode) -> bool:
    # 닫힌 메뉴는 크기 0 (AXExpanded는 열려도 false라 상태로는 판단 불가).
    # 앱이 뒤에 있으면 메뉴 막대 항목도 크기 0이므로 메뉴 자신만 본다
    b = node.bounds
    return node.role is Role.MENU and b is not None and not (b.w and b.h)


def _key(s: str | None) -> str:
    return plain(s or "").strip()


def _echo(text: UIElementNode, parent: UIElementNode, siblings: list[UIElementNode]) -> bool:
    """부모나 옆 요소의 이름을 되풀이하는 글자 노드 — 웹의 `button > StaticText "저장"`,
    `label > StaticText "이름 " + textbox "이름"`. 같은 내용이 이름으로 이미 전달된다."""
    if text.role is not Role.TEXT or text.children or not (t := _key(text.name)):
        return False
    return t == _key(parent.name) or any(s.role is not Role.TEXT and _key(s.name) == t for s in siblings)


def _decor(node: UIElementNode) -> bool:
    """글자 · 숫자가 없는 장식용 잎 노드 — 웹의 목록 기호 `"• "`, 링크 사이 공백 `" "`."""
    plain_leaf = node.role in (Role.TEXT, Role.GENERIC) and not (node.children or node.value or node.states)
    return plain_leaf and not any(ch.isalnum() for ch in node.name or "")


def _kids(node: UIElementNode) -> list[UIElementNode]:
    children = [c for c in map(_prune, node.children) if c is not None]
    return [c for c in children if not (_echo(c, node, children) or _decor(c))]


def _prune(node: UIElementNode) -> UIElementNode | None:
    if _closed_menu(node):
        return None  # 메뉴 막대 항목(이름)은 남아 에이전트가 열 수 있다
    children = _kids(node)
    if node.role is Role.MENUITEM and not (node.name or node.value or children) and State.DISABLED in node.states:
        return None  # 구분선
    bare = not (node.name or node.value or node.description or node.states)
    if bare and node.role in (Role.GROUP, Role.GENERIC) and len(children) <= 1:
        return children[0] if children else None  # 감싸기만 하는 group은 끌어올림 — 자식 ID 유지
    return node.model_copy(update={"children": children})


def prune(snap: UISnapshot) -> UISnapshot:
    """루트(앱)는 헤더로 표시되므로 남기고 그 자식부터 가지치기한다."""
    return snap.model_copy(update={"root": snap.root.model_copy(update={"children": _kids(snap.root)})})


def _matches(node: UIElementNode, goal: str, words: list[str]) -> bool:
    """이름 · 값이 goal 안에 있거나("2" ⊂ "2 더하기 5"), goal 단어가 이름 · 값 안에 있다("저장" ⊂ "저장…")."""
    fields = [plain(f).lower() for f in (node.name, node.value) if f]
    return any(f in goal or any(w in f for w in words) for f in fields if f)


def task_filter(
    view: UISnapshot,
    goal: str,
    keep: set[str],
    seen: set[str] | None = None,
    budget: int = BUDGET,
    scores: dict[str, float] | None = None,
) -> UISnapshot:
    """goal과 관련된 노드를 고르고 조상은 맥락으로 남긴다. 뺀 자손 수는 부모의 omitted에 담는다.

    - keep(에이전트가 이미 받은 노드)은 화면에 있는 한 예산과 관계없이 남긴다. 그래서 Diff의 removed에는
      필터에서 빠진 노드가 아니라 UI에서 실제로 사라진 노드만 들어간다.
    - seen(같은 goal로 한 직전 관찰에서 화면에 있던 노드) 중 그때 뺀 노드는 포커스가 아니면 계속 뺀다.
      예산은 UI에 새로 나타난 노드(대화상자 · 열린 메뉴 등)에 쓰이고, 숨긴 노드가 관찰마다 새어 들어오지 않는다.
      goal이 바뀌면 호출 측이 seen을 비운다 → 새 goal로 다시 고른다.
    점수: 포커스 3 · goal 매칭 2(매칭된 노드의 자손 포함) · 조작 가능하거나 읽을 수 있는 role 1 · 그 외 0(조상으로만).
    창 · 대화상자 제목 매칭은 자손에게 물려주지 않는다 — goal에 흔한 앱 · 문서 이름이 창 전체를 끌어오지 않게.
    후보 중 점수 순 상위 budget개를 새로 고르고, 동점이면 문서 순서(창 내용이 메뉴 막대보다 앞).
    scores(선택, Jev 플러그인): 후보는 규칙 그대로 두고 순위만 '이 작업에 필요한가' 확률로 — 포커스는 그대로 맨 앞(D18).
    """
    seen = seen or set()
    g = plain(goal).lower()
    words = [w for w in re.findall(r"\w+", g) if len(w) >= 2]
    ranked: list[tuple[float, int, str]] = []  # (-점수, 문서 순서, id)
    # 포커스가 여럿이면(시스템 설정 사이드바 칸마다 AXFocused) 신호가 아니라 노이즈 — 포커스 점수를 주지 않는다
    focus_ok = sum(1 for n in view.root.walk() if State.FOCUSED in n.states) <= 3

    def score(node: UIElementNode, in_match: bool) -> None:
        hit = bool(g) and _matches(node, g, words)
        useful = node.role in ACTIONABLE and (bool(g) or bool(node.name or node.value))  # goal 없음: 이름 있는 것 먼저
        s = 3 if focus_ok and State.FOCUSED in node.states else 2 if in_match or hit else 1 if useful else 0
        if s and node.id not in keep and (s == 3 or node.id not in seen):
            # Jev 확률 0–1 → 0–2(포커스 3 아래). 점수를 못 받은 후보(400개 상한 밖)는 규칙 점수 그대로
            rank = s if scores is None or s == 3 or node.id not in scores else 2 * scores[node.id]
            ranked.append((-rank, len(ranked), node.id))
        for c in node.children:
            score(c, in_match or (hit and node.role not in (Role.WINDOW, Role.DIALOG)))

    for top in view.root.children:
        score(top, False)
    chosen = keep | {i for _, _, i in sorted(ranked)[:budget]}

    def build(node: UIElementNode) -> UIElementNode | None:
        kids: list[UIElementNode] = []
        omitted = 0
        for c in node.children:
            if (k := build(c)) is None:
                omitted += sum(1 for _ in c.walk())
            else:
                kids.append(k)
        if node.id not in chosen and not kids:
            return None
        return node.model_copy(update={"children": kids, "omitted": omitted})

    hidden = sum(1 for _ in view.root.walk()) - 1  # 아무것도 고르지 못했으면 루트 아래 전부 생략
    root = build(view.root) or view.root.model_copy(update={"children": [], "omitted": hidden})
    return view.model_copy(update={"root": root})
