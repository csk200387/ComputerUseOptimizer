"""web Partial Vision 실측 — 색 칸을 캔버스에만 그린 페이지(pages/canvas.html)에서 이미지 좌표로 칸을 누른다.

    uv run python benchmarks/live_web_vision.py [full compact proposed]

하네스 전용 Chromium(새 프로필 · 권한 창 없음)이 잠깐 뜬다. 모드마다:
1) observe(url) — 트리에 이름이 거의 없어 자동 이미지(보이는 영역 전체) → 그 이미지 좌표로 '파랑'
2) observe(vision=캔버스 id) — 캔버스만 찍은 이미지 → 그 이미지 좌표로 '빨강'
받은 이미지는 logs/live_web_vision_{1,2}.png(좌표가 맞는지 눈으로 확인용), Step 로그는 logs/steps.jsonl.
"""

import base64
import re
import sys
from pathlib import Path
from typing import get_args

import anyio
from mcp import Client
from mcp.types import CallToolResult, ImageContent, TextContent

from cuop.collectors.web import WebCollector
from cuop.mcp_server.server import build_server
from cuop.schema import Bounds, Mode, Role

PAGE = Path(__file__).parent / "pages" / "canvas.html"
COLORS = ["빨강", "주황", "노랑", "초록", "파랑", "보라"]  # canvas.html과 같은 순서 · 3열 × 2행
TILE = 100  # 칸 한 변(CSS px)
GOAL = "파랑 칸 누르기"


def center(color: str) -> tuple[int, int]:
    i = COLORS.index(color)
    return i % 3 * TILE + TILE // 2, i // 3 * TILE + TILE // 2


async def pixel(result: CallToolResult, n: int, area: Bounds, x: int, y: int) -> tuple[int, int]:
    """이미지를 저장하고, 영역 기준 좌표(x, y)를 이미지 픽셀로 — 에이전트가 이미지에서 찾을 위치의 정답."""
    assert not result.is_error, result.content[0]
    text, image = result.content
    assert isinstance(text, TextContent) and isinstance(image, ImageContent), result
    await anyio.Path(f"logs/live_web_vision_{n}.png").write_bytes(base64.b64decode(image.data))
    size = re.search(r"image \S+ (\d+)x\d+", text.text)
    assert size, text.text
    scale = int(size.group(1)) / area.w
    return round(x * scale), round(y * scale)


def status(result: CallToolResult) -> str:
    text = next(c.text for c in result.content if isinstance(c, TextContent))
    return m.group(0) if (m := re.search(r"선택: [^\s\"]+", text)) else f"(없음) {text.splitlines()[0]}"


async def run(web: WebCollector, mode: Mode) -> None:
    try:
        async with Client(build_server(web, mode, Path("logs/steps.jsonl"), web), raise_exceptions=True) as c:
            first = await c.call_tool("observe", {"goal": GOAL, "url": PAGE.as_uri()})
            snap = await web.snapshot()  # 정답 위치용 — 캔버스 영역(뷰포트 CSS px)
            view = snap.root.bounds
            canvas = next(n for n in snap.root.walk() if n.role is Role.CANVAS)
            assert view and canvas.bounds, snap.root
            cx, cy = center("파랑")
            x, y = await pixel(first, 1, view, canvas.bounds.x + cx, canvas.bounds.y + cy)
            blue = await c.call_tool("click", {"x": x, "y": y})
            seen = await c.call_tool("observe", {"goal": "빨강 칸 누르기", "vision": canvas.id})
            x2, y2 = await pixel(seen, 2, canvas.bounds, *center("빨강"))
            red = await c.call_tool("click", {"x": x2, "y": y2})
        print(f"{mode:8} 문서 이미지 ({x}, {y}) → {status(blue)} · 캔버스 이미지 ({x2}, {y2}) → {status(red)}")
    finally:
        await web.reset()


async def main(modes: list[str]) -> None:
    web = WebCollector("launch")  # 하네스 전용 브라우저 — 사용자 Edge에 붙지 않는다
    try:
        for m in get_args(Mode):
            if m in modes and m != "screenshot":  # ①은 웹도 OS 전체 화면이라 여기서 다루지 않는다
                await run(web, m)
    finally:
        await web.close()


anyio.run(main, sys.argv[1:] or ["full", "compact", "proposed"])
