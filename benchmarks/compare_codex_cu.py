# pyright: basic
"""복합 작업 비교 — Codex Computer Use vs CUOP (docs/parity/CODEX_CU_BRIEF.md).

같은 에이전트(`codex exec -m gpt-6.2-sol`, effort medium, --json)에 같은 프롬프트, 데스크톱 도구(MCP)만 다르다:
  cu   = Codex 번들 computer-use MCP(SkyComputerUseClient mcp) — 사용자 ~/.codex/config.toml은 읽지도 고치지도 않는다
  cuop = `cuop mcp --format text --toolset core`(CUOP_ALLOW_APPS)
두 쪽 모두 셸 · web_search · 브라우저 · 플러그인 · 훅을 끄고(--ignore-user-config · --ephemeral · read-only 샌드박스),
하네스가 띄운 새 프로필 Chrome for Testing과 빈 일회용 Word 문서(logs/compare/report-<run>.docx)만 쓴다.

uv run python benchmarks/compare_codex_cu.py cu|cuop [--rep N]   # run 1회 → logs/compare/runs.jsonl · 표 한 줄
uv run python benchmarks/compare_codex_cu.py grade <docx>         # 판정만
uv run python benchmarks/compare_codex_cu.py table                # runs.jsonl → 마크다운 표(run별 · 평균)
"""

import asyncio
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import tasks_m9 as m9
import tasks_office as office
from docx import Document

ROOT = m9.ROOT
OUT = ROOT / "logs" / "compare"
MODEL, EFFORT, TIMEOUT_S = "gpt-6.2-sol", "medium", 1800
START = "https://ko.wikipedia.org/"
TITLE = "제임스 웹 우주 망원경 요약"
CU_BIN = (Path.home() / ".codex/computer-use/Codex Computer Use.app/Contents/SharedSupport"
          / "SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient")  # fmt: skip
PANEL = "com.apple.appkit.xpc.openAndSavePanelService"  # 파일 저장 · 열기 대화상자(샌드박스 앱의 패널은 이 프로세스)
ALLOW = (m9.CFT_ID, office.WORD, PANEL)
CU_APPS = ("google chrome for testing", "chrome for testing", "microsoft word", "word", PANEL, m9.CFT_ID, office.WORD)
# 앱 인자는 이름 · 번들 ID · .app 경로 — 경로는 끝 이름으로 본다
PROMPT = (
    "브라우저에서 '제임스 웹 우주 망원경'을 검색해 위키백과 문서를 찾고, 열린 Word 문서에 다음을 작성해 저장하라: "
    f"① 제목 '{TITLE}' ② 발사일 · 발사체 · 주경 지름 · 위치(라그랑주점)를 담은 3문단 이상 "
    "③ 위키백과 문서의 망원경 이미지 1장(이미지를 내려받아 Word에 삽입) ④ 마지막 줄에 출처 URL. "
    "Word 밖에서 파일을 만들거나 셸을 쓰지 마라.\n"
    "조건: 브라우저는 이미 열려 있는 'Google Chrome for Testing'(위키백과 대문), Word 문서는 이미 열려 있는 "
    "'{doc}'이다. 이 두 앱(과 그 앱의 파일 대화상자)만 쓰고 다른 앱 · 다른 문서는 열지 마라. "
    "이미지는 브라우저로 '{dl}' 폴더에 내려받아라. 끝나면 Word에서 저장하라(.docx 형식 그대로)."
)
# Codex CU의 앱 사용 허가: CU는 처음 쓰는 앱마다 MCP elicitation으로 사람에게 묻는다. `codex exec`에는 답할 사람이 없어
# never면 거부(시험 run ea286cd1), auto_review면 취소(bf1e3043), 세션 파일을 미리 써도 다시 묻고(1175f842),
# 중간 중계는 CU가 부모 프로세스 인증으로 거부한다(5a9c05b9) → 두 모드 모두 `codex app-server`를 직접 클라이언트로
# 돌리고, 데스크톱 앱이 사람의 '허용'을 전하는 그 요청에 하네스가 띄운 두 앱일 때만 '허용'으로 답한다(consent, D16).
APPROVAL = {"granular": {"sandbox_approval": False, "rules": False, "skill_approval": False,
                         "request_permissions": False, "mcp_elicitations": True}}  # fmt: skip
USER_MCP = ("node_repl", "ai-memory")  # 사용자 config.toml에 있는 다른 MCP — 이 run에서는 끈다
CU_NAMES = ("Google Chrome for Testing", "Microsoft Word")  # 허가 요청 문구의 앱 이름
# gpt-6.2-sol API 단가 $/1M(입력 · 캐시 입력 · 출력) — 제3자 정리(eesel.ai · aipricing.guru, 2026-11-22 검색),
# OpenAI 공식 가격표는 직접 확인하지 못함. 실제 실행은 ChatGPT 구독(Codex 한도)이라 이 값은 환산용.
PRICE = (2.0, 0.10, 10.0)
DISABLE = ("shell_tool", "browser_use", "browser_use_external", "in_app_browser", "image_generation", "apps",
           "plugins", "hooks", "multi_agent", "tool_suggest", "goals")  # fmt: skip
# 판정: 사실 4개(발사일 · 발사체 · 주경 지름 · 위치) — 한국어 · 영어 표기
FACTS = {
    "launch": re.compile(
        r"2021\s*년\s*12\s*월\s*25\s*일|2021\s*[-./]\s*12\s*[-./]\s*25|December\s+25,?\s+2021"
        r"|25\s+December\s+2021",
        re.I,
    ),  # fmt: skip
    "ariane5": re.compile(r"아리안\s*5|Ariane\s*5", re.I),
    "mirror": re.compile(r"6[.,]5\s*(m(?![a-z])|미터|meter|metre)", re.I),  # "6.5 m로" — 한글은 \w라 \b가 없다
    "l2": re.compile(r"(?<![A-Za-z])L\s?2(?!\d)|L₂|제\s*2\s*라그랑주|두\s*번째\s*라그랑주", re.I),
}


# --- 판정 (저장된 docx 파싱) ---
def grade(path: Path, made: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"saved": False, "title": False, "paras": 0, "facts": [], "images": 0, "source": False}
    if not path.exists() or (made is not None and path.stat().st_mtime == made):
        return {**out, "score": 0, "ok": False}
    d = Document(str(path))
    paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    xml = d.element.xml
    out["saved"] = True
    out["title"] = bool(paras) and paras[0] == TITLE
    out["paras"] = sum(1 for p in paras[1:] if "wikipedia.org" not in p and len(p) >= 20)
    text = "\n".join(paras)
    out["facts"] = [k for k, rx in FACTS.items() if rx.search(text)]
    out["images"] = len(re.findall(r"<a:blip\b", xml)) + len(re.findall(r"<v:imagedata\b", xml))
    out["source"] = bool(paras) and "wikipedia.org" in paras[-1]
    checks = [out["saved"], out["title"], out["paras"] >= 3, len(out["facts"]) >= 3, out["images"] >= 1,
              out["source"]]  # fmt: skip
    return {**out, "score": sum(checks), "ok": all(checks)}


# --- 이벤트 → 숫자 ---
def _strip(v: Any) -> Any:
    """이미지 base64 · 긴 data URL을 크기 표시로 — 원본 로그에 화면 이미지를 남기지 않는다."""
    if isinstance(v, dict):
        return {k: (f"<{len(x)} chars removed>" if isinstance(x, str) and len(x) > 2000 and k in ("data", "image_url",
                    "url", "image") else _strip(x)) for k, x in v.items()}  # fmt: skip
    if isinstance(v, list):
        return [_strip(x) for x in v]
    if isinstance(v, str) and v.startswith("data:image/") and len(v) > 2000:
        return f"<data url {len(v)} chars removed>"
    return v


def _images(v: Any) -> int:
    if isinstance(v, dict):
        own = 1 if v.get("type") in ("image", "input_image") else 0
        return own + sum(_images(x) for x in v.values())
    if isinstance(v, list):
        return sum(_images(x) for x in v)
    return 0


def summarize(events: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    use = Counter()
    for e in events:
        if e.get("type") == "turn.completed":
            use.update({k: int(v or 0) for k, v in (e.get("usage") or {}).items()})
    items = [e["item"] for e in events if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)]
    kinds = Counter(str(i.get("type")) for i in items)
    mcp = [i for i in items if i.get("type") == "mcp_tool_call"]
    tools = Counter(f"{i.get('server')}.{i.get('tool')}" for i in mcp)
    want = "computer-use" if mode == "cu" else "cuop"
    bad = [f"{k}×{n}" for k, n in kinds.items() if k in ("command_execution", "file_change", "web_search")]
    bad += sorted({f"server:{i.get('server')}" for i in mcp if i.get("server") != want})
    bad += [f"request:{e.get('method')}" for e in events if e.get("type") == "server_request"]  # 명령 · 파일 승인 요청
    bad += sorted({f"tool:{i.get('tool')}" for i in mcp if i.get("tool") in ("desktop_launch", "desktop_close_app")
                   or (i.get("tool") == "desktop_screenshot" and ((i.get("arguments") or {}).get("output_path")
                       or (i.get("arguments") or {}).get("path")))})  # fmt: skip
    if mode == "cu":  # Codex CU는 앱 이름을 인자로 받는다 — 허용 앱 밖이면 위반
        apps = {Path(str((i.get("arguments") or {}).get("app") or "").rstrip("/")).name.lower().removesuffix(".app")
                for i in mcp} - {""}  # fmt: skip
        bad += sorted(f"app:{a}" for a in apps if a not in {x.lower() for x in CU_APPS})
    return {
        "input": use["input_tokens"], "cached": use["cached_input_tokens"], "output": use["output_tokens"],
        "reasoning": use["reasoning_output_tokens"], "total": use["input_tokens"] + use["output_tokens"],
        "cost": round(((use["input_tokens"] - use["cached_input_tokens"]) * PRICE[0] + use["cached_input_tokens"]
                       * PRICE[1] + use["output_tokens"] * PRICE[2]) / 1e6, 4),
        "model_calls": sum(int(e.get("model_calls") or 0) for e in events if e.get("type") == "turn.completed"),
        "tool_calls": len(mcp), "tool_errors": sum(1 for i in mcp if i.get("error") or i.get("status") == "failed"),
        "images": sum(_images(i.get("result")) for i in mcp), "items": dict(kinds), "tools": dict(tools),
        "violations": bad,
    }  # fmt: skip


# --- run ---
def _toml(v: Any) -> str:
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}={_toml(x)}" for k, x in v.items()) + "}"
    return json.dumps(v, ensure_ascii=False)  # 문자열 · 배열은 JSON 표기가 TOML과 같다


def command(mode: str, run_id: str, home: Path) -> list[str]:
    """`codex app-server`(stdio) — 사용자 config.toml의 플러그인 · 다른 MCP · 알림은 -c로 끈다(파일은 고치지 않음)."""
    cmd = [
        "codex",
        "app-server",
        "-c",
        'web_search="disabled"',
        "-c",
        "notify=[]",
        "-c",
        f"model_reasoning_effort={_toml(EFFORT)}",
    ]
    for f in DISABLE + (("computer_use",) if mode == "cuop" else ()):
        cmd += ["--disable", f]
    for other in USER_MCP:
        cmd += ["-c", f"mcp_servers.{other}.enabled=false"]
    if mode == "cu":
        server: dict[str, Any] = {
            "command": str(CU_BIN),
            "args": ["mcp"],
            "cwd": str(CU_BIN.parents[6]),
            "enabled": True,
        }
    else:
        server = {"command": "uv", "args": ["run", "--project", str(ROOT), "cuop", "mcp", "--format", "text",
                                             "--toolset", "core"],
                  "env": {"CUOP_HOME": str(home), "CUOP_OVERLAY": "0", "CUOP_RUN_ID": run_id,
                          "CUOP_STEP_LOG": str(OUT / "steps-cuop.jsonl"),
                          "CUOP_ALLOW_APPS": ",".join(ALLOW)}}  # fmt: skip
    name = "computer-use" if mode == "cu" else "cuop"
    server |= {"default_tools_approval_mode": "approve", "startup_timeout_sec": 60, "tool_timeout_sec": 180}
    for k, v in server.items():
        cmd += ["-c", f"mcp_servers.{name}.{k}={_toml(v)}"]
    return cmd


def consent(params: dict[str, Any], thread_id: str | None = None, turn_id: str | None = None) -> dict[str, Any]:
    """Codex CU의 앱 사용 허가("Allow ChatGPT to use <앱>?", 빈 form)에 사람 대신 답한다: 이 run의 스레드가 묻는
    하네스가 띄운 두 앱이면 그 한 번 '허용'(_meta.persist "always"는 보내지 않는다 — 다음 세션 · 사용자 설정에 남지
    않게, run 뒤 ~/.codex/computer-use/sessions에 새 파일 없음 확인), 아니면 거부."""
    m = re.fullmatch(r"Allow ChatGPT to use (.+)\?", str(params.get("message") or ""))
    form = params.get("mode") == "form" and params.get("requestedSchema") == {"type": "object", "properties": {}}
    if (params.get("serverName") != "computer-use" or m is None or m.group(1) not in CU_NAMES or not form
            or thread_id is None or turn_id is None
            or params.get("threadId") != thread_id or params.get("turnId") != turn_id):  # fmt: skip  # 확정된 이 턴만
        return {"action": "decline"}
    return {"action": "accept", "content": {}}


def _snake(t: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", t).lower()


def _usage_event(usage: dict[str, Any], calls: int, status: Any) -> dict[str, Any]:
    return {"type": "turn.completed", "status": status, "model_calls": calls, "usage": {
        "input_tokens": usage.get("inputTokens", 0), "cached_input_tokens": usage.get("cachedInputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "reasoning_output_tokens": usage.get("reasoningOutputTokens", 0)}}  # fmt: skip


def _codex(cmd: list[str], prompt: str, consents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """app-server에 스레드 하나 · 턴 하나를 보내고 끝까지 읽는다. 이벤트는 exec --json 모양(item.completed ·
    turn.completed usage)으로 바꿔 돌려준다 — 끝나지 못한 run도 받은 데까지의 usage를 남긴다. 이 스레드 밖의 알림은
    버린다. 승인 요청: 이 스레드의 elicitation만 consent(), 나머지는 모두 거부."""
    events: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    error: str | None = None
    done = False
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryFile("w+") as err:
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                             start_new_session=True)  # fmt: skip  # 시간 초과면 자식(MCP 서버)까지 그룹째 끊는다

        def kill() -> None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        assert p.stdin is not None and p.stdout is not None
        timer = threading.Timer(TIMEOUT_S, kill)
        timer.start()
        n = calls = 0
        tid: str | None = None
        turn_id: str | None = None

        def refuse(rid: Any, meth: Any) -> None:  # 승인 요청 거부(JSON-RPC 오류) + 기록
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000,  # type: ignore[union-attr]
                                      "message": "declined by harness"}}) + "\n")  # fmt: skip
            p.stdin.flush()  # type: ignore[union-attr]
            events.append({"type": "server_request", "method": meth})

        def send(method: str | None, params: Any = None, rid: Any = None, result: Any = None) -> int:
            nonlocal n
            if method is None:  # 서버 요청에 대한 답
                msg = {"jsonrpc": "2.0", "id": rid, "result": result}
            else:
                n += 1
                msg = {"jsonrpc": "2.0", "id": n, "method": method, "params": params}
            p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
            p.stdin.flush()  # type: ignore[union-attr]
            return n

        try:
            send("initialize", {"clientInfo": {"name": "cuop-compare", "version": "0"},
                               "capabilities": {"experimentalApi": True,  # granular 승인 정책
                                                "mcpServerOpenaiFormElicitation": True}})  # fmt: skip
            started = turn = None
            for raw in p.stdout:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                meth, rid = m.get("method"), m.get("id")
                params = m.get("params") or {}
                if meth is not None and tid is not None and params.get("threadId") not in (None, tid):
                    if rid is not None:  # 다른 스레드의 요청 — 거부
                        refuse(rid, meth)
                    continue
                if meth is None and rid == 1:  # initialize 응답
                    if "error" in m:
                        error = f"initialize: {str(m['error'])[:160]}"
                        break
                    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
                    started = send("thread/start", {"model": MODEL, "cwd": cwd, "sandbox": "read-only",
                                                    "approvalPolicy": APPROVAL, "ephemeral": True})  # fmt: skip
                elif meth is None and rid == started:
                    if "error" in m:
                        error = f"thread/start: {str(m['error'])[:160]}"
                        break
                    tid = m["result"]["thread"]["id"]
                    events.append({"type": "thread.started", "thread_id": tid})
                    turn = send("turn/start", {"threadId": tid, "effort": EFFORT,
                                               "input": [{"type": "text", "text": prompt}]})  # fmt: skip
                elif meth is None and rid == turn:
                    if "error" in m:
                        error = f"turn/start: {str(m['error'])[:160]}"
                        break
                    turn_id = ((m.get("result") or {}).get("turn") or {}).get("id")
                elif meth == "mcpServer/elicitation/request" and rid is not None:
                    res = consent(params, tid, turn_id)
                    consents.append({"params": params, "answer": res})
                    send(None, rid=rid, result=res)
                elif meth is not None and rid is not None:  # 명령 · 파일 · 권한 승인 등 — 모두 거부
                    refuse(rid, meth)
                elif meth == "item/completed":
                    item = dict(params.get("item") or {})
                    item["type"] = _snake(str(item.get("type")))
                    events.append({"type": "item.completed", "item": item})
                elif meth == "thread/tokenUsage/updated":  # 모델 응답마다 한 번(누적 total)
                    usage = (params.get("tokenUsage") or {}).get("total") or usage
                    calls += 1
                elif (
                    meth == "turn/completed" and turn_id is not None and (params.get("turn") or {}).get("id") == turn_id
                ):
                    t = params.get("turn") or {}
                    if t.get("error") or t.get("status") != "completed":
                        error = f"turn {t.get('status')}: {str(t.get('error'))[:160]}"
                    events.append(_usage_event(usage, calls, t.get("status")))
                    done = True
                    break
                elif meth == "error":
                    events.append({"type": "error", "params": params})
        finally:
            timer.cancel()
            p.stdin.close()
            p.stdout.close()  # 더 읽지 않는다 — 남은 출력에 막혀 종료를 기다리지 않게
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                kill()
                p.wait()
        if not done:
            events.append(_usage_event(usage, calls, "incomplete"))  # 받은 데까지 — 평균에서 빼지 않고 실패로 센다
            err.seek(0)
            killed = p.returncode in (-9, -signal.SIGKILL)
            error = error or (f"timeout {TIMEOUT_S}s" if killed else f"no turn/completed: {err.read().strip()[-200:]}")
    return events, error


def make_doc(path: Path) -> Path:
    """빈 일회용 문서(표식 있음) — 새 이름이라 있는 파일을 지우지 않는다."""
    OUT.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"{path} 가 이미 있다 — run 제외")
    fd, name = tempfile.mkstemp(dir=OUT, prefix=".report-", suffix=".docx")
    os.close(fd)
    d = Document()
    d.core_properties.subject = office.MARK
    d.save(name)
    os.replace(name, path)
    office._made[path] = path.stat().st_mtime
    return path


def run_one(mode: str, rep: int) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:8]
    doc, dl = OUT / f"report-{run_id}.docx", OUT / f"dl-{run_id}"
    home = Path(tempfile.mkdtemp(prefix="cuop-cmp-"))
    made_dl = False
    row: dict[str, Any] = {"date": datetime.now().isoformat(timespec="minutes"), "mode": mode, "rep": rep,
                           "run_id": run_id, "model": MODEL, "effort": EFFORT}  # fmt: skip
    try:
        try:
            dl.mkdir(parents=True)  # 이미 있으면 예외 — 아래 정리는 이 run이 만든 폴더만 지운다
            made_dl = True
            prefs = {"download": {"default_directory": str(dl), "prompt_for_download": True},
                     "savefile": {"default_directory": str(dl)}}  # fmt: skip
            asyncio.run(office.open_doc("word", lambda: make_doc(doc)))
            asyncio.run(m9.cft_open(START, prefs))
        except (Exception, SystemExit) as e:  # 사용자 Word · CFT가 떠 있음 등 → 비교 제외
            return {**row, "valid": False, "success": False, "error": f"setup: {type(e).__name__}: {str(e)[:160]}"}
        prompt = PROMPT.replace("{doc}", doc.name).replace("{dl}", str(dl))
        raw = OUT / "runs" / f"{run_id}.jsonl"
        raw.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        consents: list[dict[str, Any]] = []
        events, error = _codex(command(mode, run_id, home), prompt, consents)
        row["consents"] = [c["answer"] for c in consents]
        (OUT / "runs" / f"{run_id}-consent.json").write_text(json.dumps(consents, ensure_ascii=False, indent=1))
        seconds = round(time.monotonic() - t0, 1)
        raw.write_text("".join(json.dumps(_strip(e), ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
        g = grade(doc, office._made.get(doc))
        s = summarize(events, mode)
        return {**row, **s, "valid": not s["violations"], "success": g["ok"], "grade": g, "seconds": seconds,
                "error": error}  # fmt: skip
    finally:
        office.close_all()  # Word는 '저장 안 함'으로 닫는다(판정은 이미 끝남)
        m9.cleanup_all()  # CFT · 임시 프로필
        shutil.rmtree(home, ignore_errors=True)
        if made_dl:
            shutil.rmtree(dl, ignore_errors=True)  # 내려받은 이미지(위키백과)는 문서 안에 있다


def line(r: dict[str, Any]) -> str:
    g = r.get("grade") or {}
    cells = [r["run_id"], r["mode"], "O" if r.get("success") else "X", f"{g.get('score', 0)}/6",
             "" if r.get("valid", True) else "무효", f"{r.get('total', 0):,}", f"{r.get('input', 0):,}",
             f"{r.get('cached', 0):,}", f"{r.get('output', 0):,}", f"{r.get('reasoning', 0):,}",
             f"{r.get('cost', 0):.2f}", r.get("model_calls", 0), r.get("tool_calls", 0), r.get("images", 0),
             r.get("seconds", 0), (r.get("error") or "")[:60]]  # fmt: skip
    return "| " + " | ".join(map(str, cells)) + " |"


HEAD = ("| run | 모드 | 성공 | 점수 | 유효 | 토큰 합 | 입력 | 캐시 입력 | 출력 | 추론 | API 환산 $ | 모델 호출 "
        "| 도구 호출 | 이미지 | 시간 s | 오류 |\n|" + "---|" * 16)  # fmt: skip
AVG = ("total", "input", "cached", "output", "cost", "model_calls", "tool_calls", "images", "seconds")


def table(rows: list[dict[str, Any]]) -> str:
    out = [HEAD, *map(line, rows), "", "| 모드 | 성공 | " + " | ".join(f"{k}(평균)" for k in AVG) + " |",
           "|" + "---|" * (len(AVG) + 2)]  # fmt: skip
    for m in ("cu", "cuop"):
        rs = [r for r in rows if r["mode"] == m and r.get("valid") and "setup" not in (r.get("error") or "")]
        if rs:
            avg = [statistics.mean(float(r.get(k) or 0) for r in rs) for k in AVG]
            cells = [f"{v:.2f}" if k == "cost" else f"{v:,.0f}" for k, v in zip(AVG, avg, strict=True)]
            out.append(f"| {m} | {sum(bool(r['success']) for r in rs)}/{len(rs)} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["grade"]:
        print(json.dumps(grade(Path(args[1])), ensure_ascii=False))
        return
    log = OUT / "runs.jsonl"
    if args[:1] == ["table"]:
        skip = {a.removeprefix("--exclude=") for a in args if a.startswith("--exclude=")}
        rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
        print(table([r for r in rows if r["run_id"] not in skip and r.get("trial") is not True]))
        return
    mode = args[0]
    assert mode in ("cu", "cuop"), "mode = cu | cuop"
    rep = int(args[args.index("--rep") + 1]) if "--rep" in args else 0
    r = run_one(mode, rep)
    if "--trial" in args:
        r["trial"] = True
    OUT.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(HEAD)
    print(line(r))
    print(json.dumps({k: r.get(k) for k in ("grade", "items", "tools", "violations")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
