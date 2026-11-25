"""실제 계산기에서 2 + 5 =를 서버 경로 그대로 실행한다 — 에이전트 대신 observe 1 + click 4를 고정 순서로.

    uv run python benchmarks/live_calculator.py [full compact proposed]

run마다 AC로 디스플레이를 0으로 되돌린다(Step 로그에는 남지 않음). 버튼 AXPress라 계산기가 앞으로 나오지 않는다.
Step 로그는 logs/steps.jsonl → `uv run python benchmarks/report.py`로 results.md 행을 만든다.
"""

import re
import sys
from pathlib import Path
from typing import get_args

import anyio
from mcp import Client
from mcp.types import TextContent

from cuop.collectors.macos import MacCollector
from cuop.mcp_server.server import build_server
from cuop.schema import Mode, Role, UISnapshot

APP = "계산기"


def _button(snap: UISnapshot, *names: str) -> str | None:
    return next((n.id for n in snap.root.walk() if n.role is Role.BUTTON and n.name in names), None)


async def display() -> str:
    snap = await MacCollector().snapshot(APP)
    group = next(n for n in snap.root.walk() if n.name == "필드 편집")
    return (group.children[0].name or "").replace("‎", "")  # LRM


async def reset() -> None:
    col = MacCollector()
    for _ in range(2):  # C → AC
        if ac := _button(await col.snapshot(APP), "지우기", "모두 지우기"):
            await col.click(ac)


async def run(mode: Mode) -> None:
    await reset()
    async with Client(build_server(MacCollector(), mode, Path("logs/steps.jsonl")), raise_exceptions=True) as c:
        first = (await c.call_tool("observe", {"goal": "2 더하기 5 등호", "app": APP})).content[0]
        assert isinstance(first, TextContent)
        for name in ["2", "더하기", "5", "등호"]:  # ID는 첫 관찰에서 찾는다 — proposed의 액션 결과는 Diff다
            match = re.search(rf'(e\d+) button "{name}"', first.text)
            assert match, name
            await c.call_tool("click", {"target": match.group(1)})
    print(f"{mode:8} 결과 {await display()}")


if __name__ == "__main__":  # live_vision.py가 도우미를 가져다 쓴다
    wanted = sys.argv[1:] or ["full", "compact", "proposed"]
    for m in get_args(Mode):
        if m in wanted:
            anyio.run(run, m)
