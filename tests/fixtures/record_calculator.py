"""계산기 fixture 녹화 — `uv run python tests/fixtures/record_calculator.py` (macOS, 손쉬운 사용 권한 필요)

계산기를 앞으로 가져와 7 + 3 = 을 누르고 보기 메뉴를 연다. 단계마다 수집기의 원본 UISnapshot을
calculator/에 JSON으로 저장한다. 한 수집기 세션에서 녹화하므로 element ID가 스냅샷 사이에서 유지된다(Diff의 전제).
"""

import asyncio
import subprocess
import time
from pathlib import Path

from cuop.collectors.macos import MacCollector
from cuop.schema import UISnapshot

APP = "com.apple.calculator"
OUT = Path(__file__).parent / "calculator"
STEPS = [  # (파일 이름, 누를 요소 이름 — 없으면 관찰만)
    ("start", None),
    ("click_7", "7"),
    ("click_plus", "더하기"),
    ("click_3", "3"),
    ("click_equals", "등호"),
    ("open_view_menu", "보기"),  # compact 규칙(닫힌 메뉴 접기)이 열린 메뉴와 구분할 근거
]


def find(snap: UISnapshot, name: str) -> str:
    """전위 순서로 첫 번째 — 창이 메뉴 막대보다 앞이라 '7'은 소수점 메뉴 항목이 아니라 버튼."""
    return next(n.id for n in snap.root.walk() if n.name == name)


async def record() -> list[UISnapshot]:
    c = MacCollector()
    snap = await c.snapshot(APP)
    await c.click(find(snap, "모두 지우기"))  # 이전 계산 지우기 (녹화 전 준비)
    snaps: list[UISnapshot] = []
    for _, target in STEPS:
        if target:
            await c.click(find(snap, target))
        await asyncio.sleep(0.3)  # 화면 갱신 대기
        snap = await c.snapshot(APP)
        snaps.append(snap)
    await c.click(find(snap, "기본"))  # 열린 메뉴 닫기: 이미 선택된 '기본'을 고르면 상태 변화 없이 메뉴만 닫힌다
    return snaps


if __name__ == "__main__":
    subprocess.run(["open", "-b", APP], check=True)
    time.sleep(1)  # 활성화 대기: 메뉴 막대는 맨 앞 앱 것만 화면에 있다
    snaps = asyncio.run(record())
    OUT.mkdir(exist_ok=True)
    for i, ((label, _), snap) in enumerate(zip(STEPS, snaps, strict=True)):
        text = snap.model_dump_json(indent=1, exclude_defaults=True)
        (OUT / f"{i}_{label}.json").write_text(text + "\n", encoding="utf-8")
        print(f"{i}_{label}.json: {sum(1 for _ in snap.root.walk())} nodes")
