# pyright: reportPrivateUsage=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""Jev 플러그인(src/cuop/optimizer/jev.py) — 키 없이 모의 응답으로. 키는 어떤 출력에도 나오지 않아야 한다."""

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from cuop.optimizer import jev
from cuop.optimizer.filter import task_filter
from cuop.schema import Role, UIElementNode, UISnapshot

FAKE = "tsk-test-not-a-real-key"


def _resp(answers: dict[str, Any], i: int = 10, o: int = 5) -> bytes:
    usage = {"input_tokens": i, "output_tokens": o}
    return json.dumps({"model": "jev-1", "answers": answers, "usage": usage}).encode()


def test_key_from_env_or_hermes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert jev._key() is None and not jev.enabled()
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / ".env").write_text(f"OTHER=1\nexport TYPESAFE_API_KEY='{FAKE}'\n")
    assert jev._key() == FAKE
    assert not jev.enabled()  # CUOP_JEV=1 없이는 꺼져 있다
    monkeypatch.setenv("CUOP_JEV", "1")
    assert jev.enabled()
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-wins")
    assert jev._key() == "env-wins"


def test_relevance_chunks_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE)
    monkeypatch.setattr(jev, "MAX_QUESTIONS", 2)
    sent: list[dict[str, Any]] = []

    def urlopen(req: Any, timeout: float) -> Any:
        body = json.loads(req.data)
        sent.append(body)
        assert FAKE not in json.dumps(body)  # 키는 본문이 아니라 헤더에만
        return _resp({k: {"type": "noul", "noul": 0.9 if k == "e2" else 0.1} for k in body["questions"]})

    monkeypatch.setattr(jev, "_open", urlopen)
    jev.STATS.take()
    got = jev.relevance("굵게", {"e1": 'button "저장"', "e2": 'checkbox "굵게"', "e3": 'button "인쇄"'})
    assert got == {"e1": 0.1, "e2": 0.9, "e3": 0.1} and len(sent) == 2  # 질문 2개씩 두 묶음
    stats = jev.STATS.take()
    assert stats["jev_calls"] == 2 and stats["jev_in"] == 20 and FAKE not in json.dumps(stats)

    def broken(req: Any, timeout: float) -> Any:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(jev, "_open", broken)
    assert jev.relevance("굵게", {"e1": "x"}) is None  # 실패 = 규칙으로
    assert jev.STATS.take()["jev_errors"] == 1


def test_route_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE)

    def urlopen(req: Any, timeout: float) -> Any:
        q = json.loads(req.data)["questions"]["route"]
        assert q["type"] == "choice" and set(q["criteria"]) == {"tree", "keys", "screenshot"}
        return _resp({"route": {"type": "choice", "choice": "keys", "confidence": 0.87}})

    monkeypatch.setattr(jev, "_open", urlopen)
    assert jev.route("Excel", "[@s1] Excel …", None) == ("keys", 0.87)


def _view() -> UISnapshot:
    kids = [UIElementNode(id=f"e{i}", role=Role.BUTTON, raw_role="AXButton", name=f"버튼 {i}") for i in range(1, 6)]
    win = UIElementNode(id="w", role=Role.WINDOW, raw_role="AXWindow", name="창", children=kids)
    return UISnapshot(
        id="s1",
        platform="macos",
        app="X",
        root=UIElementNode(id="r", role=Role.APPLICATION, raw_role="AXApplication", children=[win]),
    )


def test_task_filter_ranks_by_jev_scores() -> None:
    view = _view()
    rule = task_filter(view, "무관한 목표", set(), budget=2)
    jevd = task_filter(view, "무관한 목표", set(), budget=2, scores={"e4": 0.9, "e5": 0.8})

    def pick(v: UISnapshot) -> list[str]:
        return [n.id for n in v.root.walk() if n.id.startswith("e")]

    assert pick(rule) == ["e1", "e2"]  # 규칙: 동점이면 문서 순서
    assert pick(jevd) == ["e4", "e5"]  # Jev: 확률 순


def test_text_uses_jev_only_with_goal_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from cuop.desktop import text

    calls: list[str] = []
    monkeypatch.setattr(jev, "enabled", lambda: True)

    def relevance(goal: str, lines: dict[str, str]) -> dict[str, float]:
        calls.append(goal)
        return dict.fromkeys(lines, 0.5)

    monkeypatch.setattr(jev, "relevance", relevance)
    text._jev_cache.clear()
    view = _view()
    assert text.jev_scores(view, "목표") == {f"e{i}": 0.5 for i in range(1, 6)} | {"w": 0.5}
    assert text.jev_scores(view, "목표") is not None and calls == ["목표"]  # 같은 뷰 · 같은 goal은 캐시
    monkeypatch.setattr(jev, "enabled", lambda: False)
    text._jev_cache.clear()
    assert text.jev_scores(view, "목표") is None


def test_route_hint_once_per_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from cuop.desktop import commands as C
    from cuop.desktop import mcp

    monkeypatch.setattr(jev, "enabled", lambda: True)
    answers = iter([("keys", 0.87), ("tree", 0.95), ("screenshot", 0.4)])
    monkeypatch.setattr(jev, "route", lambda *_: next(answers))  # pyright: ignore[reportUnknownLambdaType]
    routed: set[str] = set()
    ctx = C.Ctx(fmt="text")

    def env(wid: str) -> dict[str, Any]:
        return {"ok": True, "data": {"window": {"id": wid}}}

    out = mcp._route("snapshot", env("w-1"), {}, ctx, "[@s1] Excel\ntree", routed)
    assert out.startswith("[route: keys+screenshot, jev 0.87]\n")
    assert mcp._route("snapshot", env("w-1"), {}, ctx, "[@s2] Excel", routed) == "[@s2] Excel"  # 창마다 한 번
    assert mcp._route("snapshot", env("w-2"), {}, ctx, "[@s3] TextEdit", routed) == "[@s3] TextEdit"  # tree → 힌트 없음
    assert mcp._route("snapshot", env("w-3"), {}, ctx, "[@s4] Game", routed) == "[@s4] Game"  # 확신도 0.4 < 0.7
    assert mcp._route("click", env("w-9"), {}, ctx, "diff", routed) == "diff"
    assert mcp._route("snapshot", env("w-5"), {}, ctx, "[@s5] X (partial: raise --timeout-ms)", routed).startswith(
        "[@s5]"
    )
    assert mcp._route("snapshot", {"ok": True, "data": {}}, {}, ctx, "[@s6] Y", routed) == "[@s6] Y"  # 창 id 없음
    assert "w-5" not in routed  # 부분 뷰는 판단을 쓰지 않는다
    events = jev.STATS.take()["jev_events"]
    assert [e["shown"] for e in events] == [True, False, False]


def test_malformed_answers_fall_back_to_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """잘린 응답 · 객체 아닌 JSON · 이상한 usage · 범위 밖 확률 · 불리언 — 모두 None(규칙)이고 예외가 새지 않는다."""
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE)
    bodies = [b'{"answers": {"e1": {"noul": 0.5}', b"[1, 2]", json.dumps({"answers": {"e1": {"noul": 1.5}}}).encode(),
              json.dumps({"answers": {"e1": {"noul": True}}, "usage": {"input_tokens": "lots"}}).encode()]  # fmt: skip
    for body in bodies:
        monkeypatch.setattr(jev, "_open", lambda *_, b=body: b)
        assert jev.relevance("g", {"e1": "x"}) is None
    monkeypatch.setattr(jev, "_open", lambda *_: _resp({"route": {"choice": "keys", "confidence": float("inf")}}))
    assert jev.route("X", "t", None) is None


def test_redirects_are_not_followed() -> None:
    """Authorization 헤더가 다른 호스트로 넘어가지 않게 리다이렉트를 따라가지 않는다."""
    import urllib.request

    h = jev._NoRedirect()
    req = urllib.request.Request(jev.URL, headers={"Authorization": "Bearer x"})
    assert h.redirect_request(req, None, 302, "Found", {}, "http://evil.example/") is None


def test_unscored_rule_matches_keep_their_rule_rank() -> None:
    view = _view()
    # e5만 goal과 맞는다(규칙 2점) — Jev 점수를 못 받았어도(상한 밖) 낮은 Jev 점수 요소보다 앞
    got = task_filter(view, "버튼 5", set(), budget=1, scores={"e1": 0.1, "e2": 0.1, "e3": 0.1, "e4": 0.1})
    assert [n.id for n in got.root.walk() if n.id.startswith("e")] == ["e5"]


def test_overflow_and_bad_usage_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE)
    huge = json.dumps({"answers": {"e1": {"noul": 10**400}}, "usage": {"input_tokens": 1, "output_tokens": 1}})
    monkeypatch.setattr(jev, "_open", lambda *_: huge.encode())
    assert jev.relevance("g", {"e1": "x"}) is None
    bad = json.dumps({"answers": {"e1": {"noul": 0.5}}, "usage": {"input_tokens": -5, "output_tokens": 1}})
    monkeypatch.setattr(jev, "_open", lambda *_: bad.encode())
    assert jev.relevance("g", {"e1": "x"}) is None


def test_route_reads_text_header_not_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    from cuop.desktop import commands as C
    from cuop.desktop import mcp

    monkeypatch.setattr(jev, "enabled", lambda: True)
    monkeypatch.setattr(jev, "route", lambda *_: ("keys", 0.9))
    env = {"ok": True, "data": {"window": {"id": "w-7"}}}
    out = '{"version":"2.6","ok":true}\n[@s1] Excel (partial: raise --timeout-ms)\ntree'
    routed: set[str] = set()
    assert mcp._route("snapshot", env, {}, C.Ctx(fmt="text"), out, routed) == out and not routed


def test_stuck_requests_do_not_pile_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import urllib.request

    gate = threading.Event()

    class Slow:
        def open(self, *_: Any, **__: Any) -> Any:
            gate.wait(5)
            raise OSError

    monkeypatch.setattr(jev, "_OPENER", Slow())
    monkeypatch.setattr(jev, "_INFLIGHT", [])
    req = urllib.request.Request(jev.URL)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            jev._open(req, 0.05)
    assert len(jev._INFLIGHT) == 2
    with pytest.raises(TimeoutError):
        jev._open(req, 0.05)  # 상한 — 새 스레드를 만들지 않는다
    assert len(jev._INFLIGHT) == 2
    gate.set()
