"""읽은 트리 → 표시용 트리(-i · --compact · --skeleton) · agent-desktop JSON 모양 (OS 무관)."""

from collections import Counter
from typing import Any

from cuop.schema import Role, State, UIElementNode

# CUOP Role 값 → agent-desktop role 토큰 (나머지는 값이 같다)
AD_ROLE = {
    Role.TEXT: "statictext",
    Role.TEXTBOX: "textfield",
    Role.RADIO: "radiobutton",
    Role.TREE: "outline",
    Role.GENERIC: "group",
    Role.MENUBAR: "menu",
    Role.LISTITEM: "listitem",
    Role.CANVAS: "image",
}
AD_STATES = frozenset(State) - {State.COLLAPSED, State.EDITABLE, State.FOCUSABLE, State.CLICKABLE}


def relabel(node: UIElementNode, ids: list[str]) -> UIElementNode:
    """수집기의 임시 id("#i") → 안정 번호(eN)."""
    return node.model_copy(update={"id": ids[int(node.id[1:])], "children": [relabel(c, ids) for c in node.children]})


def identity_key(n: UIElementNode) -> tuple[str, str | None, str | None]:
    """capture proof에 쓰는 안정 필드: role + native id + 이름. 값 · bounds는 정체성이 아니다."""
    stable_name = None if n.role in (Role.TEXTBOX, Role.COMBOBOX, Role.SLIDER) else n.name  # 값이 이름에 섞이는 role
    return (n.raw_role, n.native_id, stable_name)


def twins(tree: UIElementNode) -> set[tuple[str, str | None, str | None]]:
    c = Counter(identity_key(n) for n in tree.walk())
    return {k for k, v in c.items() if v > 1}


def interactive_only(node: UIElementNode) -> UIElementNode | None:
    """ref가 있는 노드와 그 조상만 남긴다. 잘린 컨테이너(children_count)는 drill 대상이라 남긴다."""
    kids = [k for k in (interactive_only(c) for c in node.children) if k is not None]
    if node.ref or kids or node.children_count or node.truncated:
        return node.model_copy(update={"children": kids})
    return None


def compact(node: UIElementNode, top: bool = True) -> UIElementNode | None:
    """빈 wrapper(이름 · 값 · ref · 상태 없음) 제거: 자식 하나면 끌어올리고, 자식이 없으면 뺀다."""
    kids = [k for k in (compact(c, False) for c in node.children) if k is not None]
    bare = not (node.name or node.value or node.ref or node.states or node.children_count or node.truncated)
    if not top and bare and node.role in (Role.GROUP, Role.GENERIC, Role.SCROLLAREA, Role.SPLITTER, Role.LAYOUTITEM):
        if not kids:
            return None
        if len(kids) == 1:
            return kids[0]
    if not top and bare and not kids and node.role in (Role.IMAGE, Role.SEPARATOR, Role.TEXT):
        return None
    return node.model_copy(update={"children": kids})


def refs_of(node: UIElementNode) -> list[UIElementNode]:
    return [n for n in node.walk() if n.ref]


def to_json(node: UIElementNode, sid: str, bounds: bool) -> dict[str, Any]:
    """agent-desktop 노드 모양 — 빈 필드는 생략, 키는 정렬(결정적)."""
    out: dict[str, Any] = {"role": AD_ROLE.get(node.role, node.role.value)}
    if node.name:
        out["name"] = node.name
    if node.value:
        out["value"] = node.value
    if node.description:
        out["description"] = node.description
    if node.ref:
        out["ref_id"] = f"@{sid}:{node.id}"
    if states := sorted(s.value for s in node.states if s in AD_STATES):
        out["states"] = states
    if node.actions:
        out["available_actions"] = list(node.actions)
    if node.native_id:
        out["native_id"] = {"kind": "ax_identifier", "value": node.native_id}
    if bounds and node.bounds:
        b = node.bounds
        out["bounds"] = {"x": b.x, "y": b.y, "width": b.w, "height": b.h}
    if node.children_count is not None:
        out["children_count"] = node.children_count
    if node.truncated:
        out["subtree_truncated"] = True
    if node.children:
        out["children"] = [to_json(c, sid, bounds) for c in node.children]
    return dict(sorted(out.items()))
