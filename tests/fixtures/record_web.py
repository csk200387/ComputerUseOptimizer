"""web fixture 녹화 — `uv run python tests/fixtures/record_web.py` (브라우저 원격 디버깅 켜기 + 허용 창 1회)

benchmarks/pages/shop.html(측정용 가상 페이지)을 새 탭으로 열고 주문 4단계(수량 2 · 선물 포장 · 받는 분 · 주문하기)를
수행하며 단계마다 수집기의 원본 UISnapshot을 web/에 저장한다. 끝나면 그 탭만 닫는다.
개인 정보: 페이지 주소에는 사용자 경로가 들어 있어 저장소 기준 상대 경로로 바꿔 저장한다. 다른 탭은 읽지 않는다.
"""

import asyncio
from pathlib import Path

from cuop.collectors.macos import MacCollector
from cuop.collectors.web import WebCollector
from cuop.schema import Role, UISnapshot

ROOT = Path(__file__).parents[2]
PAGE = ROOT / "benchmarks" / "pages" / "shop.html"
OUT = Path(__file__).parent / "web"
LABELS = ["open", "select_qty", "click_wrap", "type_to", "click_order"]


async def record() -> list[UISnapshot]:
    web = WebCollector(front_title=MacCollector().window_title)
    try:
        await web.open(PAGE.as_uri())
        snaps = [await web.snapshot()]

        def find(role: Role, name: str) -> str:
            return next(n.id for n in snaps[0].root.walk() if n.role is role and (n.name or "").startswith(name))

        qty, wrap = find(Role.COMBOBOX, "수량"), find(Role.CHECKBOX, "선물 포장")
        to, order = find(Role.TEXTBOX, "받는 분"), find(Role.BUTTON, "주문하기")
        print("ids", qty, wrap, to, order)  # benchmarks/replay.py 시나리오에 쓴다
        await web.select(qty, "2")
        snaps.append(await web.snapshot())
        await web.click(wrap)
        snaps.append(await web.snapshot())
        await web.type_text(to, "테스트")
        snaps.append(await web.snapshot())
        await web.click(order)
        snaps.append(await web.snapshot())
        return snaps
    finally:
        await web.close()


if __name__ == "__main__":
    snaps = asyncio.run(record())
    OUT.mkdir(exist_ok=True)
    for i, (label, snap) in enumerate(zip(LABELS, snaps, strict=True)):
        snap = snap.model_copy(update={"app": PAGE.relative_to(ROOT).as_posix()})
        (OUT / f"{i}_{label}.json").write_text(snap.model_dump_json(indent=1, exclude_defaults=True) + "\n", "utf-8")
        print(f"{i}_{label}.json: {sum(1 for _ in snap.root.walk())} nodes")
