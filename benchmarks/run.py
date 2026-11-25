"""하네스 — 헤드리스 Claude Code 에이전트로 작업 × 모드 × 반복을 실행한다 (2026-11-03 사용자 결정).

    uv run python benchmarks/run.py [--tasks calculator textedit] [--modes screenshot full …] [--repeat 1]

run마다: 앱 초기화 → 임시 MCP 설정(모드 · CUOP_RUN_ID)으로 `claude -p` 실행(cuop 서버가 새로 뜬다) → 판정.
에이전트는 빈 폴더에서 `--setting-sources ""`로 돌려 이 저장소의 CLAUDE.md · 훅 · 메모리를 보지 못하고,
`--tools ""`라 cuop 도구로만 조작한다. 요약은 logs/runs.jsonl, 원본 stream-json은 logs/runs/<run_id>.jsonl.
끝에 benchmarks/results.md '에이전트 하네스' 표의 행을 출력한다.
"""

import argparse
import asyncio
import functools
import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import agent_textedit
import live_calculator
import tasks_m9 as m9
import tasks_office as office

from cuop.collectors.macos import MacCollector
from cuop.schema import Role

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
MODEL, EFFORT = "sonnet", "medium"  # 모든 run 고정 — 비교 조건
BUDGET_USD, TIMEOUT_S = 2.0, 900  # run당 상한: 에이전트가 헤매도 비용 · 시간이 새지 않게


ORDERS: list[dict[str, Any]] = []  # 웹 주문서가 POST한 주문 — webwizard 판정용


class _Pages(SimpleHTTPRequestHandler):
    """benchmarks/pages를 제공하고 POST /order를 받는다 (127.0.0.1 전용)."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        ORDERS.append(json.loads(body or b"{}"))
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 요청마다 찍히는 로그로 하네스 출력을 덮지 않게


def _serve() -> str:
    handler = functools.partial(_Pages, directory=str(Path(__file__).parent / "pages"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}"


@dataclass(frozen=True)
class Task:
    prompt: str  # 에이전트에게 주는 지시문 — UI 경로 힌트 없이 할 일만. {url} = 하네스 페이지 서버
    reset: Callable[[], Awaitable[None]]  # 매 run 같은 시작 상태
    check: Callable[[], Awaitable[bool]]  # 성공 판정 (끝나면 정리까지)


async def _calc_reset() -> None:
    await (await asyncio.create_subprocess_exec("open", "-a", "Calculator")).wait()
    await asyncio.sleep(1.5)
    await live_calculator.reset()


async def _calc_check() -> bool:
    shown = await live_calculator.display()
    print(f"계산기 표시 {shown!r}")
    return shown == "7"


async def _textedit_reset() -> None:
    """문서를 열고 본문 입력 칸이 트리에 보일 때까지 기다린다 — 고정 대기만 하면 첫 관찰에 창이 없을 때가 있었다."""
    agent_textedit.setup()
    for _ in range(40):
        try:
            snap = await MacCollector().snapshot(agent_textedit.APP)
            if any(n.raw_role == "AXTextArea" for n in snap.root.walk()):
                return
        except LookupError:
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError("텍스트 편집기 문서 창이 10초 안에 뜨지 않았다")


async def _calc_view(name: str) -> None:
    """보기 메뉴에서 계산기 모드 선택(기본 · 공학용) — 메뉴 항목 AXPress는 닫힌 메뉴에서도 된다."""
    col = MacCollector()
    snap = await col.snapshot(live_calculator.APP)
    await col.click(next(n.id for n in snap.root.walk() if n.role is Role.MENUITEM and n.name == name))
    await asyncio.sleep(0.5)


async def _scicalc_reset() -> None:
    await _calc_reset()
    await _calc_view("기본")  # 지난 run이 공학용으로 남겼어도 기본에서 시작
    await live_calculator.reset()


async def _scicalc_check() -> bool:
    try:
        shown = (await live_calculator.display()).replace(",", "")
        snap = await MacCollector().snapshot(live_calculator.APP)
        buttons = sum(1 for n in snap.root.walk() if n.role is Role.BUTTON)
        print(f"계산기 표시 {shown!r} · 버튼 {buttons}개(공학용이면 30개 넘음)")
        return shown == "1036" and buttons > 30
    finally:
        await _calc_view("기본")  # 사용자 계산기를 기본 모드로 돌려 둔다


async def _twoapp_reset() -> None:
    await _calc_reset()
    await _textedit_reset()  # 텍스트 편집기가 마지막으로 열려 맨 앞


async def _twoapp_check() -> bool:
    try:
        shown = (await live_calculator.display()).replace(",", "")
        text, used, _ = await agent_textedit.read_doc()
        ok = (
            shown == "4480"
            and text.replace(",", "") == "결과: 4480"
            and bool(used)
            and all("Bold" in f for f, _ in used)
        )
        print(f"계산기 {shown!r} · 문서 {text!r} · 글자 서식 {sorted(used)} → {'성공' if ok else '실패'}")
        return ok
    finally:
        agent_textedit.quit_app()


WIZARD = {"name": "테스트", "phone": "010-1234-5678", "addr": "서울시 테스트로 1", "zip": "12345",
          "qty": 3, "wrap": "선물", "coupon": True, "total": 34200}  # (12,000 × 3 + 2,000) × 0.9  # fmt: skip


URL = [""]  # main이 띄운 로컬 페이지 서버 주소


async def _wizard_reset() -> None:
    ORDERS.clear()
    await m9.cft_open(f"{URL[0]}/wizard.html")  # 두 모드 모두 하네스 전용 Chrome for Testing(새 프로필)에서 시작


async def _seats_reset() -> None:
    ORDERS.clear()
    await m9.cft_open(f"{URL[0]}/seats.html")


async def _wizard_check() -> bool:
    try:
        got = ORDERS[-1] if ORDERS else None
        ok = got is not None and all(got.get(k) == v for k, v in WIZARD.items())
        print(f"주문 {got} → {'성공' if ok else '실패'}")
        return ok
    finally:
        m9.cft_close()


SEATS = {"seats": ["C4", "C5"], "name": "테스트", "qty": 2}


async def _seats_check() -> bool:
    try:
        got = ORDERS[-1] if ORDERS else None
        ok = got == SEATS
        print(f"예매 {got} → {'성공' if ok else '실패'}")
        return ok
    finally:
        m9.cft_close()


TASKS = {
    "calculator": Task("macOS 계산기 앱에서 2 + 5 = 를 계산해.", _calc_reset, _calc_check),
    "textedit": Task(
        f"macOS 텍스트 편집기에 열려 있는 문서에 '{agent_textedit.TEXT}'를 입력하고, "
        "서체 크기 18 · 볼드체 · 중앙 정렬로 바꿔.",
        _textedit_reset,
        agent_textedit.check,
    ),
    # 긴 작업 (2026-11-03 사용자 선택) — 공학용 화면은 버튼이 많아 Filter 효과, 두 앱은 앱별 Diff 상태를 본다
    "scicalc": Task("macOS 계산기를 공학용 모드로 바꾼 뒤 2¹⁰ + √144 를 계산해.", _scicalc_reset, _scicalc_check),
    "twoapp": Task(
        "macOS 계산기로 (128 × 37) − 256 을 계산하고, 그 결과를 텍스트 편집기에 열려 있는 문서에 "
        "'결과: <값>' 형식으로 적은 뒤 볼드체로 바꿔.",
        _twoapp_reset,
        _twoapp_check,
    ),
    # M9(2026-11-21): 웹 페이지는 하네스가 Chrome for Testing(새 프로필)에 미리 연다 — 두 모드 같은 시작 상태
    "webwizard": Task(
        "Google Chrome for Testing 창에 열려 있는 주문서 페이지를 작성하고 주문을 완료해: 받는 분 테스트, "
        "연락처 010-1234-5678, 주소 서울시 테스트로 1, 우편번호 12345, 수량 3개, 선물 포장, 쿠폰 CUOP10 적용, "
        "구매 조건 동의.",
        _wizard_reset,
        _wizard_check,
    ),
    # web Partial Vision (2026-11-04 사용자 선택: 혼합형) — 좌석은 캔버스에만, 나머지는 일반 폼
    "webcanvas": Task(
        "Google Chrome for Testing 창에 열려 있는 좌석 예매 페이지에서 C열 4번 · 5번 좌석을 고르고, "
        "예매자 이름 테스트, 인원 2명으로 예매를 완료해.",
        _seats_reset,
        _seats_check,
    ),
    # M9 앱 종류 확장 (BRIEF §3-5)
    "dictionary": Task(f"macOS 사전 앱에서 '{m9.WORD}'를 찾아 뜻이 보이게 해.", m9.dict_reset, m9.dict_check),
    "preview": Task(
        "미리보기 앱에 열려 있는 이미지 rotate.png를 왼쪽으로 90도 회전하고 저장해.", m9.preview_reset, m9.preview_check
    ),
    "vscode": Task(
        "Visual Studio Code에 열려 있는 note.txt 파일에 hello cuop 이라고 입력하고 저장해.",
        m9.code_reset,
        m9.code_check,
    ),
    "activity": Task(
        "macOS 활성 상태 보기에서 '메모리' 탭으로 바꾸고, 프로세스 목록을 'Finder'로 검색해.",
        m9.activity_reset,
        m9.activity_check,
    ),
    "clock": Task("macOS 시계 앱의 스톱워치를 시작했다가 몇 초 뒤에 정지해.", m9.clock_reset, m9.clock_check),
    "battery": Task(
        "macOS 메뉴 막대의 배터리 메뉴(제어 센터)를 확인해서 지금 배터리 잔량이 몇 %인지 알려줘.",
        m9.battery_reset,
        m9.battery_check,
    ),
    # Office 시험(OFFICE_BRIEF.md, 2026-11-22): 새 일회용 문서를 백그라운드로 열고, 판정은 저장한 파일을 파싱
    "word": Task(
        "Microsoft Word에 열려 있는 cuop-test 문서의 맨 앞에 '검토: '라고 입력한 뒤, 문서 전체를 굵게 해.",
        office.word_reset,
        office.word_check,
    ),
    "excel": Task(
        "Microsoft Excel에 열려 있는 cuop-test 통합 문서에서 B2 셀 값을 10으로 바꾸고, "
        "B6 셀(=SUM(B2:B5))의 계산 결과가 얼마인지 알려줘.",
        office.excel_reset,
        office.excel_check,
    ),
    "powerpoint": Task(
        "Microsoft PowerPoint에 열려 있는 cuop-test 프레젠테이션에 새 슬라이드를 하나 추가하고, "
        "그 슬라이드의 제목을 '2분기 실적'으로 입력해.",
        office.powerpoint_reset,
        office.powerpoint_check,
    ),
}
OFFICE = ("word", "excel", "powerpoint")  # 직접 고를 때만(--tasks) — 사용자 Office가 떠 있으면 run 제외
# 작업마다 서버가 읽고 조작해도 되는 앱(번들 ID) — 그 밖은 두 서버 모두 거부한다(CUOP_ALLOW_APPS, D13)
_CALC, _TE = "com.apple.calculator", "com.apple.TextEdit"
APPS: dict[str, tuple[str, ...]] = {
    "calculator": (_CALC,), "scicalc": (_CALC,), "textedit": (_TE,), "twoapp": (_CALC, _TE),
    "webwizard": (m9.CFT_ID,), "webcanvas": (m9.CFT_ID,), "dictionary": (m9.DICT,), "preview": (m9.PREVIEW,),
    "vscode": (m9.CODE,), "activity": (m9.ACTIVITY,), "clock": (m9.CLOCK,), "battery": ("com.apple.controlcenter",),
    "word": (office.WORD,), "excel": (office.EXCEL,), "powerpoint": (office.PPT,),
}  # fmt: skip


def _command(prompt: str, config: Path) -> list[str]:
    return [
        "claude", "-p", prompt, "--model", MODEL, "--effort", EFFORT,
        "--tools", "", "--setting-sources", "", "--mcp-config", str(config), "--strict-mcp-config",
        "--allowedTools", "mcp__cuop", "--no-session-persistence", "--max-budget-usd", str(BUDGET_USD),
        "--output-format", "stream-json", "--verbose",
    ]  # fmt: skip


def _strip(value: Any) -> Any:
    """stream-json에서 base64 이미지를 크기 표시로 바꾼다."""
    if isinstance(value, dict):
        d: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        if d.get("type") == "base64" and "data" in d:
            return {**d, "data": f"<{len(d['data'])} base64 chars removed>"}
        return {k: _strip(v) for k, v in d.items()}
    if isinstance(value, list):
        return [_strip(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _server_steps(run_id: str, mode: str = "") -> list[dict[str, Any]]:
    path = LOGS / ("steps-cuop.jsonl" if mode.startswith("cuop") else "steps.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [r for r in rows if r["run_id"] == run_id]


def _cuop_server(run_id: str, home: Path, allow: str, jev: bool = False) -> dict[str, Any]:
    """새 desktop CLI의 MCP 서버(`cuop mcp --format text --toolset core`) — 관찰 = 텍스트 + Diff, 도구 24개(D14).
    상태 폴더는 run마다 새로(refmap · 안정 번호가 run 사이에 섞이지 않게) · run이 끝나면 지운다, HUD는 끈다."""
    return {
        "command": "uv",
        "args": ["run", "--project", str(ROOT), "cuop", "mcp", "--format", "text", "--toolset", "core"],
        "env": {"CUOP_HOME": str(home), "CUOP_OVERLAY": "0", "CUOP_STEP_LOG": str(LOGS / "steps-cuop.jsonl"),
                "CUOP_RUN_ID": run_id, "CUOP_ALLOW_APPS": allow, "CUOP_JEV": "1" if jev else "0"},
    }  # fmt: skip


def _events(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # 끊긴 마지막 줄 등
    return out


def _setup_failed(name: str, mode: str, rep: int, run_id: str, error: str) -> dict[str, Any]:
    return {"date": date.today().isoformat(), "task": name, "mode": mode, "rep": rep, "run_id": run_id,
            "model": None, "effort": EFFORT, "success": False, "excluded": True, "stop": None, "turns": 0,
            "tool_calls": 0, "input_total": 0, "cache_read": 0, "output": 0, "cost_usd": 0, "seconds": 0,
            "server_calls": 0, "server_errors": 0, "server_tokens": 0, "vision": 0, "error": error}  # fmt: skip


def run_one(name: str, mode: str, rep: int, url: str) -> dict[str, Any]:
    task, run_id = TASKS[name], uuid.uuid4().hex[:8]
    if mode == "cuop+jev":
        from cuop.optimizer import jev

        if not jev.has_key():  # 키가 없으면 플러그인이 조용히 꺼진다 — 'cuop+jev'라는 이름의 규칙 run이 되지 않게 제외
            return _setup_failed(name, mode, rep, run_id, "setup: no TYPESAFE_API_KEY")
    allow = ",".join(APPS.get(name, ()))
    home = Path(tempfile.mkdtemp(prefix="cuop-run-"))
    try:
        try:
            asyncio.run(task.reset())
        except (Exception, SystemExit) as e:  # 준비 실패(사용자 앱이 이미 떠 있음 등) = 비교 제외로 기록하고 다음 run
            return _setup_failed(name, mode, rep, run_id, f"setup: {type(e).__name__}: {str(e)[:120]}")
        return _run(task, name, mode, rep, url, run_id, allow, home)
    finally:
        office.close_all()  # Office는 '저장 안 함'으로 먼저 닫는다 — 강제 종료하면 다음 run에 복구 창이 뜬다
        m9.cleanup_all()  # 이 run이 띄운 앱 · 임시 폴더(판정이 이미 정리했으면 아무것도 안 함)
        shutil.rmtree(home, ignore_errors=True)  # refmap · 텍스트 뷰에 화면 글자가 있다 — run 뒤에 남기지 않는다


def _run(task: Task, name: str, mode: str, rep: int, url: str, run_id: str, allow: str, home: Path) -> dict[str, Any]:
    server = _cuop_server(run_id, home, allow, jev=mode == "cuop+jev") if mode.startswith("cuop") else {
        "command": "uv",
        "args": ["run", "--project", str(ROOT), "cuop-mcp", "--platform", "macos", "--mode", mode],
        "env": {"CUOP_LOG": str(LOGS / "steps.jsonl"), "CUOP_RUN_ID": run_id, "CUOP_ALLOW_APPS": allow},
    }  # fmt: skip  # 웹은 하네스가 연 Chrome for Testing을 OS 수집기로 본다(M9)
    raw = LOGS / "runs" / f"{run_id}.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    t0, error, stdout = time.monotonic(), None, ""
    with tempfile.TemporaryDirectory() as cwd:
        config = Path(cwd) / "cuop.json"
        config.write_text(json.dumps({"mcpServers": {"cuop": server}}), encoding="utf-8")
        try:
            command = _command(task.prompt.replace("{url}", url), config)
            proc = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=TIMEOUT_S, check=False)  # fmt: skip
            stdout = proc.stdout
        except subprocess.TimeoutExpired as e:
            error = f"timeout {TIMEOUT_S}s"
            stdout = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    seconds = round(time.monotonic() - t0, 1)
    events = _events(stdout)
    # 화면 이미지는 디스크에 쓰기 전에 메모리에서 뺀다 — 원본 출력을 그대로 쓰지 않는다(Codex M9 리뷰 #2)
    raw.write_text("".join(json.dumps(_strip(e), ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    m9.ANSWER[0] = next((str(e.get("result") or "") for e in reversed(events) if e.get("type") == "result"), "")
    try:
        success = asyncio.run(task.check())
    except Exception as e:  # 판정 불가(앱이 사라짐 등)는 실패로 기록하고 다음 run으로
        success, error = False, error or f"check: {type(e).__name__}: {e}"

    none: dict[str, Any] = {}
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), none)
    result = next((e for e in reversed(events) if e.get("type") == "result"), none)
    usage: dict[str, int] = result.get("usage") or {}
    blocks: list[dict[str, Any]] = [
        b for e in events if e.get("type") == "assistant" for b in e["message"].get("content", [])
    ]
    steps = _server_steps(run_id, mode)
    fresh, created, cached = (usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens"))  # fmt: skip
    return {
        "date": date.today().isoformat(), "task": name, "mode": mode, "rep": rep, "run_id": run_id,
        "model": init.get("model"), "effort": EFFORT, "success": success,
        "mcp": [s.get("status") for s in init.get("mcp_servers", [])],
        "stop": result.get("subtype"), "turns": result.get("num_turns"),
        "tool_calls": sum(1 for b in blocks if b.get("type") == "tool_use"),
        "input_total": fresh + created + cached, "cache_read": cached, "output": usage.get("output_tokens", 0),
        "cost_usd": result.get("total_cost_usd"), "seconds": seconds,
        "server_calls": len(steps), "server_errors": sum(1 for s in steps if s["error"]),
        "server_tokens": sum(s["out_tokens"] + s["image_tokens"] for s in steps),
        "vision": sum(1 for s in steps if s["image_tokens"]), "error": error,
    }  # fmt: skip


def row(r: dict[str, Any]) -> str:
    share = f"{r['cache_read'] / r['input_total']:.0%}" if r["input_total"] else "—"
    cells = [
        r["date"], r["task"], r["run_id"], r["mode"], "O" if r["success"] else "X", r["turns"], r["tool_calls"],
        f"{r['input_total']:,}", share, f"{r['output']:,}", f"{r['cost_usd'] or 0:.3f}",
        f"{r['server_tokens']:,}", r["server_errors"], r["vision"], r["seconds"], "",
    ]  # fmt: skip
    return "| " + " | ".join(map(str, cells)) + " |"


def main() -> None:
    p = argparse.ArgumentParser(description="헤드리스 에이전트 하네스")
    p.add_argument("--tasks", nargs="+", choices=sorted(TASKS), default=sorted(set(TASKS) - set(OFFICE)))
    # 비교군 ①②③④ + 새 desktop CLI(MCP) + Jev 플러그인(JEV_BRIEF: 키는 서버가 ~/.hermes/.env에서 직접 읽는다)
    modes = ["screenshot", "full", "compact", "proposed", "cuop", "cuop+jev"]
    p.add_argument("--modes", nargs="+", choices=modes, default=modes)
    p.add_argument("--repeat", type=int, default=1)
    a = p.parse_args()
    rows: list[str] = []
    url = URL[0] = _serve()  # 웹 작업용 로컬 페이지 서버 (다른 작업은 쓰지 않는다)
    for rep in range(1, a.repeat + 1):  # 반복 안에서 모드를 번갈아 → 시간에 따른 변화가 한 모드에 몰리지 않게
        for name in a.tasks:
            for mode in a.modes:
                if name in OFFICE and mode not in (
                    "cuop",
                    "cuop+jev",
                    "screenshot",
                ):  # 옛 트리 모드엔 headed 입력이 없다
                    print(f"건너뜀: {name} × {mode} (Office 작업은 cuop · screenshot만)", flush=True)
                    continue
                r = run_one(name, mode, rep, url)
                with (LOGS / "runs.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                rows.append(row(r))
                print(rows[-1], flush=True)
    print("\n".join(rows))


if __name__ == "__main__":
    main()
