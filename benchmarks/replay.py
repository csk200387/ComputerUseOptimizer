"""녹화한 fixture를 서버 전체 경로로 재생해 모드별 관찰 토큰을 잰다 — 결정적이라 회귀 확인에도 쓴다.

    uv run python benchmarks/replay.py [--scenario calculator textedit web] [--mode full compact proposed]

시나리오마다 observe 1회 + 녹화 순서대로 액션(재생 수집기는 액션 종류와 관계없이 다음 녹화본으로 넘어간다).
모드마다 run 하나로 Step 로그(기본 logs/replay.jsonl)에 쌓이므로
`uv run python benchmarks/report.py logs/replay.jsonl`로 results.md 행을 만든다.
goal은 proposed의 Filter 입력이라 UI에 보이는 단어로 쓴다(observe 도구 설명과 같은 규칙).
"""

import argparse
from pathlib import Path
from typing import get_args

import anyio
from mcp import Client

from cuop.collectors.replay import ReplayCollector
from cuop.mcp_server.server import build_server
from cuop.schema import Mode, StepLog

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures"
Step = tuple[str, dict[str, str]]  # (tool, 인자)


def clicks(*ids: str) -> list[Step]:
    return [("click", {"target": i}) for i in ids]


SCENARIOS: dict[str, tuple[str, list[Step]]] = {  # 이름 → (goal, 녹화 순서대로 한 액션 — 인자의 ID는 fixture ID)
    "calculator": ("7 더하기 3 등호, 그다음 보기 메뉴 열기", clicks("e13", "e24", "e23", "e28", "e158")),
    "textedit": ("볼드체 켜고 중앙 정렬", clicks("e24", "e36")),
    "web": (  # benchmarks/live_web.py와 같은 goal · 순서 (ID는 record_web.py 출력)
        "수량 2 선택, 선물 포장 체크, 받는 분 입력, 주문하기",
        [("select", {"target": "w94", "option": "2"}), *clicks("w107"),
         ("type", {"target": "w111", "text": "테스트"}), *clicks("w124")],
    ),
}  # fmt: skip


async def replay(scenario: str, mode: Mode, log: Path) -> None:
    goal, steps = SCENARIOS[scenario]
    server = build_server(ReplayCollector((FIXTURES / scenario).glob("*.json")), mode, log)
    async with Client(server, raise_exceptions=True) as c:
        await c.call_tool("observe", {"goal": goal})
        for tool, args in steps:
            await c.call_tool(tool, args)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", nargs="+", choices=list(SCENARIOS), default=list(SCENARIOS))
    p.add_argument("--mode", nargs="+", choices=get_args(Mode), default=["full", "compact", "proposed"])
    p.add_argument("--log", type=Path, default=Path("logs/replay.jsonl"))
    a = p.parse_args()
    for scenario in a.scenario:
        for mode in a.mode:
            start = len(a.log.read_text().splitlines()) if a.log.exists() else 0
            anyio.run(replay, scenario, mode, a.log)
            steps = [StepLog.model_validate_json(line) for line in a.log.read_text().splitlines()[start:]]
            tokens = [s.out_tokens for s in steps]
            print(f"{scenario:10} {mode:8} run {steps[0].run_id}  단계별 {tokens}  합 {sum(tokens):,}")


main()
