# pyright: basic
"""MCP `initialize.instructions`가 모델 컨텍스트에 들어가는지 1턴으로 확인한다(INSTRUCTIONS_BRIEF 1단계).

도구를 부르지 말고 cuop 서버 지시문의 첫 문장을 그대로 인용하게 한다 — 그 문장은 도구 설명에 없다.
앱을 띄우거나 조작하지 않는다(도구를 부르지 말라고 하고, 답만 본다).

uv run python benchmarks/instructions_probe.py codex|claude
"""

import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import compare_codex_cu as cmp
import run as harness

from cuop.desktop.mcp import INSTRUCTIONS

PROMPT = ("Do not call any tool. Quote verbatim the first sentence of the instructions that the MCP server named "
          "'cuop' gave you. If you received no such instructions, answer exactly NONE.")  # fmt: skip
FIRST = INSTRUCTIONS.split(". ", 1)[0]


def codex() -> str:
    cmp.ALLOW = ("invalid.none",)  # 비교 하네스의 허용 앱(CFT · Word)을 쓰지 않는다 — 도구가 불려도 어떤 앱도 못 읽게
    home = Path(tempfile.mkdtemp(prefix="cuop-probe-"))
    try:
        events, error = cmp._codex(cmp.command("cuop", uuid.uuid4().hex[:8], home), PROMPT, [])
    finally:
        shutil.rmtree(home, ignore_errors=True)
    msgs = [e["item"].get("text", "") for e in events if e.get("type") == "item.completed"
            and e["item"].get("type") == "agent_message"]  # fmt: skip
    return (msgs[-1] if msgs else "") + (f" [error: {error}]" if error else "")


def claude() -> str:
    home = Path(tempfile.mkdtemp(prefix="cuop-probe-"))
    try:
        with tempfile.TemporaryDirectory() as cwd:
            cfg = Path(cwd) / "cuop.json"
            server = harness._cuop_server(uuid.uuid4().hex[:8], home, "invalid.none")
            cfg.write_text(json.dumps({"mcpServers": {"cuop": server}}), encoding="utf-8")
            p = subprocess.run(harness._command(PROMPT, cfg), cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=300, check=False)  # fmt: skip
    finally:
        shutil.rmtree(home, ignore_errors=True)
    res = next((e for e in harness._events(p.stdout)[::-1] if e.get("type") == "result"), {})
    return str(res.get("result") or p.stderr[-300:])


if __name__ == "__main__":
    answer = {"codex": codex, "claude": claude}[sys.argv[1]]()
    # 답이 정확히 NONE일 때만 '전달 안 됨' — 연결 실패 · 시간 초과(그 밖의 답)는 판정하지 않는다
    verdict = True if FIRST in answer else (False if answer.strip() == "NONE" else None)
    print(json.dumps({"client": sys.argv[1], "delivered": verdict, "first": FIRST, "answer": answer[:400]},
                     ensure_ascii=False))  # fmt: skip
