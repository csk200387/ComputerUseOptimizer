# pyright: basic
# pyright: reportAttributeAccessIssue=false, reportOptionalSubscript=false, reportOptionalMemberAccess=false
# pyright: reportArgumentType=false
"""Office 시험(docs/parity/OFFICE_BRIEF.md) — Word · Excel · PowerPoint만, 일회용 문서(logs/office/)로.

uv run python benchmarks/tasks_office.py probe [word excel powerpoint]   # 앱 × 항목 측정 → logs/office/probe.json
하네스 작업(run.py TASKS): word · excel · powerpoint — reset(새 문서를 백그라운드로) · check(저장 → 파일 파싱).

규칙: 이미 떠 있는 Office(사용자 것일 수 있음)는 건드리지 않고 그 앱을 뺀다 ·
하네스가 띄운 앱만 '저장 안 함'으로 닫는다 · 측정 결과는 숫자 · 참거짓만 남긴다(문서는 시험용 글자뿐).
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import tasks_m9 as m9
from ApplicationServices import AXUIElementPerformAction, AXUIElementSetAttributeValue
from docx import Document
from openpyxl import Workbook, load_workbook
from oracle_compare import screenshot_tokens, tokens
from pptx import Presentation

from cuop.collectors import ax
from cuop.desktop.mcp import render

DIR = m9.ROOT / "logs" / "office"
COMPARE = m9.ROOT / "logs" / "compare"
WORD, EXCEL, PPT = "com.microsoft.Word", "com.microsoft.Excel", "com.microsoft.Powerpoint"
PARAS = ["사과 문단은 첫째 문단입니다.", "바나나 문단은 둘째 문단입니다.", "체리 문단은 셋째 문단입니다."]
CELLS = [["품목", "수량", "단가"], ["사과", 3, 1200], ["바나나", 5, 800], ["체리", 2, 3000], ["포도", 4, 2500]]
SLIDE = ("분기 보고 제목", "매출 요약 본문")
# 트리에 내용이 나왔나 = 이 글자들이 몇 개 보이나(Excel은 한 자리 숫자를 빼고 — 다른 곳에도 흔하다)
MARKERS = {
    "word": PARAS,
    "excel": [str(v) for row in CELLS for v in row if not (isinstance(v, int) and v < 10)],
    "powerpoint": list(SLIDE),
}
APPS = {"word": (WORD, "docx"), "excel": (EXCEL, "xlsx"), "powerpoint": (PPT, "pptx")}
DONT_SAVE = ("저장 안 함", "Don't Save")
SAVE = ("저장", "Save")
CANCEL = ("취소", "Cancel")
STEM = "cuop-test"
MARK = "cuop harness fixture"  # 시험 문서의 '주제' 속성 — 이 표식이 있는 파일만 갈아 끼운다(사용자 문서를 지우지 않게)


def _ours(path: Path) -> bool:
    """하네스가 만든 시험 문서인가: 문서 속성 '주제'가 MARK. 읽지 못하면 아니다."""
    try:
        if path.suffix == ".docx":
            return Document(str(path)).core_properties.subject == MARK
        if path.suffix == ".xlsx":
            return load_workbook(path, read_only=True).properties.subject == MARK
        return Presentation(str(path)).core_properties.subject == MARK
    except Exception:  # noqa: BLE001 — 깨진 파일 · 다른 형식
        return False


def _write(tmp: Path, ext: str) -> None:
    if ext == "docx":
        d = Document()
        d.core_properties.subject = MARK
        for p in PARAS:
            d.add_paragraph(p)
        d.save(tmp)
    elif ext == "xlsx":
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "시험"
        for row in CELLS:
            ws.append(row)
        ws["B6"] = "=SUM(B2:B5)"  # 처음 14
        wb.properties.subject = MARK
        wb.save(tmp)
    else:
        prs = Presentation()
        prs.core_properties.subject = MARK
        s = prs.slides.add_slide(prs.slide_layouts[1])  # 제목 및 내용
        s.shapes.title.text = SLIDE[0]
        s.placeholders[1].text = SLIDE[1]
        prs.save(tmp)


def make(ext: str) -> Path:
    """처음 상태의 시험 문서를 새로 쓴다. 이름은 사용자 문서와 겹치지 않게 `cuop-test`."""
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"{STEM}.{ext}"
    if path.is_symlink() or (path.exists() and not _ours(path)):  # 하네스가 만든 시험 문서(표식)만 갈아 끼운다
        raise RuntimeError(f"{path} 는 하네스가 만든 시험 문서가 아니다 — 지우지 않고 run 제외")
    fd, name = tempfile.mkstemp(dir=DIR, prefix=f".{STEM}-", suffix=f".{ext}")  # 새 이름 — 있는 파일을 지우지 않는다
    os.close(fd)
    tmp = Path(name)
    try:
        _write(tmp, ext)
        os.replace(tmp, path)  # 다 쓴 뒤에만 바꾼다 — 만들다 실패해도 지난 파일을 잃지 않는다
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _made[path] = path.stat().st_mtime
    return path


# --- 앱 열기 · 닫기: 하네스가 띄운 인스턴스의 시험 문서 창만 만진다(Codex Office 리뷰 #1 · #4) ---
_made: dict[Path, float] = {}  # 시험 문서 → 만든 시각(mtime) — 저장됐는지는 이것과 비교한다


def _kids(el: Any) -> list[Any]:
    return list(ax.attr(el, "AXChildren") or [])


def _windows(bundle: str, pid: int | None = None) -> list[Any]:
    pids = [pid] if pid else [p for p, _ in m9._launched.get(bundle, [])]
    return [w for p in pids for w in (ax.attr(ax.app_element(p), "AXWindows") or [])]


def _doc_path(w: Any) -> Path | None:
    doc = ax.attr(w, "AXDocument")
    return Path(unquote(urlparse(doc).path)) if isinstance(doc, str) and doc else None


def _fixture(w: Any) -> bool:
    """시험 문서 창인가 — 창이 가리키는 파일(AXDocument)이 있으면 그것으로, 없으면 제목으로."""
    doc = ax.attr(
        w, "AXDocument"
    )  # 세 앱 모두 준다(2026-11-22 확인) — 없으면 시험 문서로 보지 않는다(제목은 믿지 않음)
    if not isinstance(doc, str) or not doc:
        return False
    p = Path(unquote(urlparse(doc).path))  # 정확히 이 폴더의 cuop-test.<확장자> · 이 프로세스가 만든 표식 문서
    if p in _made or (p.parent == DIR and p.name in {f"{STEM}.{ext}" for _, ext in APPS.values()}):
        return True
    # 비교 하네스(compare_codex_cu)의 문서 — 앞 run이 남겨 Word가 복구한 창도 표식이 있으면 시험 문서
    return p.parent == COMPARE and p.name.startswith("report-") and p.suffix == ".docx" and _ours(p)


async def open_doc(app: str, make_doc: Any = None) -> Path:
    """make_doc = 시험 문서를 만드는 함수(기본 cuop-test.<확장자>) — 다른 하네스(compare_codex_cu)의 문서."""
    bundle, ext = APPS[app]
    if set(m9._pids(bundle)) - {p for p, _ in m9._launched.get(bundle, [])}:  # 시험 문서를 쓰기 전에
        raise RuntimeError(f"{bundle} is already running outside the harness — skipping this run")
    path = make_doc() if make_doc else make(ext)
    m9._open(bundle, str(path))  # 사용자 인스턴스가 떠 있으면 RuntimeError → 이 run 제외
    for _ in range(120):  # 첫 실행은 10초 넘게 걸린다
        docs = [w for w in _windows(bundle) if ax.attr(w, "AXSubrole") == "AXStandardWindow"]
        if any(_doc_path(w) == path for w in docs):  # 이번에 만든 그 파일 — 복구된 지난 시험 문서는 아니다
            await asyncio.sleep(1.5)  # 리본이 다 그려지고, 앱이 지난 문서를 복원한다면 그 창도 뜨게
            others = [w for w in _windows(bundle) if ax.attr(w, "AXSubrole") == "AXStandardWindow"
                      and _doc_path(w) != path]  # fmt: skip
            if others:  # 복원된 사용자 문서 — 에이전트에게 보이면 안 된다 → 이 run을 하지 않는다
                raise RuntimeError(f"{bundle}: 시험 문서 말고 다른 문서 창 {len(others)}개가 열렸다 — run 제외")
            return path
        await asyncio.sleep(0.25)
    raise RuntimeError(f"{bundle}: 문서 창이 30초 안에 뜨지 않았다 (로그인 · 첫 실행 안내 창이면 BLOCKED)")


def _dont_save(pid: int, w: Any) -> bool:
    """저장 확인(창의 sheet 또는 AXWindows에 없는 앱 경고창 — PowerPoint)의 '저장 안 함(N)'을 누른다."""
    app = ax.app_element(pid)
    focused = ax.attr(app, "AXFocusedWindow")
    docs = [d for d in ax.attr(app, "AXWindows") or [] if ax.attr(d, "AXSubrole") == "AXStandardWindow"]
    named = re.compile(rf"(?<![\w-]){re.escape(STEM)}(\.(docx|xlsx|pptx))?(?![\w.-])")  # cuop-test-notes는 아니다
    about = (
        focused is not None
        and all(_fixture(d) for d in docs)  # 앱 창 목록에 없는 경고 — 열린 문서가 시험 문서뿐일 때만
        and any(named.search(str(ax.attr(k, "AXValue") or "")) for k in _kids(focused))
    )
    for dlg in [k for k in _kids(w) if ax.attr(k, "AXRole") == "AXSheet"] + ([focused] if about else []):
        no = next((k for k in _kids(dlg) if str(ax.attr(k, "AXTitle") or "").startswith(DONT_SAVE)), None)
        if no is not None:
            return AXUIElementPerformAction(no, "AXPress") == 0
    return False


def _buttons(el: Any, depth: int = 3) -> list[Any]:
    kids = _kids(el)
    out = [k for k in kids if ax.attr(k, "AXRole") == "AXButton"]
    return out + [b for k in kids for b in _buttons(k, depth - 1)] if depth > 1 else out


def _dismiss_dialogs(pid: int) -> None:
    """에이전트가 시험 문서에서 연 앱 모달 대화상자(Excel '이동' · '참조가 잘못되었습니다')를 닫는다 —
    대화상자가 있으면 문서 창을 닫을 수 없다(본 실험 C회차). 열린 문서 창이 시험 문서뿐이고, 앱 창 목록에 있는
    포커스 대화상자일 때 그 '취소'만 누른다."""
    app = ax.app_element(pid)
    wins = list(ax.attr(app, "AXWindows") or [])
    docs = [w for w in wins if ax.attr(w, "AXSubrole") == "AXStandardWindow"]
    dlg = ax.attr(app, "AXFocusedWindow")
    if not docs or not all(_fixture(w) for w in docs) or dlg not in wins or ax.attr(dlg, "AXSubrole") != "AXDialog":
        return
    # '취소'만 — '확인'은 시험과 무관한 앱 알림(로그인 · 라이선스 안내 등)일 수 있어 누르지 않는다(OFFICE_BRIEF)
    pick = next((b for b in _buttons(dlg) if str(ax.attr(b, "AXTitle") or "") in CANCEL), None)
    if pick is not None:
        AXUIElementPerformAction(pick, "AXPress")
        time.sleep(0.5)


def _quit(pid: int, start: int | None) -> bool:
    """정상 종료만 요청한다(저장 확인이 뜨면 앱이 남는다 — 강제 종료하지 않는다). 종료됐으면 True."""
    a = m9.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if a is None or ax.process_start(pid) != start:
        return True
    a.terminate()
    for _ in range(150):  # Office는 창을 다 닫고도 종료에 10초 넘게 걸린다. isTerminated는 런루프 없이는 안 바뀐다
        if ax.process_start(pid) != start:
            return True
        time.sleep(0.2)
    return False


def close_doc(bundle: str) -> bool:
    """시험 문서 창을 '저장 안 함'으로 닫고 앱에 정상 종료를 요청한다. Office는 강제 종료하지 않는다 — 다른 창(복원된
    사용자 문서 · 저장 안 된 창)이 있으면 그 문서를 잃을 수 있고, 다음 실행에 복구 창이 뜬다. 추적은 언제나 여기서 빼서
    일반 정리(cleanup_all)의 강제 종료가 Office에 닿지 않게 한다. 앱이 남으면 다음 run은 '이미 실행 중'으로 빠진다."""
    entries = m9._launched.pop(bundle, [])  # 먼저 뺀다 — 아래에서 예외가 나도 일반 정리가 Office를 강제 종료하지 않게
    done = True
    for pid, start in entries:
        if start is None or ax.process_start(pid) != start:  # 끝났거나 pid 재사용 · 시작 시각을 모름 — 건드리지 않는다
            continue
        try:
            for _ in range(3):  # 경고를 닫으면 그 뒤의 대화상자('이동')가 남는다
                _dismiss_dialogs(pid)
            for w in [w for w in _windows(bundle, pid) if _fixture(w)]:
                btn = next((k for k in _kids(w) if ax.attr(k, "AXSubrole") == "AXCloseButton"), None)
                if btn is not None:
                    AXUIElementPerformAction(btn, "AXPress")
                for _ in range(50):
                    if ax.attr(w, "AXRole") is None:  # 닫혔다
                        break
                    _dont_save(pid, w)
                    time.sleep(0.1)
            for _ in range(50):  # 닫힌 창이 목록에서 빠질 때까지
                if not any(_fixture(w) for w in _windows(bundle, pid)):
                    break
                time.sleep(0.1)
            kinds = ("AXStandardWindow", "AXDialog", "AXSystemDialog")  # 문서 · 대화상자(떠 있는 도구 창은 빼고)
            ws = ax.attr(ax.app_element(pid), "AXWindows")
            if ws is None:  # 창 목록을 못 읽으면 남은 창이 없다고 볼 수 없다 → 종료하지 않는다
                raise RuntimeError("AXWindows unreadable")
            others = sum(1 for w in ws if ax.attr(w, "AXSubrole") in kinds)
        except Exception as e:  # noqa: BLE001 — 창을 다 셀 수 없으면 종료도 하지 않는다
            print(f"정리: {bundle} 창 확인 실패({type(e).__name__}) · 종료 안 함")
            done = False
            continue
        if others:  # 시험 문서 말고 남은 창이 있으면 종료하지 않는다 — 다음 run은 '이미 실행 중'으로 빠진다
            print(f"정리: {bundle} 남은 창 {others}개 · 종료 안 함")
            done = False
        elif not _quit(pid, start):
            print(f"정리: {bundle} 종료 안 됨(강제 종료 안 함)")
            done = False
    return done


def close_all() -> None:
    for bundle, _ in APPS.values():
        if bundle in m9._launched:
            try:
                close_doc(bundle)
            except Exception as e:  # 정리 하나가 실패해도 나머지 정리(cleanup_all)는 하게
                print(f"정리 오류: {bundle}: {type(e).__name__}: {e}")


def save(bundle: str, path: Path) -> bool:
    """시험 문서 창의 '저장' 버튼(AXPress). Word는 앞에 있을 때만 저장한다(백그라운드 AXPress는 0을 돌려주고 무시).
    성공 = 만든 뒤로 파일이 바뀌었다(에이전트가 이미 저장했어도)."""
    for w in [w for w in _windows(bundle) if _fixture(w)]:
        bar = next((k for k in _kids(w) if ax.attr(k, "AXRole") == "AXToolbar"), None)
        btns = [b for g in _kids(bar) for b in _kids(g)] if bar is not None else []
        btn = next((b for b in btns if ax.attr(b, "AXTitle") in SAVE), None)
        if btn is None:
            continue
        for p, _ in m9._launched.get(bundle, []):
            a = m9.NSRunningApplication.runningApplicationWithProcessIdentifier_(p)
            if a is not None:
                a.activateWithOptions_(0)
        time.sleep(1.0)
        before = path.stat().st_mtime
        AXUIElementPerformAction(btn, "AXPress")
        for _ in range(40):
            if path.stat().st_mtime != before:
                break
            time.sleep(0.2)
        time.sleep(0.5)  # 쓰기가 끝나게
    return path.stat().st_mtime != _made.get(path)


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", "")))


# --- 하네스 작업 (판정 = 저장한 파일이 요청한 변경만 담았나) ---
async def word_reset() -> None:
    await open_doc("word")


async def word_check() -> bool:
    try:
        path = DIR / f"{STEM}.docx"
        saved = save(WORD, path)
        d = Document(str(path))
        texts = [p.text for p in d.paragraphs]
        runs = [r for p in d.paragraphs for r in p.runs if r.text.strip()]
        ok = saved and texts == ["검토: " + PARAS[0], *PARAS[1:]] and bool(runs) and all(r.bold for r in runs)
        print(f"word 저장 {saved} · 문단 일치 {texts == ['검토: ' + PARAS[0], *PARAS[1:]]} · "
              f"굵게 {sum(bool(r.bold) for r in runs)}/{len(runs)}")  # fmt: skip
        return ok
    finally:
        close_doc(WORD)


async def excel_reset() -> None:
    m9.ANSWER[0] = ""
    await open_doc("excel")


async def excel_check() -> bool:
    try:
        path = DIR / f"{STEM}.xlsx"
        saved = save(EXCEL, path)
        ws = load_workbook(path, data_only=True).active  # Excel이 저장할 때 계산한 값
        raw = load_workbook(path).active
        want = [[10 if (r, c) == (1, 1) else v for c, v in enumerate(row)] for r, row in enumerate(CELLS)]
        cells = [[ws.cell(r + 1, c + 1).value for c in range(3)] for r in range(len(CELLS))]
        same = cells == want and raw["B6"].value == "=SUM(B2:B5)" and (ws.max_row, ws.max_column) == (6, 3)
        said = "21" in _numbers(m9.ANSWER[0])
        ok = saved and same and ws["B6"].value == 21 and said
        print(f"excel 저장 {saved} · 셀 · 수식 일치 {same} · B6 {ws['B6'].value} · 답에 21 {said}")
        return ok
    finally:
        close_doc(EXCEL)


async def powerpoint_reset() -> None:
    await open_doc("powerpoint")


async def powerpoint_check() -> bool:
    try:
        path = DIR / f"{STEM}.pptx"
        saved = save(PPT, path)
        slides = list(Presentation(str(path)).slides)
        titles = [s.shapes.title.text if s.shapes.title is not None else "" for s in slides]
        first = slides[0].placeholders[1].text if slides and len(slides[0].placeholders) > 1 else None
        ok = saved and titles == [SLIDE[0], "2분기 실적"] and first == SLIDE[1]
        print(f"powerpoint 저장 {saved} · 제목 {titles} · 1번 본문 일치 {first == SLIDE[1]}")
        return ok
    finally:
        close_doc(PPT)


# --- 측정(probe): 항목 1 · 2 · 3 · 4 ---
SESSION = "office-probe"


def cli(*args: str, session: str = SESSION) -> dict[str, Any]:
    # 하네스처럼 Office 세 앱만 읽고 누르게 — 좌표 클릭이 앞을 가린 다른 앱 창에 닿지 않게
    env = os.environ | {
        "CUOP_SESSION": session,
        "CUOP_OVERLAY": "0",
        "CUOP_ALLOW_APPS": ",".join(b for b, _ in APPS.values()),
    }
    out = subprocess.run(["uv", "run", "cuop", *args], capture_output=True, text=True, timeout=120, env=env).stdout
    try:
        return json.loads(out)
    except ValueError:
        return {"ok": False, "error": {"code": "BAD_OUTPUT"}}


def measure(app: str, *extra: str) -> dict[str, Any]:
    """그 순간 트리: 시험 글자가 몇 개 보이나 · AD JSON 토큰 · CUOP 텍스트 토큰(MCP 텍스트 모드가 주는 문자열)."""
    bundle, fresh = APPS[app][0], f"{SESSION}-{time.monotonic_ns()}"  # 새 세션 = 첫 관찰(이전 뷰 없음)
    full = cli("snapshot", "--app", bundle, *extra, session=fresh)
    raw = json.dumps(full, ensure_ascii=False)
    text = cli("snapshot", "--app", bundle, "--format", "text", *extra, session=fresh)
    found = sum(m in raw for m in MARKERS[app])
    return {"found": f"{found}/{len(MARKERS[app])}", "refs": (full.get("data") or {}).get("ref_count"),
            "json_tokens": tokens(raw), "text_tokens": tokens(render(text)) if text.get("ok") else None}  # fmt: skip


def ref_in(text: str, pattern: str, before: int = 0) -> str | None:
    """텍스트 뷰에서 pattern이 든 줄의 ref(before=1이면 그 앞 줄 — 부모 scrollarea)."""
    head = re.match(r"\[@(\w+)", text)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if pattern in line and head:
            m = re.search(r"\be(\d+)\b", lines[i - before])
            return f"@{head.group(1)}:e{m.group(1)}" if m else None
    return None


def find(bundle: str, *args: str) -> str | None:
    d = cli("find", "--app", bundle, *args)
    ms = (d.get("data") or {}).get("matches") or []
    return next((m["ref_id"] for m in ms if m.get("ref_id")), None)


def step(rows: list[dict[str, Any]], label: str, *args: str) -> dict[str, Any]:
    env = cli(*args, "--format", "text")
    err = env.get("error") or {}
    data = env.get("data") or {}
    rows.append({"step": label, "ok": bool(env.get("ok")), "code": err.get("code"),
                 "delivery": (data.get("disposition") or err.get("disposition") or {}).get("delivery"),
                 "tokens": tokens(render(env))})  # fmt: skip
    return env


def keys(rows: list[dict[str, Any]], combo: str, bundle: str, after: dict[str, Any]) -> dict[str, Any]:
    """키는 앞 단계가 성공했을 때만, 시험 문서 창을 지정해서 보낸다 — `press --window-id`는 그 창이 앱의 포커스 창이
    아니면 보내지 않는다. 앱 전체로 보내면 그 사이 포커스를 가져간 대화상자(로그인 · 라이선스)의 기본 단추를
    누를 수 있다(Codex Office 리뷰)."""
    wins = cli("list-windows", "--app", bundle).get("data") or []
    wid = next((w["id"] for w in wins if isinstance(w, dict) and str(w.get("title", "")).startswith(STEM)), None)
    if not after.get("ok") or wid is None:
        rows.append({"step": f"press {combo}", "ok": False, "code": "SKIPPED"})
        return {"ok": False}
    return step(rows, f"press {combo}", "press", combo, "--window-id", wid, "--headed")


def _value(bundle: str, ref: str | None) -> str:
    return str(((cli("get", ref, "--property", "value").get("data") or {}).get("value")) or "") if ref else ""


def manipulate(app: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bundle, rows = APPS[app][0], []
    if app == "word":
        body = find(bundle, "--role", "textbox")
        before = _value(bundle, body)
        step(rows, "type headless", "type", str(body), "검토: ")
        headless_changed = _value(bundle, body) != before
        keys(rows, "cmd+a", bundle, step(rows, "type headed", "type", str(body), "검토: ", "--headed"))
        step(rows, "click 굵게 (headless)", "click", str(find(bundle, "--name", "굵게")))
        path = DIR / f"{STEM}.docx"
        saved = save(WORD, path)
        d = Document(str(path))
        runs = [r for p in d.paragraphs for r in p.runs if r.text.strip()]
        result = {"headless_insert_changed": headless_changed, "saved": saved,
                  "first_starts": d.paragraphs[0].text.startswith("검토: 사과"),
                  "bold_runs": f"{sum(bool(r.bold) for r in runs)}/{len(runs)}"}  # fmt: skip
    elif app == "excel":
        nb = find(bundle, "--name", "이름 상자")
        step(rows, "type 이름 상자 headless", "type", str(nb), "B2")
        keys(rows, "return", bundle, step(rows, "type 이름 상자 B2 headed", "type", str(nb), "B2", "--headed"))
        snap = cli("snapshot", "--app", bundle, "--format", "text", "--goal", "콘텐츠 영역")
        grid = ref_in((snap.get("data") or {}).get("text") or "", 'layoutitem "콘텐츠 영역"')
        step(rows, "type 격자 headless", "type", str(grid), "10")
        keys(rows, "return", bundle, step(rows, "type 격자 10 headed", "type", str(grid), "10", "--headed"))
        keys(rows, "return", bundle, step(rows, "type 이름 상자 B6", "type", str(nb), "B6", "--headed"))
        bar = render(step(rows, "snapshot goal 수식 입력줄", "snapshot", "--app", bundle, "--goal", "수식 입력줄"))
        keys(rows, "return", bundle, step(rows, "type 이름 상자 B2:B5", "type", str(nb), "B2:B5", "--headed"))
        status = render(step(rows, "snapshot goal 합계", "snapshot", "--app", bundle, "--goal", "합계"))
        path = DIR / f"{STEM}.xlsx"
        saved = save(EXCEL, path)
        ws = load_workbook(path, data_only=True).active
        result = {"formula_bar_b6": "SUM(B2:B5)" in bar, "b6_value_in_tree": bool(re.search(r"B,6 21\b", bar)),
                  "status_sum_21": "합계: 21" in status, "saved": saved, "b2": ws["B2"].value,
                  "b6": ws["B6"].value}  # fmt: skip
    else:
        step(rows, "click 새 슬라이드 (headless)", "click", str(find(bundle, "--name", "새 슬라이드")))
        snap = cli("snapshot", "--app", bundle, "--format", "text", "--goal", "슬라이드 편집기")
        editor = ref_in((snap.get("data") or {}).get("text") or "", "슬라이드 편집기 창", before=1)
        step(rows, "type 제목 headless", "type", str(editor), "2분기 실적")
        # 개체 틀을 고르지 않고 치면 글자가 버려진다 → 편집 영역 포커스 · Tab(첫 개체 틀 = 제목) · Return(글자 편집)
        tab = keys(rows, "tab", bundle, step(rows, "focus 슬라이드 편집 영역", "focus", str(editor)))
        keys(rows, "return", bundle, tab)
        step(rows, "type 제목 headed", "type", str(editor), "2분기 실적", "--headed")
        time.sleep(1.0)
        cells = json.dumps(cli("find", "--app", bundle, "--name", "번째 슬라이드"), ensure_ascii=False)
        path = DIR / f"{STEM}.pptx"
        saved = save(PPT, path)
        slides = list(Presentation(str(path)).slides)
        result = {"thumbnail_title_in_tree": "2분기 실적, 2개 슬라이드 중 2번째" in cells, "saved": saved,
                  "slides": len(slides), "title2": slides[1].shapes.title.text == "2분기 실적" if len(slides) > 1
                  else False}  # fmt: skip
    return rows, result


def _scale() -> float:
    disp = (cli("list-displays").get("data") or [{}])[0]
    return float(disp.get("scale", 2.0))


def _window_tokens(bundle: str, dpr: float) -> int:
    wins = (cli("list-windows", "--app", bundle).get("data")) or []
    b = next((w["bounds"] for w in wins if w.get("accessible")), None)
    return screenshot_tokens(b["width"] * dpr, b["height"] * dpr) if b else 0


def probe(app: str) -> dict[str, Any]:
    bundle = APPS[app][0]
    if m9._pids(bundle):
        return {"app": app, "skipped": "already running outside the probe — not touched"}
    out: dict[str, Any] = {"app": app}
    chess = not m9._pids("com.apple.Chess")
    try:
        asyncio.run(open_doc(app))
        out["screenshot_tokens"] = _window_tokens(bundle, _scale())
        out["bg_fresh"] = measure(app)
        out["bg_fresh_depth40"] = measure(app, "--max-depth", "40")
        pid = m9._launched[bundle][0][0]
        app_el = ax.app_element(pid)
        out["flags"] = {a: AXUIElementSetAttributeValue(app_el, a, True)
                        for a in ("AXEnhancedUserInterface", "AXManualAccessibility")}  # fmt: skip
        time.sleep(1.0)
        out["bg_flags"] = measure(app)
        cli("focus-window", "--app", bundle, "--headed")
        t0, front = time.monotonic(), measure(app)
        while time.monotonic() - t0 < 10 and front["found"] == out["bg_flags"]["found"]:
            time.sleep(1.0)
            front = measure(app)
        out["front"] = front | {"after_s": round(time.monotonic() - t0, 1)}
        subprocess.run(["open", "-b", "com.apple.Chess"], check=False)  # 같은 Space에서 앞을 가린다
        time.sleep(3.0)
        out["bg_after_front"] = measure(app)
        rows, result = manipulate(app)
        out["steps"], out["result"] = rows, result
    finally:
        close_doc(bundle)
        if chess:
            for a in m9.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.Chess"):
                a.terminate()
    return out


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "probe":
        sys.exit(__doc__)
    apps = sys.argv[2:] or list(APPS)
    with tempfile.TemporaryDirectory(prefix="cuop-office-") as home:  # refmap · 뷰(화면 글자)는 측정 뒤 지운다
        os.environ["CUOP_HOME"] = home
        res = [probe(a) for a in apps]
    out = DIR / "probe.json"  # 앱별로 갈아 끼운다(다른 앱의 이전 결과는 둔다)
    prev = json.loads(out.read_text()) if out.is_file() and not out.is_symlink() else []
    old = [r for r in prev if r["app"] not in apps]
    fd, name = tempfile.mkstemp(dir=DIR, prefix=".probe-", suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.write_text(json.dumps(old + res, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)  # 링크가 있어도 링크 자리만 바꾼다(가리키는 파일을 덮어쓰지 않게)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
