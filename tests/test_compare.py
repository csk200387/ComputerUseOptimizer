"""복합 작업 비교 하네스(benchmarks/compare_codex_cu.py) — 판정 · 이벤트 요약 · 명령 구성. GUI 없이."""

import importlib
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
C: Any = importlib.import_module("compare_codex_cu")
M9: Any = importlib.import_module("tasks_m9")


def _doc(tmp: Path, paras: list[str], picture: bool) -> Path:
    from docx import Document

    d = Document()
    for p in paras:
        d.add_paragraph(p)
    if picture:
        png = tmp / "p.png"
        M9._png(png)
        d.add_picture(str(png))
    path = tmp / "r.docx"
    d.save(str(path))
    return path


BODY = [
    "제임스 웹 우주 망원경은 2021년 12월 25일에 발사된 적외선 우주 망원경이다.",
    "발사체는 아리안 5 로켓이며 프랑스령 기아나에서 발사되었다.",
    "주경 지름은 6.5 m로 금도금 베릴륨 거울 18개로 이루어져 있다.",
    "망원경은 태양-지구 L2 라그랑주점 근처 궤도에 있다.",
]


def test_grade_full_and_partial(tmp_path: Path) -> None:
    src = "출처: https://ko.wikipedia.org/wiki/제임스_웹_우주_망원경"
    g = C.grade(_doc(tmp_path, [C.TITLE, *BODY, src], picture=True))
    assert g["ok"] and g["score"] == 6 and g["images"] == 1 and set(g["facts"]) == {"launch", "ariane5", "mirror", "l2"}
    g = C.grade(_doc(tmp_path, ["다른 제목", *BODY[:2], src], picture=False))
    assert not g["ok"] and not g["title"] and g["paras"] == 2 and g["images"] == 0 and g["source"]
    p = _doc(tmp_path, [C.TITLE], picture=False)
    assert C.grade(p, made=p.stat().st_mtime)["score"] == 0  # 만든 뒤 저장 안 됨


def test_summarize_usage_and_violations() -> None:
    ev = [
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 10,
                "reasoning_output_tokens": 4,
            },
        },  # fmt: skip
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "computer-use",
                "tool": "get_app_state",
                "arguments": {"app": "Microsoft Word"},
                "result": {"content": [{"type": "text"}, {"type": "image"}]},
            },
        },  # fmt: skip
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "computer-use",
                "tool": "click",
                "arguments": {"app": "Finder"},
            },
        },  # fmt: skip
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
    ]
    s = C.summarize(ev, "cu")
    assert (s["total"], s["cached"], s["tool_calls"], s["images"]) == (110, 60, 2, 1)
    assert s["violations"] == ["command_execution×1", "app:finder"]
    assert C.summarize(ev[:2], "cuop")["violations"] == ["server:computer-use"]


def test_command_overrides_parse_as_toml(tmp_path: Path) -> None:
    for mode in ("cu", "cuop"):
        cmd = C.command(mode, "abcd1234", tmp_path)
        assert cmd[:2] == ["codex", "app-server"] and "shell_tool" in cmd and "plugins" in cmd
        sets = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-c"]
        for s in sets:
            tomllib.loads(s)  # -c 값은 TOML로 읽힌다
        name = "computer-use" if mode == "cu" else "cuop"
        assert any(s.startswith(f"mcp_servers.{name}.command=") for s in sets)
        assert "mcp_servers.ai-memory.enabled=false" in sets  # 사용자 설정의 다른 MCP는 끈다
        assert ("computer_use" in cmd) == (mode == "cuop")  # cuop 쪽만 computer_use 기능을 끈다


def test_strip_removes_images() -> None:
    e = {
        "item": {"result": {"content": [{"type": "image", "data": "A" * 5000}]}},
        "u": "data:image/png;base64," + "B" * 3000,
    }
    s = C._strip(e)
    assert "AAAA" not in str(s) and "BBBB" not in str(s)


def test_consent_only_for_harness_apps() -> None:
    tid = "01a20120-4f05-7c80-898a-6abc94cc54cf"
    ask: dict[str, Any] = {"serverName": "computer-use", "mode": "form", "threadId": tid, "turnId": "t1",
                           "message": "Allow ChatGPT to use Microsoft Word?",
                           "requestedSchema": {"type": "object", "properties": {}},
                           "_meta": {"persist": ["always"]}}  # fmt: skip
    assert C.consent(ask, tid, "t1") == {"action": "accept", "content": {}}  # persist 없이 — 이번 한 번
    no = {"action": "decline"}
    assert C.consent({**ask, "message": "Allow ChatGPT to use Finder?"}, tid, "t1") == no
    assert C.consent({**ask, "message": "Allow ChatGPT to use Microsoft Word? and Finder"}, tid, "t1") == no
    assert C.consent({**ask, "serverName": "cuop"}, tid, "t1") == no
    assert C.consent(ask, "other-thread", "t1") == no  # 이 run의 스레드만
    assert C.consent(ask, tid) == no  # 턴 id가 확정되기 전에는 허가하지 않는다
    assert C.consent({**ask, "mode": "url"}, tid, "t1") == no
    assert C.consent({**ask, "requestedSchema": {"properties": {"x": {"type": "string"}}}}, tid, "t1") == no
    assert C.consent({**ask, "requestedSchema": None}, tid, "t1") == no
    assert C.consent({**ask, "turnId": "t1"}, tid, "t1") == {"action": "accept", "content": {}}
    assert C.consent({**ask, "turnId": "t1"}, tid, "t2") == no  # 이 턴만


def test_cu_app_paths_are_allowed() -> None:
    ev = [{"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "computer-use", "tool": "click",
                                              "arguments": {"app": "/Applications/Microsoft Word.app/"}}}]  # fmt: skip
    assert C.summarize(ev, "cu")["violations"] == []
