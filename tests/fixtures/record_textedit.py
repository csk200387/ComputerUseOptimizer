# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""텍스트 편집기 fixture 녹화 — `uv run python tests/fixtures/record_textedit.py` (macOS, 손쉬운 사용 권한 필요)

임시 폴더의 빈 RTF 문서를 텍스트 편집기로 열고(몇 초간 맨 앞으로 나옴) 서식 막대의 볼드체 → 중앙 정렬을 누른다.
단계마다 수집기의 원본 UISnapshot을 textedit/에 저장한다. 끝나면 닫기 버튼(AXPress)으로 문서를 닫고 앱을 종료한다.
AppleEvent(osascript)는 자동화 권한 창을 띄우고 응답이 없으면 시간 초과(-1712)되므로 쓰지 않는다.

개인 정보: '최근 사용 열기'(최근 파일) · '공유'(연락처 제안) · '서비스'(설치 앱) 하위 메뉴는 비워서 저장한다.
텍스트 편집기가 이미 떠 있으면 사용자의 다른 문서가 섞이므로 녹화하지 않는다.
"""

import asyncio
import subprocess
import tempfile
import time
from pathlib import Path

from AppKit import NSRunningApplication

from cuop.collectors.macos import MacCollector
from cuop.schema import UIElementNode, UISnapshot

APP = "com.apple.TextEdit"
OUT = Path(__file__).parent / "textedit"
STEPS = [("new_doc", None), ("click_bold", "볼드체"), ("click_center", "중앙 정렬")]  # (파일 이름, 누를 요소)
SCRUB = {"최근 사용 열기", "공유", "서비스"}


def find(snap: UISnapshot, name: str) -> str:
    """전위 순서로 첫 번째 — 창이 메뉴 막대보다 앞이라 '볼드체'는 포맷 메뉴 항목이 아니라 서식 막대 체크박스."""
    return next(n.id for n in snap.root.walk() if n.name == name)


def scrub(node: UIElementNode) -> UIElementNode:
    kids = [] if node.name in SCRUB else [scrub(c) for c in node.children]
    return node.model_copy(update={"children": kids})


async def record() -> list[UISnapshot]:
    c = MacCollector()
    snap = await c.snapshot(APP)
    snaps: list[UISnapshot] = []
    for _, target in STEPS:
        if target:
            await c.click(find(snap, target))
        await asyncio.sleep(0.3)  # 화면 갱신 대기
        snap = await c.snapshot(APP)
        snaps.append(snap)
    await c.click(find(snap, "CloseButton"))  # 문서 닫기 — 자동 저장이라 묻지 않는다
    await asyncio.sleep(0.5)
    return snaps


if __name__ == "__main__":
    if subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True).returncode == 0:
        raise SystemExit("텍스트 편집기를 먼저 종료하세요 (열린 문서가 fixture에 섞인다)")
    with tempfile.TemporaryDirectory() as tmp:
        doc = Path(tmp) / "cuop-fixture.rtf"
        doc.write_text("{\\rtf1\\ansi }", encoding="ascii")
        subprocess.run(["open", "-a", "TextEdit", str(doc)], check=True)
        time.sleep(2)  # 실행 · 창 표시 대기
        try:
            snaps = asyncio.run(record())
        finally:
            for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(APP):
                app.terminate()
    OUT.mkdir(exist_ok=True)
    for i, ((label, _), snap) in enumerate(zip(STEPS, snaps, strict=True)):
        snap = snap.model_copy(update={"root": scrub(snap.root)})
        (OUT / f"{i}_{label}.json").write_text(snap.model_dump_json(indent=1, exclude_defaults=True) + "\n", "utf-8")
        print(f"{i}_{label}.json: {sum(1 for _ in snap.root.walk())} nodes")
