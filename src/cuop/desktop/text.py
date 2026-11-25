"""CUOP compact 텍스트 형식 (`--format text`) — agent-desktop JSON보다 적은 토큰으로 같은 ref를 전달한다.

한 줄 = `e42 button "저장" ="값" [disabled]`. eN은 헤더 `[@<snapshot>]`과 합쳐 `@<snapshot>:eN`으로 쓴다.
보여 준 모든 노드는 refmap에 있어 `--root` · get · is에 쓸 수 있다(조작은 상호작용 노드만).
- `--goal`: Task-aware Filter(포커스 · goal 매칭 · 조작 가능 role 점수, 예산 30 + 조상, 빠진 자손은 `… +N`)
- `--diff`: 이 창에서 마지막으로 보낸 뷰 이후의 변경만(`~` 바뀜 · `+` 추가 · `-` 삭제 · `^` 순서). 처음이면 전체.
- 토큰 예산(`BUDGET`): goal 없이 전체 뷰가 예산을 넘으면 skeleton(깊이 3, 잘린 컨테이너 `(+N)`)으로 줄인다 →
  에이전트는 `--root @…:eN`으로 그 부분만 내려가 읽는다(agent-desktop skeleton과 같은 흐름).
"""

from functools import cache
from typing import Any

from cuop.desktop.store import Namespace, Snapshot
from cuop.optimizer import jev
from cuop.optimizer.compress import render_diff, render_tree
from cuop.optimizer.diff import diff
from cuop.optimizer.filter import ACTIONABLE, prune, task_filter
from cuop.schema import UIElementNode, UISnapshot

BUDGET = (
    900  # ponytail: 고정 토큰 예산 — 1470×923 창 스크린샷 ≈ 1,533토큰의 약 60%. 창 크기에 비례시키는 것이 다음 단계
)
SKELETON_DEPTH = 3
JEV_MAX = 400  # Jev에 보내는 후보 수 상한(문서 순서 앞부분)
_jev_cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, float]] = {}


def _line(n: UIElementNode) -> str:
    name = (n.name or "")[:60]
    value = f" ={(n.value or '')[:40]}" if n.value else ""
    return f'{n.role.value} "{name}"{value}'


def jev_scores(view: UISnapshot, goal: str) -> dict[str, float] | None:
    """Jev 플러그인(CUOP_JEV=1 · 키): 이름 · 값이 있거나 조작 가능한 요소마다 '이 goal에 필요한가' 확률. 같은 뷰 ·
    같은 goal은 캐시. 꺼져 있거나 실패하면 None — 규칙 순위 그대로."""
    if not jev.enabled():
        return None
    nodes = [n for n in view.root.walk() if n is not view.root and (n.name or n.value or n.role in ACTIONABLE)]
    lines = tuple((n.id, _line(n)) for n in nodes[:JEV_MAX])
    key = (goal, lines)
    if key in _jev_cache:
        return _jev_cache[key]
    got = jev.relevance(goal, dict(lines))
    if got is not None:  # 실패는 캐시하지 않는다 — Jev가 돌아오면 같은 뷰도 다시 묻는다
        if len(_jev_cache) > 64:
            _jev_cache.clear()
        _jev_cache[key] = got
    return got


@cache
def _enc() -> Any:
    import tiktoken

    return tiktoken.get_encoding("o200k_base")


def tokens(text: str) -> int:
    return len(_enc().encode(text, disallowed_special=()))


def header(snap: Snapshot, extra: str = "") -> str:
    win = f' "{snap.window_title}"' if snap.window_title else ""
    wid = f" w-{snap.window_id}" if snap.window_id else ""
    part = "" if snap.complete else " (partial: raise --timeout-ms or drill with --root)"
    return f"[@{snap.snapshot_id}] {snap.app}{win}{wid}{part}{extra}"


def skeleton(node: UIElementNode, depth: int, width: int = 20) -> UIElementNode:
    """depth 아래를 잘라 남은 자식 수를 달고(children_count), 자식이 width개를 넘으면 앞 width개만(`… +N`)."""
    if depth <= 0 and node.children:
        return node.model_copy(update={"children": [], "children_count": len(node.children)})
    kids = [skeleton(c, depth - 1, width) for c in node.children[:width]]
    rest = sum(sum(1 for _ in c.walk()) for c in node.children[width:])
    return node.model_copy(update={"children": kids, "omitted": node.omitted + rest})


def _render(view: UISnapshot) -> list[str]:
    body: list[str] = []
    for c in view.root.children:
        body += render_tree(c)
    if view.root.omitted:
        body.append(f"… +{view.root.omitted}")
    return body


def _mark_counts(node: UIElementNode) -> UIElementNode:
    """children_count를 이름 뒤 `(+N)` 표시로 — render_tree는 이 필드를 모른다."""
    kids = [_mark_counts(c) for c in node.children]
    omitted = node.omitted + (node.children_count or 0) if not kids else node.omitted
    return node.model_copy(update={"children": kids, "omitted": omitted})


def render(ns: Namespace, snap: Snapshot, tree: UIElementNode, goal: str | None, want_diff: bool = False,
           inherit: bool = False) -> str:  # fmt: skip
    original = prune(UISnapshot(id=f"@{snap.snapshot_id}", platform="macos", app=snap.app, window=snap.window_title,
                                root=_mark_counts(tree)))  # fmt: skip
    key = f"{snap.pid}-{snap.window_id or 0}-{snap.surface}-{tree.id}"  # drill · surface마다 따로
    prev, revision = ns.view(key)
    if inherit and prev is not None and prev.get("goal") is not None:
        # 자동 관찰은 이 창의 지금 필터 그대로(시작 때 준 goal보다 우선) — 에이전트가 고른 goal 뷰와 Diff가 맞게.
        # 저장된 ""는 예산 초과 자동 필터 → None으로 두고 다시 판단한다
        goal = str(prev["goal"]) or None
    text, data = _render_view(snap, original, goal, want_diff, prev)
    if ns.save_view(key, data, expected_revision=revision):
        return text
    # 다른 호출이 기준 뷰를 바꿨다. 이전 뷰의 내용에 의존하지 않는 전체 뷰를 다시 보낸다.
    text, data = _render_view(snap, original, goal, False, None)
    while True:
        _, revision = ns.view(key)
        if ns.save_view(key, data, expected_revision=revision):
            return text


def _render_view(
    snap: Snapshot, view: UISnapshot, goal: str | None, want_diff: bool, prev: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    base = None
    if prev is not None and prev.get("view", {}).get("root", {}).get("id") == view.root.id:
        base = UISnapshot.model_validate(prev["view"], strict=False)  # 저장한 JSON(list → frozenset 등)
    seen: set[str] = set()
    hint = ""
    original = view
    auto = goal is None and tokens("\n".join(_render(view))) > BUDGET
    if auto:  # 예산을 넘는 창(goal 없음): 깊이로 자르면 큰 Electron 창은 컨테이너만 남는다 → 이름 있는 조작 요소 먼저
        goal = ""
        drill = f"drill with --root @{snap.snapshot_id}:eN or pass --goal"
        hint = f" (filtered: over {BUDGET} tokens, named controls first — {drill})"
    if base is not None and want_diff and (prev or {}).get("goal") != goal:
        # 필터가 바뀌었다(goal 변경 · 예산을 넘어 자동 필터로 · 그 반대) — 걸러진 노드를 '삭제'로 보내지 않게 전체 뷰
        base = None
    if goal is not None:
        same_goal = prev is not None and prev.get("goal") == goal
        known = {n.id for n in base.root.walk()} if base is not None and want_diff and same_goal else set[str]()
        old_seen = set(prev.get("seen", [])) if prev and same_goal else set[str]()
        seen = {n.id for n in view.root.walk()}
        # Jev: 에이전트가 goal을 줬고 전체 뷰가 예산을 넘을 때만(작은 창은 규칙 그대로)
        scores = (
            jev_scores(original, goal) if goal and not auto and tokens("\n".join(_render(original))) > BUDGET else None
        )
        for cap in (30, 20, 12, 6) if auto else (30,):  # 자동 필터는 예산 안에 들 때까지 고르는 수를 줄인다
            view = task_filter(original, goal, known, old_seen, budget=cap, scores=scores)
            if tokens("\n".join(_render(view))) <= BUDGET:
                break
    text = render_diff(view, diff(base, view)) if want_diff and base is not None else ""
    if text and not goal and tokens(text) > BUDGET:
        text = ""  # 변경이 전체보다 크다 — 예산 안의 전체 뷰로 다시 보낸다
    if not text:
        text = "\n".join([header(snap, hint), *_render(view)])
        if not goal and tokens(text) > BUDGET:  # 필터로도 넘으면(긴 이름 등) 원래 뷰를 깊이로 자른다
            hint = f" (skeleton: over {BUDGET} tokens — drill with --root @{snap.snapshot_id}:eN or pass --goal)"
            for depth, width in ((SKELETON_DEPTH, 20), (SKELETON_DEPTH, 8), (2, 8), (1, 8), (1, 3)):  # 예산 안까지
                view = original.model_copy(update={"root": _mark_counts(skeleton(original.root, depth + 1, width))})
                text = "\n".join([header(snap, hint), *_render(view)])
                if tokens(text) <= BUDGET:
                    break
    return text, {"view": view.model_dump(mode="json"), "goal": goal, "seen": sorted(seen)}
