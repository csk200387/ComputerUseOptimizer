# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""M9 하네스 작업 — 앱 종류를 넓힌다(BRIEF §3-5): 사전 · 활성 상태 보기(시스템 도구) · 시계(Catalyst)
· VS Code(Electron) · 미리보기(문서) · 메뉴 막대 배터리(시스템 surface) · 웹 작업용 Chrome for Testing.
(서체 관리자 · Word는 시험에서 뺐다: 검색 결과 · 문서 본문이 백그라운드 접근성 트리에 나오지 않는다 — COVERAGE.md 한계)

규칙: 하네스가 띄운 앱만 끈다 · 문서는 임시 파일만 · 판정은 AX 트리 · 파일 내용으로(화면 이미지를 쓰지 않는다).
"""

import asyncio
import json
import shutil
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

from AppKit import NSRunningApplication

from cuop.collectors.macos import MacCollector
from cuop.schema import Role, State, UIElementNode

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "logs" / "m9"
CFT = Path.home() / "Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app"
COL = MacCollector()  # 요소 id는 수집기 인스턴스마다 따로 — 관찰과 클릭이 같은 인스턴스를 쓰게
_launched: dict[str, list[tuple[int, int | None]]] = {}  # 번들 ID → 이 하네스가 띄운 (pid, 프로세스 시작 시각)
_tmp: list[Path] = []


def _pids(bundle: str) -> list[int]:
    return [int(a.processIdentifier()) for a in NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)]


def _open(bundle: str, *args: str, new: bool = False) -> None:
    """백그라운드로 띄운다(`open -g`). 이미 떠 있는 인스턴스가 있으면 사용자 것일 수 있다 → 이 run을 하지 않는다
    (사용자 문서 · 작업 공간을 읽거나 고치지 않게, Codex M9 리뷰 #3 · #4)."""
    from cuop.collectors import ax

    mine = {p for p, _ in _launched.get(bundle, [])}
    if set(_pids(bundle)) - mine:
        raise RuntimeError(f"{bundle} is already running outside the harness — skipping this run")
    flags = ["-g", "-n"] if new else ["-g"]
    subprocess.run(["open", *flags, "-b", bundle, *args], check=True, capture_output=True, timeout=30)
    for _ in range(60):
        now = set(_pids(bundle)) - mine
        if now:
            _launched.setdefault(bundle, []).extend((p, ax.process_start(p)) for p in now)
            return
        if mine:
            return
        time.sleep(0.25)


def quit_launched(bundle: str) -> None:
    from cuop.collectors import ax

    for pid, start in _launched.pop(bundle, []):
        a = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if a is None or ax.process_start(pid) != start:  # 그 pid가 이제 다른 프로세스면 건드리지 않는다
            continue
        a.terminate()
        for _ in range(40):
            if a.isTerminated():
                break
            time.sleep(0.1)
        else:
            a.forceTerminate()  # 하네스가 띄운 임시 인스턴스만 — 사용자 앱에는 쓰지 않는다


async def _snap(app: str, tries: int = 40) -> list[UIElementNode]:
    """앱 창이 트리에 보일 때까지 기다린 뒤 노드 목록."""
    for _ in range(tries):
        try:
            snap = await COL.snapshot(app)
            if any(c.role in (Role.WINDOW, Role.DIALOG) for c in snap.root.children):
                return list(snap.root.walk())
        except LookupError:
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError(f"{app}: 창이 10초 안에 뜨지 않았다")


def _has(nodes: list[UIElementNode], text: str, exclude: UIElementNode | None = None) -> bool:
    """이름 · 값에 text가 있나. 사전 표제어의 장음 표시(컴퓨ː터)는 빼고 본다."""
    t = text.casefold()

    def norm(f: str | None) -> str:
        return (f or "").replace("ː", "").casefold()

    return any(n is not exclude and any(t in norm(f) for f in (n.name, n.value)) for n in nodes)


# --- 사전 (네이티브 + WebKit 본문) ---
DICT = "com.apple.Dictionary"
WORD = "컴퓨터"


def _search_field(nodes: list[UIElementNode]) -> UIElementNode | None:
    return next((n for n in nodes if n.raw_role.startswith("AXTextField") and "Search" in n.raw_role), None) or next(
        (n for n in nodes if n.role is Role.TEXTBOX), None
    )


async def dict_reset() -> None:
    _open(DICT)
    nodes = await _snap(DICT)
    field = _search_field(nodes)
    if field is not None and field.value:  # 지난 run의 검색어 — 비우고 시작
        await COL.type_text(field.id, "")
        await asyncio.sleep(0.5)


def _webarea_text(app_nodes: list[UIElementNode]) -> list[UIElementNode]:
    """본문(뜻이 나오는 웹 영역) 아래 노드만 — 검색 결과 목록 · 자동 완성은 뜻이 아니다(Codex M9 리뷰 #26)."""
    areas = [n for n in app_nodes if n.raw_role.startswith("AXWebArea")]
    return [d for a in areas for d in a.walk() if d is not a]


async def dict_check() -> bool:
    try:
        nodes = await _snap(DICT)
        field = _search_field(nodes)
        typed = field is not None and (field.value or "").strip().casefold() == WORD
        shown = _has(_webarea_text(nodes), WORD)
        print(f"사전 검색어 {'O' if typed else 'X'} · 본문에 단어 {'O' if shown else 'X'}")
        return typed and shown
    finally:
        quit_launched(DICT)


# --- 미리보기 (문서 · 메뉴 명령 · 저장) ---
PREVIEW = "com.apple.Preview"
IMG = WORK / "rotate.png"
W, H = 240, 120


def _png(path: Path) -> None:
    """왼쪽 위 사분면만 빨강, 나머지 흰색 — 회전 방향을 픽셀로 판정한다."""
    rows = b"".join(
        b"\0" + b"".join(b"\xff\x00\x00" if x < W // 2 and y < H // 2 else b"\xff\xff\xff" for x in range(W))
        for y in range(H)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    )


def _pixel_red(path: Path, x: int, y: int) -> tuple[int, int, bool]:
    """(가로, 세로, (x, y)가 빨강인가) — Quartz로 디코드."""
    import Quartz as Q

    url = Q.CFURLCreateWithFileSystemPath(None, str(path), Q.kCFURLPOSIXPathStyle, False)
    src = Q.CGImageSourceCreateWithURL(url, None)
    img = Q.CGImageSourceCreateImageAtIndex(src, 0, None)
    w, h = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img)
    data = bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img)))
    bpr, bpp = Q.CGImageGetBytesPerRow(img), Q.CGImageGetBitsPerPixel(img) // 8
    if not (0 <= x < w and 0 <= y < h):
        return w, h, False
    px = data[y * bpr + x * bpp : y * bpr + x * bpp + 3]
    return w, h, px[1] < 60 and max(px[0], px[2]) > 200  # 빨강 = 초록이 낮다(RGBA · BGRA 어느 순서든)


async def preview_reset() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    _png(IMG)
    _open(PREVIEW, str(IMG))
    await _snap(PREVIEW)


async def preview_check() -> bool:
    try:
        await asyncio.sleep(1.0)  # 저장이 디스크에 닿을 시간
        w, h, red = _pixel_red(IMG, 10, max(0, W - 10))  # 왼쪽으로 돌리면 빨강은 왼쪽 아래(새 높이 = W)
        ok = (w, h) == (H, W) and red
        print(f"이미지 {w}x{h} · 왼쪽 아래 빨강 {'O' if red else 'X'} → {'성공' if ok else '실패'}")
        return ok
    finally:
        quit_launched(PREVIEW)


# --- VS Code (Electron) — 임시 프로필 · 임시 파일 ---
CODE = "com.microsoft.VSCode"
_code: dict[str, Path] = {}


async def code_reset() -> None:
    work = Path(tempfile.mkdtemp(prefix="cuop-m9-code-"))
    prof = Path(tempfile.mkdtemp(prefix="cuop-m9-profile-"))
    _tmp.extend([work, prof])
    (work / "note.txt").write_text("", encoding="utf-8")
    (prof / "User").mkdir()
    settings = {"workbench.startupEditor": "none", "security.workspace.trust.enabled": False,
                "telemetry.telemetryLevel": "off", "update.mode": "none", "workbench.tips.enabled": False,
                "extensions.autoCheckUpdates": False, "chat.disableAIFeatures": True,
                "workbench.welcomePage.walkthroughs.openOnInstall": False,
                "editor.accessibilitySupport": "on"}  # 편집기 본문을 접근성 트리에(임시 프로필만)  # fmt: skip
    (prof / "User" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    _code.update(work=work, prof=prof)
    _open(CODE, "--args", "--user-data-dir", str(prof), "--extensions-dir", str(prof / "ext"),
          "--disable-extensions", "--new-window", str(work / "note.txt"), new=True)  # fmt: skip
    await _snap(CODE)
    await asyncio.sleep(3.0)  # 편집기가 그려질 시간


async def code_check() -> bool:
    try:
        text = (_code["work"] / "note.txt").read_text(encoding="utf-8")
        ok = "hello cuop" in text
        print(f"note.txt {len(text)}자 · 'hello cuop' {'O' if ok else 'X'}")
        return ok
    finally:
        quit_launched(CODE)
        _cleanup()


def _cleanup() -> None:
    while _tmp:
        shutil.rmtree(_tmp.pop(), ignore_errors=True)


def cleanup_all() -> None:
    """run 하나가 끝나면(준비 실패 · 판정 예외 포함) 이 하네스가 띄운 앱과 임시 폴더를 모두 정리한다."""
    for bundle in list(_launched):
        quit_launched(bundle)
    _cleanup()


# --- 웹: 하네스 전용 Chrome for Testing(새 프로필) — 두 모드가 같은 시작 상태 ---
CFT_ID = "com.google.chrome.for.testing"


async def cft_open(url: str, prefs: dict[str, Any] | None = None) -> None:
    """prefs = 새 프로필의 Default/Preferences(내려받기 폴더 등)."""
    prof = Path(tempfile.mkdtemp(prefix="cuop-m9-cft-"))
    _tmp.append(prof)
    if prefs:
        (prof / "Default").mkdir()
        (prof / "Default" / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")
    _open(CFT_ID, "--args", f"--user-data-dir={prof}", "--use-mock-keychain", "--password-store=basic",
          "--no-first-run", "--no-default-browser-check", "--window-size=1280,860", url, new=True)  # fmt: skip
    for _ in range(80):  # 페이지 제목이 창 제목이 될 때까지(빈 탭이 아님)
        try:
            snap = await COL.snapshot(CFT_ID)
        except LookupError:  # 프로세스는 떴지만 창이 아직 없다
            await asyncio.sleep(0.25)
            continue
        win = next((c for c in snap.root.children if c.role is Role.WINDOW), None)
        if win is not None and win.name and "제목 없음" not in win.name and "Untitled" not in win.name:
            return
        await asyncio.sleep(0.25)


def cft_close() -> None:
    quit_launched(CFT_ID)
    _cleanup()


# --- 활성 상태 보기 · 서체 관리자 · 시계: 트리 구조는 앱마다 달라 판정 함수에 적는다 ---
ACTIVITY = "com.apple.ActivityMonitor"
FONTBOOK = "com.apple.FontBook"
CLOCK = "com.apple.clock"


def _selected(n: UIElementNode) -> bool:
    return State.SELECTED in n.states or State.CHECKED in n.states


# --- Word (Office 문서) — 임시 .docx ---
WORD_ID = "com.microsoft.Word"
DOCX = WORK / "word-task.docx"
_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/'
       'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
       'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/'
       'document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
       '</Types>')  # fmt: skip
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats'
         '.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
         'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')  # fmt: skip
_DOC = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.'
    'org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>CUOP 문서</w:t></w:r></w:p></w:body></w:document>'
)


def _docx(path: Path) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", _DOC)


def docx_text(path: Path) -> str:
    import re
    import zipfile

    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))


# --- 활성 상태 보기 (시스템 도구) ---
def _by(nodes: list[UIElementNode], role: Role, name: str) -> UIElementNode | None:
    return next((n for n in nodes if n.role is role and n.name == name), None)


def _toolbar_search(nodes: list[UIElementNode]) -> UIElementNode | None:
    return next((n for n in reversed(nodes) if n.role is Role.TEXTBOX), None)  # 툴바 검색 칸(마지막 입력 칸)


async def activity_reset() -> None:
    _open(ACTIVITY)
    nodes = await _snap(ACTIVITY)
    col = COL
    mem, cpu = _by(nodes, Role.RADIO, "메모리"), _by(nodes, Role.RADIO, "CPU")
    if mem is not None and _selected(mem) and cpu is not None:
        await col.click(cpu.id)
    field = _toolbar_search(nodes)
    if field is not None and field.value:
        await col.type_text(field.id, "")
    await asyncio.sleep(0.5)


async def activity_check() -> bool:
    try:
        nodes = await _snap(ACTIVITY)
        mem, field = _by(nodes, Role.RADIO, "메모리"), _toolbar_search(nodes)
        tab = mem is not None and _selected(mem)
        typed = field is not None and (field.value or "").strip() == "Finder"
        print(f"메모리 탭 {'O' if tab else 'X'} · 검색 'Finder' {'O' if typed else 'X'}")
        return tab and typed
    finally:
        quit_launched(ACTIVITY)


# --- 시계 (Catalyst) — 스톱워치 ---
async def _clock_tab(name: str) -> list[UIElementNode]:
    nodes = await _snap(CLOCK)
    tab = _by(nodes, Role.RADIO, name)
    if tab is not None and not _selected(tab):
        await COL.click(tab.id)
        await asyncio.sleep(0.5)
        nodes = await _snap(CLOCK)
    return nodes


async def _stopwatch_clear() -> None:
    col = COL
    nodes = await _clock_tab("스톱워치")
    if (stop := _by(nodes, Role.BUTTON, "중단")) is not None:
        await col.click(stop.id)
        await asyncio.sleep(0.3)
        nodes = await _snap(CLOCK)
    if (clear := _by(nodes, Role.BUTTON, "재설정")) is not None:
        await col.click(clear.id)
        await asyncio.sleep(0.3)


async def clock_reset() -> None:
    _open(CLOCK)
    await _stopwatch_clear()
    await _clock_tab("타이머")  # 다른 탭에서 시작 — 에이전트가 스톱워치를 찾아가게


async def clock_check() -> bool:
    try:
        nodes = await _clock_tab("스톱워치")
        import re

        clock = re.compile(
            r"^(\d+시간 )?(\d+분 )?\d+(\.\d+)?초$"
        )  # 경과 시간 표시('7.98초', '1분 3.2초') — role은 수집기마다 다르다
        shown = next((n.name for n in nodes if clock.match(n.name or "")), None)
        stopped = _by(nodes, Role.BUTTON, "시작") is not None and _by(nodes, Role.BUTTON, "재설정") is not None
        ok = stopped and shown not in (None, "0초")
        print(f"스톱워치 {shown!r} · 정지 {'O' if stopped else 'X'} → {'성공' if ok else '실패'}")
        return ok
    finally:
        await _stopwatch_clear()
        quit_launched(CLOCK)


# --- 메뉴 막대 배터리(시스템 surface) — 에이전트의 마지막 답으로 판정 ---
ANSWER: list[str] = [""]  # run.py가 에이전트의 최종 답을 넣는다


async def battery_reset() -> None:
    ANSWER[0] = ""


def _battery() -> int:
    import re

    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    return int(re.search(r"(\d+)%", out).group(1))  # type: ignore[union-attr]


async def battery_check() -> bool:
    import re

    try:
        want = _battery()
        said = {int(x) for x in re.findall(r"(\d{1,3})\s*%", ANSWER[0])}
        ok = len(said) == 1 and abs(next(iter(said)) - want) <= 1  # 답한 퍼센트가 하나뿐이어야(여러 추측은 실패)
        print(f"배터리 {want}% · 답 {sorted(said)} → {'성공' if ok else '실패'}")
        return ok
    finally:
        _close_menus("com.apple.controlcenter")


def _close_menus(bundle: str) -> None:
    """에이전트가 열어 둔 메뉴 · 팝오버를 Esc로 닫는다(그 프로세스에만)."""
    from Quartz import CGEventCreateKeyboardEvent, CGEventPostToPid

    for pid in _pids(bundle):
        for down in (True, False):
            CGEventPostToPid(pid, CGEventCreateKeyboardEvent(None, 53, down))
