"""Jev(TypeSafe System One) 선택 플러그인 — 문장 없이 noul · choice · score만 돌려주는 판단 모델(JEV_BRIEF).

켜는 법: `CUOP_JEV=1` + 키. 키는 환경변수 `TYPESAFE_API_KEY`, 없으면 `~/.hermes/.env`의 그 한 줄만 읽는다.
**키는 어디에도 남기지 않는다** — 로그 · 예외 메시지 · 반환값에 넣지 않는다.
오류 · 시간 초과면 None(호출 측은 규칙 그대로).
OS 무관 · 표준 라이브러리만(urllib).
"""

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT_S = 8.0
MAX_QUESTIONS = 250  # 요청당 질문 수 — 실측 1,000개 0.55초까지 한도 없음(JEV_EXP 1절), 묶음은 넉넉히
MAX_STATE = 12000  # state 문자 수 상한(긴 트리는 자른다)


@dataclass
class Stats:
    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0
    events: list[dict[str, Any]] = field(default_factory=lambda: [])

    def take(self) -> dict[str, Any]:
        """지난번 이후의 합계를 돌려주고 비운다(MCP 도구 호출마다 step 로그에)."""
        out = {"jev_calls": self.calls, "jev_errors": self.errors, "jev_in": self.input_tokens,
               "jev_out": self.output_tokens, "jev_ms": self.ms, "jev_events": self.events}  # fmt: skip
        self.calls = self.errors = self.input_tokens = self.output_tokens = self.ms = 0
        self.events = []
        return out


STATS = Stats()


def _key() -> str | None:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    try:
        for line in (Path.home() / ".hermes" / ".env").read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == "TYPESAFE_API_KEY":
                return value.strip().strip("'\"") or None
    except OSError:
        return None
    return None


def has_key() -> bool:
    return _key() is not None


def enabled() -> bool:
    return os.environ.get("CUOP_JEV", "") == "1" and _key() is not None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다 — urllib은 Authorization 헤더를 다른 호스트 · http로도 넘긴다(Codex 리뷰 P1)."""

    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


MAX_INFLIGHT = 2  # 마감을 넘겨 뒤에서 아직 읽는 요청 수 상한(소켓 timeout이 지나면 스레드도 끝난다)
_INFLIGHT: list[threading.Thread] = []
_LOCK = threading.Lock()


def _open(req: urllib.request.Request, timeout: float) -> bytes:
    """요청 하나를 timeout 안에 끝낸다 — 소켓 timeout은 읽기마다라, 느리게 흘러오는 응답도 이 마감에서 끊는다."""
    box: dict[str, bytes] = {}

    def run() -> None:
        try:
            with _OPENER.open(req, timeout=timeout) as r:  # 고정 https URL, 리다이렉트 없음
                box["body"] = r.read()
        except Exception:  # noqa: BLE001, S110 — 원인은 남기지 않는다(호출 측이 실패로 센다)
            pass

    t = threading.Thread(target=run, daemon=True)
    with _LOCK:  # 정리 · 상한 확인 · 시작 · 등록을 한 번에 — 동시 호출이 시작 전 스레드를 지우지 못하게
        _INFLIGHT[:] = [x for x in _INFLIGHT if x.is_alive()]
        if len(_INFLIGHT) >= MAX_INFLIGHT:  # 앞의 느린 요청이 아직 읽는 중 — 쌓지 않고 바로 실패(규칙으로)
            raise TimeoutError
        t.start()
        _INFLIGHT.append(t)
    t.join(timeout)
    if "body" not in box:
        raise TimeoutError
    return box["body"]


def _count(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**9 else None


def _prob(v: Any) -> float | None:
    """0–1 사이 유한한 수만(불리언 · 무한대 · 범위 밖은 거부)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if 0.0 <= f <= 1.0 else None


def ask(state: str, questions: dict[str, dict[str, Any]], timeout: float = TIMEOUT_S) -> dict[str, Any] | None:
    """질문 묶음 하나 → answers(id → 답). 어떤 실패든 None — 원인은 STATS.errors에만(키 · 본문 · 값은 남기지 않음)."""
    key = _key()
    if key is None:
        return None
    body = json.dumps({"model": MODEL, "state": state[:MAX_STATE], "questions": questions}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(URL, data=body, method="POST", headers=headers)
    t0 = time.monotonic()
    STATS.calls += 1
    try:
        data: Any = json.loads(_open(req, max(0.1, timeout)))
    except Exception:  # noqa: BLE001 — 네트워크 · 잘린 응답 · 리다이렉트 · JSON 오류 모두 규칙으로(메시지는 남기지 않음)
        data = None
    finally:
        STATS.ms += round((time.monotonic() - t0) * 1000)
    if not isinstance(data, dict):
        STATS.errors += 1
        return None
    d = cast(dict[str, Any], data)
    usage: Any = d.get("usage")
    u = cast(dict[str, Any], usage) if isinstance(usage, dict) else {}
    i, o = _count(u.get("input_tokens")), _count(u.get("output_tokens"))
    answers: Any = d.get("answers")
    if i is None or o is None or not isinstance(answers, dict):  # 응답 모양이 다르면 전체를 실패로(규칙으로)
        STATS.errors += 1
        return None
    STATS.input_tokens += i
    STATS.output_tokens += o
    return cast(dict[str, Any], answers)


def relevance(goal: str, lines: dict[str, str], deadline_s: float = 2 * TIMEOUT_S) -> dict[str, float] | None:
    """요소(id → 한 줄)마다 '이 작업(goal)에 필요한가'의 noul 확률. 실패 · 전체 시간 초과면 None(규칙으로)."""
    out: dict[str, float] = {}
    ids = list(lines)
    instr = ("Is this UI element needed (to read or to operate) for the user's current sub-goal? "
             "Answer yes only for elements the task will likely use.")  # fmt: skip
    end = time.monotonic() + deadline_s
    for i in range(0, len(ids), MAX_QUESTIONS):
        chunk = ids[i : i + MAX_QUESTIONS]
        state = f'Sub-goal: {goal}\nUI elements (id role "name" =value):\n' + "\n".join(
            f"{k} {lines[k]}" for k in chunk
        )
        qs = {k: {"type": "noul", "instructions": f"{instr} Element: {k} {lines[k]}"} for k in chunk}
        left = end - time.monotonic()
        ans = ask(state, qs, timeout=min(TIMEOUT_S, left)) if left > 0.1 else None
        if ans is None:
            return None
        for k in chunk:
            a: Any = ans.get(k)
            p = _prob(cast(dict[str, Any], a).get("noul")) if isinstance(a, dict) else None
            if p is None:
                STATS.errors += 1  # 답이 빠졌거나 범위 밖 — 이 뷰는 규칙으로
                return None
            out[k] = p
    return out


ROUTES = ("tree", "keys", "screenshot")


def route(app: str, tree_text: str, goal: str | None) -> tuple[str, float] | None:
    """첫 snapshot의 트리로 '작업 내용이 트리에 있는가' → (tree | keys | screenshot, 확신도). 실패하면 None."""
    state = f"App: {app}\nAgent sub-goal: {goal or '(none)'}\nAccessibility tree (truncated):\n{tree_text}"
    q = {"route": {"type": "choice", "criteria": dict.fromkeys(ROUTES), "instructions": (
        "How should a computer-use agent work in this window? 'tree' if the document or work content (text, "
        "cells, items, values) appears as elements in this accessibility tree; 'keys' if the work area is a "
        "grid or canvas whose content is missing from the tree but can be driven with keyboard input (for "
        "example a spreadsheet grid or a slide editor); 'screenshot' if the content is visual only (a canvas, "
        "an image, a game) and must be seen in an image.")}}  # fmt: skip
    ans = ask(state, q)
    a: Any = (ans or {}).get("route")
    if not isinstance(a, dict):
        return None
    r = cast(dict[str, Any], a)
    choice: Any = r.get("choice")
    conf = _prob(r.get("confidence"))
    if choice not in ROUTES or conf is None:
        STATS.errors += ans is not None
        return None
    return str(choice), conf
