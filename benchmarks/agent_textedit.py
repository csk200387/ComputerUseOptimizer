# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""텍스트 편집기 에이전트 실사용 작업 — 준비 · 판정 (둘 다 Step 로그 밖).

    uv run python benchmarks/agent_textedit.py setup   # 빈 RTF 문서를 연다 (텍스트 편집기가 꺼져 있어야 함)
    … 에이전트가 TASK를 cuop 도구로 수행 (모드는 .mcp.json --mode + /mcp 재연결) …
    uv run python benchmarks/agent_textedit.py check   # 서식 막대로 판정한 뒤 문서를 닫고 앱 종료

문서는 git이 무시하는 logs/에 둔다. 서체 크기 · 볼드체는 본문 글자의 실제 속성(AXAttributedStringForRange)으로,
중앙 정렬은 서식 막대로 판정한다 — 서식 막대 칸은 적용되지 않은 값을 보여 줄 수 있다(2026-11-03 서체 크기 18 · 실제 12).
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from AppKit import NSRunningApplication
from ApplicationServices import AXUIElementCopyParameterizedAttributeValue, AXValueCreate, kAXValueCFRangeType
from Quartz import CGEventCreateKeyboardEvent, CGEventPostToPid

from cuop.collectors.macos import MacCollector
from cuop.schema import State

APP = "com.apple.TextEdit"
TEXT = "CUOP 실사용 테스트"
TASK = f"텍스트 편집기 문서에 '{TEXT}'를 입력하고, 서체 크기 18 · 볼드체 · 중앙 정렬로 바꾼다"
DOC = Path("logs/agent_textedit.rtf")


def fonts(area, n: int) -> set[tuple[str, float]]:
    """본문 앞 n글자의 구간별 (서체 이름, 크기) — 서식이 실제로 들어갔는지."""
    rng = AXValueCreate(kAXValueCFRangeType, (0, n))
    err, s = AXUIElementCopyParameterizedAttributeValue(area, "AXAttributedStringForRange", rng, None)
    out, i = set(), 0
    while not err and i < n:
        attrs, r = s.attributesAtIndex_effectiveRange_(i, None)
        font = attrs.get("AXFont") or {}
        out.add((str(font.get("AXFontName")), float(font.get("AXFontSize") or 0)))
        i = r.location + r.length
    return out


def setup() -> None:
    if subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True).returncode == 0:
        raise SystemExit("텍스트 편집기를 먼저 종료하세요 (사용자 문서가 섞인다)")
    DOC.parent.mkdir(exist_ok=True)
    DOC.write_text("{\\rtf1\\ansi }", encoding="ascii")
    subprocess.run(["open", "-a", "TextEdit", str(DOC)], check=True)
    time.sleep(2)
    print("작업:", TASK)


async def read_doc() -> tuple[str, set[tuple[str, float]], bool]:
    """(본문 텍스트, 글자 서식, 중앙 정렬 여부) — 판정용. 서식은 실제 글자 속성, 정렬은 서식 막대."""
    c = MacCollector()
    snap = await c.snapshot(APP)
    nodes = list(snap.root.walk())  # 창이 메뉴 막대보다 앞 → 같은 이름이면 서식 막대가 먼저
    named = {n.name: n for n in reversed(nodes) if n.name}
    body = next(n for n in nodes if n.raw_role == "AXTextArea")
    text = (body.value or "").strip()
    centered = "중앙 정렬" in named and State.CHECKED in named["중앙 정렬"].states
    return text, fonts(c._el(body.id), len(text)), centered


async def check() -> bool:
    """판정을 출력하고 결과를 돌려준다. 끝나면 항상 앱을 종료한다(에이전트가 메뉴를 열어 둔 채 끝나도)."""
    try:
        text, used, centered = await read_doc()
        results = {
            "본문": text == TEXT,
            "서체 크기 18": bool(used) and all(size == 18 for _, size in used),
            "볼드체": bool(used) and all("Bold" in name for name, _ in used),
            "중앙 정렬": centered,
        }
        ok = all(results.values())
        marks = " · ".join(f"{k} {'O' if v else 'X'}" for k, v in results.items())
        print(marks, "→", "성공" if ok else "실패", "· 글자 서식", sorted(used))
        return ok
    finally:
        quit_app()


def quit_app() -> None:
    """열린 메뉴를 Esc로 닫고(메뉴가 열려 있으면 종료 요청을 처리하지 못한다) 종료될 때까지 기다린다."""
    for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(APP):
        for down in (True, False):
            CGEventPostToPid(app.processIdentifier(), CGEventCreateKeyboardEvent(None, 53, down))
        time.sleep(0.3)
        app.terminate()
    deadline = time.monotonic() + 5
    while subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True).returncode == 0:
        if time.monotonic() > deadline:
            raise RuntimeError("텍스트 편집기가 종료되지 않았다")
        time.sleep(0.2)


if __name__ == "__main__":
    setup() if sys.argv[1:] == ["setup"] else asyncio.run(check())
