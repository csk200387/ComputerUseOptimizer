"""Tree Diff Engine — 에이전트가 이미 받은 뷰(sent view)와 새 뷰를 안정 ID로 비교한다."""

from cuop.schema import AddedNode, NodeChange, Reorder, UIDiffResult, UIElementNode, UISnapshot

Index = dict[str, tuple[str | None, UIElementNode]]  # id → (부모 id, 노드), 전위 순서. 최상위의 부모는 None


def _index(nodes: list[UIElementNode], parent: str | None, out: Index) -> Index:
    for n in nodes:
        out[n.id] = (parent, n)
        _index(n.children, n.id, out)
    return out


def _change(a: UIElementNode, b: UIElementNode) -> NodeChange | None:
    c = NodeChange(
        id=b.id,
        name=(a.name, b.name) if a.name != b.name else None,
        value=(a.value, b.value) if a.value != b.value else None,
        states_on=b.states - a.states,
        states_off=a.states - b.states,
        omitted=(a.omitted, b.omitted) if a.omitted != b.omitted else None,
    )
    return c if c.name or c.value or c.states_on or c.states_off or c.omitted else None


def diff(base: UISnapshot, view: UISnapshot) -> UIDiffResult:
    """bounds는 비교하지 않는다(스크롤 노이즈). 같은 노드 = id와 부모가 모두 같은 노드.

    부모가 바뀐 노드(가지치기로 끌어올려진 group이 생기거나 사라진 경우 등)는 삭제 + 추가로 보낸다:
    에이전트는 `-`를 서브트리째 지우므로, 살아남은 자손은 새 부모 아래 다시 받아야 트리가 어긋나지 않는다.
    """
    old, new = _index(base.root.children, None, {}), _index(view.root.children, None, {})
    same = {i for i, (p, n) in new.items() if i in old and old[i][0] == p and old[i][1].role == n.role}
    added: list[AddedNode] = []
    covered: set[str] = set()  # 추가한 서브트리 안의 노드 — 서브트리째 보내므로 changed에서 뺀다
    for i, (p, n) in new.items():  # 전위 순서라 부모가 먼저 온다
        if p is not None and p in covered:
            covered.add(i)
        elif i not in same:
            added.append(AddedNode(parent_id=p, node=n, after=_anchor(view.root, p, i, same)))
            covered.add(i)
    gone = old.keys() - same
    removed = [i for i, (p, _) in old.items() if i in gone and p not in gone]  # 서브트리 루트만
    changes = [_change(old[i][1], n) for i, (_, n) in new.items() if i in same and i not in covered]
    if base.root.omitted != view.root.omitted:  # 최상위 `… +N`
        changes.append(NodeChange(id="top", omitted=(base.root.omitted, view.root.omitted)))
    return UIDiffResult(
        base_id=base.id,
        snapshot_id=view.id,
        added=added,
        removed=removed,
        changed=[c for c in changes if c],
        reordered=_reorders(base.root, view.root, same),
    )


def _anchor(root: UIElementNode, pid: str | None, nid: str, same: set[str]) -> str | None:
    """기존 형제(same) 사이에 끼었으면 바로 앞 형제 id(맨 앞이면 ""), 기존 형제들 뒤에 붙었으면 None."""
    parent = root if pid is None else next((n for n in root.walk() if n.id == pid), None)
    if parent is None:
        return None
    ids = [c.id for c in parent.children]
    k = ids.index(nid)
    if not any(x in same for x in ids[k + 1 :]):
        return None
    return ids[k - 1] if k else ""


def _reorders(a: UIElementNode, b: UIElementNode, same: set[str]) -> list[Reorder]:
    """양쪽에 다 있는 자식(같은 노드)의 상대 순서가 바뀐 부모마다 새 순서 — 추가 · 삭제만으로는 정렬이 안 보인다."""
    olds: dict[str | None, list[str]] = {}
    news: dict[str | None, list[str]] = {}
    full: dict[str | None, list[str]] = {}  # 새 자식 전부(추가된 것 포함) — `^`가 마지막에 적용돼도 새 노드가 남게

    def collect(node: UIElementNode, pid: str | None, out: dict[str | None, list[str]], every: bool = False) -> None:
        out[pid] = [c.id for c in node.children if c.id in same]
        if every:
            full[pid] = [c.id for c in node.children]
        for c in node.children:
            collect(c, c.id, out, every)

    collect(a, None, olds)
    collect(b, None, news, every=True)
    out: list[Reorder] = []
    for pid, kept in news.items():
        if pid not in olds or pid in (None,) and not kept:
            continue
        shuffled = len(kept) > 1 and set(olds[pid]) >= set(kept) and [i for i in olds[pid] if i in kept] != kept
        if shuffled:  # 기존 자식끼리 순서가 바뀜(정렬) — 삽입 위치는 AddedNode.after가 맡는다
            out.append(Reorder(parent_id=pid, order=full[pid]))
    return out
