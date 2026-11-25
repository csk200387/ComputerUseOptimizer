"""Fallback 판정 · Partial Vision 좌표계 (OS 무관).

- 판정: 창에 이름 있는 요소가 거의 없으면 자동으로 이미지 (`empty_tree`)
- 좌표계: 1px = 1pt로 찍고, 크면 더 줄인다. 서버가 VisionFrame을 보관해 이미지 픽셀 → 플랫폼 좌표로 바꾼다
"""

import math

from cuop.schema import Bounds, Role, UIElementNode, UISnapshot, VisionFrame

MIN_NAMED = 5  # ponytail: 고정 임계값 — 창 제어 버튼(3–4개)만 있는 창을 잡는다. Jev 판단(judge)으로 바꿀 자리
MAX_EDGE, MAX_PIXELS = 1568, 1_150_000  # ponytail: Claude 비전의 자동 축소 기준 — 모델이 바뀌면 다시 확인
HINT = 'observe(vision="window") shows the screen'


def window(snap: UISnapshot) -> UIElementNode | None:
    """맨 앞 창 — 영역(bounds)이 있는 첫 window · dialog. web은 문서 자신(보이는 영역)."""
    if snap.root.role is Role.DOCUMENT:
        return snap.root if snap.root.bounds else None
    return next((c for c in snap.root.children if c.role in (Role.WINDOW, Role.DIALOG) and c.bounds), None)


def sparse(win: UIElementNode) -> bool:
    """트리로는 알 수 있는 것이 거의 없는 창 — 캔버스 · 게임 · 접근성을 노출하지 않는 앱."""
    return sum(1 for n in win.walk() if n is not win and (n.name or n.value)) < MIN_NAMED


def region(snap: UISnapshot, target: str) -> Bounds:
    """observe의 vision 인자("window" 또는 요소 id)가 가리키는 화면 영역."""
    node = window(snap) if target == "window" else next((n for n in snap.root.walk() if n.id == target), None)
    b = node.bounds if node else None
    if b is None or not (b.w > 0 and b.h > 0):
        raise LookupError(f"no visible area for {target!r}")
    return b


def frame(area: Bounds) -> VisionFrame:
    """1px = 1pt(Retina 원본의 1/4 토큰). 모델 쪽 자동 축소 기준을 넘으면 비율대로 더 줄인다."""
    s = min(1.0, MAX_EDGE / max(area.w, area.h), math.sqrt(MAX_PIXELS / (area.w * area.h)))
    return VisionFrame(region=area, width=max(1, round(area.w * s)), height=max(1, round(area.h * s)))


def to_screen(f: VisionFrame, x: int, y: int) -> tuple[int, int]:
    if not (0 <= x < f.width and 0 <= y < f.height):
        raise ValueError(f"x, y must be inside the {f.width}x{f.height} image")
    r = f.region
    return r.x + round(x * r.w / f.width), r.y + round(y * r.h / f.height)


def image_tokens(f: VisionFrame) -> int:
    return math.ceil(f.width * f.height / 750)  # Claude 비전 문서의 근사식
