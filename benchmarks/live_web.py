"""로컬 주문 페이지(benchmarks/pages/shop.html)에서 주문을 서버 경로 그대로 실행한다 — 에이전트 대신 고정 순서로.

    uv run python benchmarks/live_web.py [full compact proposed]

브라우저(기본 Edge)의 원격 디버깅이 켜져 있어야 하고, 처음 연결할 때 브라우저의 허용 창을 사용자가 눌러야 한다
(연결은 모든 모드가 같이 쓴다). run마다 observe(url)로 새 탭을 열고 끝나면 그 탭만 닫는다.
observe 1 + select 1 + click 2 + type 1. Step 로그는 logs/steps.jsonl → `uv run python benchmarks/report.py`.
"""

import re
import sys
from pathlib import Path
from typing import get_args

import anyio
from mcp import Client
from mcp.types import TextContent

from cuop.collectors.macos import MacCollector
from cuop.collectors.web import WebCollector
from cuop.mcp_server.server import build_server
from cuop.schema import Mode

PAGE = Path(__file__).parent / "pages" / "shop.html"
GOAL = "수량 2 선택, 선물 포장 체크, 받는 분 입력, 주문하기"
EXPECT = "주문 완료: 2개, 포장 함, 받는 분 테스트"


async def run(web: WebCollector, mode: Mode) -> None:
    try:
        async with Client(build_server(web, mode, Path("logs/steps.jsonl"), web), raise_exceptions=True) as c:
            first = (await c.call_tool("observe", {"goal": GOAL, "url": PAGE.as_uri()})).content[0]
            assert isinstance(first, TextContent)

            def find(role: str, name: str) -> str:  # ID는 첫 관찰에서 — proposed의 액션 결과는 Diff다
                match = re.search(rf'(w\d+) {role} "{name}', first.text)
                assert match, name
                return match.group(1)

            qty, wrap = find("combobox", "수량"), find("checkbox", "선물 포장")
            to, order = find("textbox", "받는 분"), find("button", "주문하기")
            await c.call_tool("select", {"target": qty, "option": "2"})
            await c.call_tool("click", {"target": wrap})
            await c.call_tool("type", {"target": to, "text": "테스트"})
            await c.call_tool("click", {"target": order})
        status = next(
            (n.name for n in (await web.snapshot()).root.walk() if (n.name or "").startswith(("주문 완료", "주문 전"))),
            None,
        )
        print(f"{mode:8} {'성공' if status == EXPECT else '실패'}: {status}")
    finally:
        await web.reset()  # 테스트 탭만 닫고 ID를 비운다 → 모드마다 같은 ID로 시작


async def main(modes: list[str]) -> None:
    web = WebCollector(front_title=MacCollector().window_title)  # 서버 main()과 같은 구성
    try:
        for m in get_args(Mode):
            if m in modes:
                await run(web, m)
    finally:
        await web.close()


anyio.run(main, sys.argv[1:] or ["full", "compact", "proposed"])
