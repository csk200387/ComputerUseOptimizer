"""Context Compression — LLM이 읽는 텍스트를 만드는 유일한 지점 (②③④ 모드 공통 직렬화기)."""

import json
import unicodedata

from cuop.schema import NodeChange, UIDiffResult, UIElementNode, UISnapshot

MAX_TEXT = 80  # ponytail: 고정 절단 길이, 필드별 예산이 필요해지면 조정


def plain(s: str) -> str:
    """LRM(U+200E) 등 보이지 않는 서식 문자 제거 — 직렬화와 goal 매칭이 같은 글자를 보게 한다."""
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")


def _q(s: str) -> str:
    s = plain(s)
    return json.dumps(s if len(s) <= MAX_TEXT else s[: MAX_TEXT - 1] + "…", ensure_ascii=False)


def node_line(node: UIElementNode, depth: int = 0) -> str:
    """`e42 button "저장" ="값" [disabled,focused]` — 비어 있는 필드는 생략, 들여쓰기 1칸 = 깊이 1."""
    parts = [" " * depth + node.id, node.role.value]
    if node.name:
        parts.append(_q(node.name))
    if node.value:
        parts.append("=" + _q(node.value))
    if node.states:
        parts.append("[" + ",".join(sorted(node.states)) + "]")
    return " ".join(parts)


def render_tree(node: UIElementNode, depth: int = 0) -> list[str]:
    lines = [node_line(node, depth)]
    for child in node.children:
        lines += render_tree(child, depth + 1)
    if node.omitted:
        lines.append(" " * (depth + 1) + f"… +{node.omitted}")
    return lines


def _header(snap: UISnapshot) -> str:
    return f"[{snap.id}] {snap.app}" + (f" {_q(snap.window)}" if snap.window else "")


def render_snapshot(snap: UISnapshot) -> str:
    """헤더(스냅샷 id · 앱 · 창 제목) + 루트의 자식 트리. 루트(앱/문서) 자체는 헤더로 대신한다."""
    lines = [_header(snap), *(line for child in snap.root.children for line in render_tree(child))]
    return "\n".join(lines + ([f"… +{snap.root.omitted}"] if snap.root.omitted else []))


def change_line(c: NodeChange) -> str:
    """`~ e8 "10" [+selected,-focused]` — 바뀐 필드의 새 값만. role 등 나머지는 에이전트가 이미 안다."""
    parts = ["~", c.id]
    if c.name:
        parts.append(_q(c.name[1] or ""))
    if c.value:
        parts.append("=" + _q(c.value[1] or ""))
    if c.states_on or c.states_off:
        parts.append(
            "[" + ",".join([*(f"+{s}" for s in sorted(c.states_on)), *(f"-{s}" for s in sorted(c.states_off))]) + "]"
        )
    if c.omitted:
        parts.append(f"… +{c.omitted[1]}")  # 숨은 자식 수가 바뀜(0이면 모두 보임)
    return " ".join(parts)


def render_diff(view: UISnapshot, d: UIDiffResult) -> str:
    """에이전트가 받은 뷰(d.base_id) 이후의 변경: `~` 바뀜 · `+ <부모 id>` 아래 서브트리 추가 · `-` 서브트리째 삭제."""
    lines = [f"[{view.id} since {d.base_id}]" + (f" {_q(view.window)}" if view.window else "")]  # 앱은 Diff 중 불변
    lines += [change_line(c) for c in d.changed]
    if d.removed:  # 삭제를 먼저 — 다른 부모로 옮겨진 노드(삭제 + 추가)가 순서대로 적용되게
        lines.append("- " + " ".join(d.removed))
    parent: str | None = ""  # 같은 부모에 연달아 붙은 노드는 헤더 하나로 묶는다
    last: str | None = None  # 바로 앞에 추가한 노드 — 그 뒤에 이어 붙으면 헤더를 다시 쓰지 않는다
    for a in d.added:
        if a.parent_id != parent or (a.after is not None and a.after != last):
            where = "" if a.after is None else " first" if a.after == "" else f" after {a.after}"
            lines.append(f"+ {a.parent_id or 'top'}{where}")
            parent = a.parent_id
        last = a.node.id
        lines += render_tree(a.node, 1)
    lines += [f"^ {r.parent_id or 'top'}: " + " ".join(r.order) for r in d.reordered]  # 새 순서
    if d.is_empty:
        lines.append("(no change)")
    return "\n".join(lines)
