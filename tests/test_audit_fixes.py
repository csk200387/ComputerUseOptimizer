"""M10 감사(Codex) 수정의 회귀 — OS 호출은 가짜로 바꾼다."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cuop.desktop import actions as A
from cuop.desktop import cli, store, trace
from cuop.desktop import commands as C
from cuop.desktop import system as S
from cuop.desktop.errors import CuopError, ok
from cuop.schema import Bounds


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CUOP_HOME", str(tmp_path))
    monkeypatch.setenv("CUOP_OVERLAY", "0")
    monkeypatch.delenv("CUOP_SESSION", raising=False)
    return tmp_path


def _fake(ran: list[str]) -> Any:
    def clear(ctx: Any) -> dict[str, Any]:
        del ctx
        ran.append("clear")
        return {}

    return clear


def test_unresponsive_write_is_not_a_safe_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "_io", lambda: SimpleNamespace(ABSENT=frozenset({-25205, -25206, -25208})))
    act = A.Act("set-value", "@sab1:e1")
    assert act.ax(-25206, "ax_set_value") is False  # 미지원 = 다음 수단, 아무것도 안 보냄
    assert act.delivery == "not_delivered"
    with pytest.raises(CuopError) as e:
        act.ax(-25204, "ax_set_value")
    assert e.value.code == "APP_UNRESPONSIVE"
    assert e.value.to_json()["disposition"] == {"delivery": "delivery_uncertain", "retry": "unsafe"}


def test_batch_rejects_missing_required_args_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    real = cli.REGISTRY["clipboard-clear"]
    monkeypatch.setitem(cli.REGISTRY, "clipboard-clear", (_fake(ran), real[1]))
    plan = json.dumps([{"command": "clipboard-clear"}, {"command": "click", "args": {}}])
    env, code = cli.run(["batch", plan])
    assert code == 1 and env["error"]["code"] == "INVALID_ARGS" and "ref" in env["error"]["message"]
    assert ran == []  # 첫 동작도 실행되지 않았다


def test_ended_session_refuses_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    real = cli.REGISTRY["clipboard-clear"]
    monkeypatch.setitem(cli.REGISTRY, "clipboard-clear", (_fake(ran), real[1]))
    sid = store.start_session("t", trace=True)["id"]
    store.end_session(sid)
    env, _ = cli.run(["clipboard-clear", "--session", sid])
    assert not env["ok"] and env["error"]["details"]["kind"] == "session_ended"
    assert ran == []


def test_gc_keeps_open_and_recent_sessions() -> None:
    live = store.start_session("open", trace=False)["id"]
    done = store.start_session("done", trace=False)["id"]
    store.end_session(done)
    assert store.gc_sessions(ended_only=False, older_than=None) == []  # 5분 안에 쓴 세션은 끝났어도 남긴다
    assert store.gc_sessions(ended_only=False, older_than=0) == []  # 열린 세션도 최근 기록이 있으면 남긴다
    assert store.gc_sessions(ended_only=True, older_than=None) == [done]  # --ended: 끝난 것은 기록 시각을 보지 않는다
    assert [m["id"] for m in store.list_sessions()] == [live]


def test_trace_hides_free_text_in_shaped_fields() -> None:
    args: dict[str, Any] = {"ref": "Dear John, see you", "action": "Reply to Mom", "window_id": "w-12", "app": "Mail"}
    out = trace.redact(args)
    assert out["ref"] == {"redacted": True} and out["action"] == {"redacted": True}
    assert out["window_id"] == "w-12" and out["app"] == "Mail"
    assert trace.redact({"ref": "@sab1:e2", "action": "click"}) == {"ref": "@sab1:e2", "action": "click"}
    ev = trace._clean({"args": args, "target": {"ref": "secret words", "role": "button"}, "ts_ms": int(time.time())})  # pyright: ignore[reportPrivateUsage]
    assert ev["target"] == {"role": "button"} and ev["args"]["ref"] == {"redacted": True}


@pytest.mark.anyio
async def test_mcp_uses_public_names_and_leaf_tools() -> None:
    from mcp import Client

    from cuop.desktop import mcp

    server = mcp.build(C.Ctx(), {})
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"desktop_session_start", "desktop_trace_show", "desktop_batch", "desktop_skills_get"} <= names
        bad = await c.call_tool("desktop_click", {"ref_id": "e3"})  # 공개 이름 ref_id → 함수 인자 ref
        assert bad.is_error and "INVALID_ARGS" in str(bad.content[0])
        assert bad.structured_content["ok"] is False and bad.structured_content["error"]["code"] == "INVALID_ARGS"
        started = await c.call_tool("desktop_session_start", {"name": "t", "screenshots": True})
        assert not started.is_error and '"screenshots":"not_recorded"' in str(started.content[0])
        assert started.structured_content["data"]["screenshots"] == "not_recorded"


@pytest.mark.anyio
async def test_mcp_preserves_structured_observations_and_moves_inline_image(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp import Client
    from mcp.types import ImageContent, TextContent

    from cuop.desktop import mcp

    envelopes = {
        "snapshot": ok("snapshot", {"text": "tree\nsecond line"}),
        "click": ok("click", {"observation": {"text": "diff\nsecond line"}}),
        "screenshot": ok("screenshot", {"format": "png", "data": "aGVsbG8="}),
        "list-windows": ok("list-windows", [{"id": "w-12"}]),
    }

    def observed(name: str, *_: object) -> tuple[dict[str, Any], int]:
        return envelopes[name], 0

    monkeypatch.setattr(cli, "observed", observed)
    async with Client(mcp.build(C.Ctx(fmt="json"), {})) as c:
        for name, args, observation in (
            ("snapshot", {}, "tree\nsecond line"),
            ("click", {"ref_id": "@sab1:e1"}, "diff\nsecond line"),
        ):
            result = await c.call_tool(f"desktop_{name}", args)
            block = result.content[0]
            assert isinstance(block, TextContent) and block.text.endswith(f"\n{observation}")
            body = json.loads(block.text.splitlines()[0])["data"]
            assert "text" not in (body if name == "snapshot" else body["observation"])
            assert result.structured_content == envelopes[name]
            data = result.structured_content["data"]
            assert (data if name == "snapshot" else data["observation"])["text"] == observation
        shot = await c.call_tool("desktop_screenshot", {})
        assert shot.structured_content["data"] == {"format": "png"}
        head, image = shot.content
        assert isinstance(head, TextContent) and json.loads(head.text) == shot.structured_content
        assert isinstance(image, ImageContent) and image.mime_type == "image/png" and image.data == "aGVsbG8="
        listed = await c.call_tool("desktop_list_windows", {})
        assert listed.structured_content == envelopes["list-windows"]
        block = listed.content[0]
        assert isinstance(block, TextContent) and json.loads(block.text) == listed.structured_content


def test_inventory_envelopes_are_arrays(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cg = [
        {
            "kCGWindowOwnerPID": 42,
            "kCGWindowNumber": 11,
            "kCGWindowName": "Document",
            "kCGWindowBounds": {"X": 1.5, "Y": 2, "Width": 300, "Height": 400},
        },
        {"kCGWindowOwnerPID": 42, "kCGWindowNumber": 12},
    ]
    bounds = {"x": 0, "y": 0, "width": 640, "height": 480}
    displays = [{"id": i, "is_primary": i == 9, "bounds": bounds, "scale": 2.0} for i in (10, 9, 2)]

    def app_windows(_: int) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=11, title="Document", focused=True, minimized=False)]

    fake_ax = SimpleNamespace(
        running_apps=lambda: [SimpleNamespace(pid=42, name="Editor")],
        cg_windows=lambda on_screen=False: cg[:1] if on_screen else cg,
        frontmost_pid=lambda: 42,
        _permitted=lambda _pid: True,  # pyright: ignore[reportUnknownLambdaType]
        app_windows=app_windows,
        displays=lambda: displays,
    )
    monkeypatch.setattr(C, "ax", lambda: fake_ax)
    trace_path = tmp_path / "inventory.jsonl"
    windows, code = cli.run(["list-windows", "--trace", str(trace_path)])
    assert code == 0
    assert windows["data"] == [
        {"id": "w-11", "title": "Document", "app_name": "Editor", "pid": 42,
         "is_focused": True, "visible": True, "accessible": True, "minimized": False,
         "bounds": {"x": 1.5, "y": 2, "width": 300, "height": 400}},
        {"id": "w-12", "title": "Editor", "app_name": "Editor", "pid": 42,
         "is_focused": False, "visible": False, "accessible": False},
    ]  # fmt: skip
    event = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[1])
    assert event["command"] == "list-windows" and event["ok"] is True
    result, code = cli.run(["list-displays"])
    assert code == 0
    assert result["data"] == [{**d, "id": str(d["id"])} for d in (displays[1], displays[2], displays[0])]
    assert displays[0]["id"] == 10


def test_focus_window_returns_window_under_focused(monkeypatch: pytest.MonkeyPatch) -> None:
    win = SimpleNamespace(
        ref_id="w-11", title="Document", pid=42, el=object(), bounds=Bounds(x=1, y=2, w=300, h=400),
        minimized=False, focused=False, on_screen=True,
    )  # fmt: skip

    def window_by_id(wid: int) -> SimpleNamespace:
        assert wid == 11
        return win

    def allowed(pid: int) -> None:
        assert pid == win.pid

    def app_by_pid(pid: int) -> SimpleNamespace:
        assert pid == win.pid
        return SimpleNamespace(name="Editor")

    def raise_window(el: object, pid: int) -> bool:
        return (el, pid) == (win.el, win.pid)

    def attr(el: object, name: str) -> object:  # 앱의 포커스 창 = 그 창
        del el
        return win.el if name == "AXFocusedWindow" else None

    fake_ax = SimpleNamespace(window_by_id=window_by_id, allowed=allowed, app_by_pid=app_by_pid, attr=attr,
                              app_element=lambda pid: "app")  # pyright: ignore[reportUnknownLambdaType]  # fmt: skip
    monkeypatch.setattr(S, "ax", lambda: fake_ax)
    monkeypatch.setattr(S, "_io", lambda: SimpleNamespace(raise_window=raise_window))
    result, code = cli.run(["focus-window", "--window-id", "w-11"])
    assert code == 0
    assert result["data"] == {
        "focused": {"id": "w-11", "title": "Document", "app_name": "Editor", "pid": 42, "is_focused": True,
                    "accessible": True, "visible": True, "minimized": False,
                    "bounds": {"x": 1, "y": 2, "width": 300, "height": 400}},
    }  # fmt: skip


def test_redrill_drops_vanished_descendants() -> None:
    from cuop.desktop.store import Namespace, RefEntry, Snapshot

    ns = Namespace(None)
    sid, _ = ns.allocate([])

    def ref(name: str) -> RefEntry:
        return RefEntry(token=None, pid=1, role="button", raw_role="AXButton", name=name)

    def snap(*names: str) -> Snapshot:
        return Snapshot(snapshot_id=sid, created_at=0, app="a", pid=1, refs={n: ref(n) for n in names})

    ns.save(snap("e1"))
    ns.save(snap("e1", "e2", "e3"), merge=True, drill="e1")
    ns.save(snap("e1", "e3"), merge=True, drill="e1")
    assert sorted(ns.load(sid).refs) == ["e1", "e3"]  # e2는 두 번째 drill에 없다 → 더는 주소가 아니다


@pytest.mark.anyio
async def test_guide_rides_first_snapshot_only_for_clients_without_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex(app-server · code mode)는 MCP instructions를 모델에 넣지 않는다(INSTRUCTIONS_EXP 1단계) → 그 클라이언트만
    첫 snapshot 응답 머리에 지시문을 한 번 받는다. 지시문을 읽는 클라이언트(Claude Code 등)는 받지 않는다."""
    from mcp import Client
    from mcp.types import Implementation, TextContent

    from cuop.desktop import mcp

    def observed(name: str, *_: object) -> tuple[dict[str, Any], int]:
        return ok(name, {"text": "tree"}), 0

    monkeypatch.setattr(cli, "observed", observed)
    for client, want in (("codex-mcp-client", True), ("claude-code", False)):
        async with Client(
            mcp.build(C.Ctx(fmt="text"), {}, "core"), client_info=Implementation(name=client, version="0")
        ) as c:
            texts: list[str] = []
            for _ in range(2):
                r = await c.call_tool("desktop_snapshot", {})
                block = r.content[0]
                assert isinstance(block, TextContent)
                texts.append(block.text)
            assert (mcp.INSTRUCTIONS in texts[0]) is want and mcp.INSTRUCTIONS not in texts[1]
            assert texts[1].endswith("tree")
