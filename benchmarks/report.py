"""Step 로그를 run별로 한 줄씩 요약 — `uv run python benchmarks/report.py [logs/steps.jsonl]`

출력은 benchmarks/results.md 표의 행 형식이다. 날짜 · 시나리오 · 비고 칸은 기록할 때 채운다.
출력 토큰 = 텍스트(tiktoken) + 이미지(가로×세로/750) 추정치. Vision = 이미지를 보낸 호출 수.
"""

import sys
from collections import defaultdict
from pathlib import Path

from cuop.schema import StepLog

path = Path(sys.argv[1] if len(sys.argv) > 1 else "logs/steps.jsonl")
runs: dict[str, list[StepLog]] = defaultdict(list)
for line in path.read_text(encoding="utf-8").splitlines():
    step = StepLog.model_validate_json(line)
    runs[step.run_id].append(step)

header = ["날짜", "시나리오", "run_id", "모드", "호출", "출력 토큰 합", "호출당", "문자 합"]
header += ["전송 노드 평균", "지연 합(ms)", "Vision", "에러", "비고"]
print("| " + " | ".join(header) + " |")
print("|" + "---|" * len(header))
for run_id, steps in runs.items():
    n = len(steps)
    tokens = sum(s.out_tokens + s.image_tokens for s in steps)
    cells = [
        run_id,
        steps[0].mode,
        n,
        f"{tokens:,}",
        f"{tokens // n:,}",
        f"{sum(s.out_chars for s in steps):,}",
        sum(s.sent_nodes for s in steps) // n,
        f"{sum(s.latency_ms for s in steps):,.0f}",
        sum(s.image_tokens > 0 for s in steps),
        sum(s.error is not None for s in steps),
    ]
    print("|  |  | " + " | ".join(map(str, cells)) + " |  |")
