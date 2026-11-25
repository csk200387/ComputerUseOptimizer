"""Partial Vision 실측 — 실제 계산기 2 + 5 =에서 '5'만 이미지 좌표로 누른다 (나머지는 element_id).

    uv run python benchmarks/live_vision.py [full compact proposed]

Terminal(서버를 띄우는 앱)에 화면 기록 권한이 있어야 한다. 이미지 관찰 · 좌표 클릭 때 계산기가 맨 앞으로 나온다.
받은 이미지는 logs/live_vision.png(좌표가 맞는지 눈으로 확인용), Step 로그는 logs/steps.jsonl.
"""

import base64
import re
import sys
from pathlib import Path
from typing import get_args

import anyio
from live_calculator import APP, display, reset
from mcp import Client
from mcp.types import ImageContent, TextContent

from cuop.collectors.macos import MacCollector
from cuop.mcp_server.server import build_server
from cuop.schema import Mode, Role

GOAL = "2 더하기 5 등호"


async def _offset(name: str) -> tuple[int, int, int]:
    """버튼 가운데의 창 기준 위치와 창 너비(point) — 에이전트가 이미지에서 찾을 위치의 정답."""
    snap = await MacCollector().snapshot(APP)
    win = next(c.bounds for c in snap.root.children if c.role is Role.WINDOW and c.bounds)
    b = next(n.bounds for n in snap.root.walk() if n.role is Role.BUTTON and n.name == name and n.bounds)
    return b.x + b.w // 2 - win.x, b.y + b.h // 2 - win.y, win.w


async def run(mode: Mode) -> None:
    await reset()
    async with Client(build_server(MacCollector(), mode, Path("logs/steps.jsonl")), raise_exceptions=True) as c:
        first = (await c.call_tool("observe", {"goal": GOAL, "app": APP})).content[0]
        assert isinstance(first, TextContent)
        ids = {n: m.group(1) for n in ["2", "더하기", "등호"] if (m := re.search(rf'(e\d+) button "{n}"', first.text))}
        for name in ["2", "더하기"]:
            await c.call_tool("click", {"target": ids[name]})
        seen = await c.call_tool("observe", {"goal": GOAL, "app": APP, "vision": "window"})
        assert not seen.is_error, seen.content[0]  # 권한이 없으면 여기서 PermissionError 안내
        text, image = seen.content
        assert isinstance(text, TextContent) and isinstance(image, ImageContent), seen
        await anyio.Path("logs/live_vision.png").write_bytes(base64.b64decode(image.data))
        size = re.search(r"image \S+ (\d+)x\d+", text.text)
        assert size, text.text
        dx, dy, width = await _offset("5")
        scale = int(size.group(1)) / width
        x, y = round(dx * scale), round(dy * scale)
        clicked = await c.call_tool("click", {"x": x, "y": y})
        assert not clicked.is_error, clicked
        await c.call_tool("click", {"target": ids["등호"]})
    print(f"{mode:8} {text.text.splitlines()[-1]} · 좌표 ({x}, {y}) · 결과 {await display()}")


wanted = sys.argv[1:] or ["full", "compact", "proposed"]
for m in get_args(Mode):
    if m in wanted:
        anyio.run(run, m)
