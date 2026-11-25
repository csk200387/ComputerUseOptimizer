"""desktop CLI 코어(OS 무관) — envelope · ref · 저장소 · 표시 변환 · 인자 파싱."""

from pathlib import Path

import pytest

from cuop.desktop import cli, commands
from cuop.desktop.errors import CuopError, disposition, fail
from cuop.desktop.present import compact, interactive_only, relabel, to_json, twins
from cuop.desktop.store import Namespace, RefEntry, Snapshot, end_session, parse_ref, start_session
from cuop.schema import Role, State, UIElementNode


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CUOP_HOME", str(tmp_path))
    monkeypatch.delenv("CUOP_SESSION", raising=False)
    return tmp_path


def n(i: str, role: Role, name: str | None = None, *kids: UIElementNode, ref: bool = False) -> UIElementNode:
    return UIElementNode(id=i, role=role, raw_role="AX", name=name, children=list(kids), ref=ref)


def test_disposition_retry_follows_delivery() -> None:
    assert disposition("not_delivered") == {"delivery": "not_delivered", "retry": "safe"}
    assert disposition("unknown")["retry"] == "unknown"
    assert disposition("delivered_unverified")["retry"] == "unsafe"


def test_error_envelope_omits_empty_and_recovery_rules() -> None:
    e = fail("click", CuopError("INVALID_ARGS", "bad")).get("error")
    assert e is not None and set(e) == {"code", "message", "disposition"}  # 비면 생략, INVALID_ARGS는 recovery 없음
    stale = CuopError("STALE_REF", "gone", details={"kind": "saved_ref_absent"}).to_json()
    assert stale["recovery"]["requires_fresh_snapshot"] is True
    unsafe = CuopError("ACTION_FAILED", "x", delivery="delivered_unverified").to_json()
    assert "recovery" not in unsafe and unsafe["disposition"]["retry"] == "unsafe"
    timeout = CuopError("TIMEOUT", "slow").to_json()
    assert timeout["recovery"]["retry_after_ms"] > 0


def test_refs_must_be_qualified() -> None:
    assert parse_ref("@s7:e42") == ("s7", "e42")
    for bad in ("@e42", "e42", "@s7e42", "s7:e42"):
        with pytest.raises(CuopError) as ex:
            parse_ref(bad)
        assert ex.value.code == "INVALID_ARGS"
    with pytest.raises(CuopError) as ex:
        parse_ref("@e3")
    assert ex.value.details["kind"] == "unqualified_ref"


def test_stable_ids_follow_tokens() -> None:
    ns = Namespace()
    s1, a = ns.allocate(["aa", "bb", None])
    s2, b = ns.allocate(["bb", "cc", "aa", None])
    assert s1.endswith("1") and s2.endswith("2") and s1[:-1] == s2[:-1]  # namespace 접두어 + 순번
    assert a[:2] == ["e1", "e2"] and b[0] == "e2" and b[2] == "e1"  # 같은 토큰 → 같은 번호
    assert a[2] != b[3]  # 토큰 없는 요소는 매번 새 번호


def test_snapshot_store_is_scoped_by_session() -> None:
    glob = Namespace()
    snap = Snapshot(
        snapshot_id="s1", created_at=0, app="X", pid=1, refs={"e1": RefEntry(pid=1, role="button", raw_role="AXButton")}
    )
    glob.save(snap)
    assert glob.load("s1").refs["e1"].role == "button"
    assert glob.latest() == {"snapshot_id": "s1", "ref_count": 1}
    sid = start_session("demo", trace=True)["id"]
    with pytest.raises(CuopError) as ex:
        Namespace(sid).load("s1")  # 다른 namespace의 스냅샷은 보이지 않는다
    assert ex.value.code == "SNAPSHOT_NOT_FOUND"
    with pytest.raises(CuopError) as ex:
        glob.resolve("@s1:e9")
    assert ex.value.code == "STALE_REF"
    end_session(sid)
    with pytest.raises(CuopError) as ex:
        Namespace(sid).check_open()
    assert ex.value.details["kind"] == "session_ended"


def test_interactive_only_keeps_ancestors_of_refs() -> None:
    tree = n(
        "w", Role.WINDOW, "W", n("g", Role.GROUP, None, n("t", Role.TEXT, "label"), n("b", Role.BUTTON, "OK", ref=True))
    )
    shown = interactive_only(tree)
    assert shown is not None
    assert [x.id for x in shown.walk()] == ["w", "g", "b"]


def test_compact_hoists_bare_wrappers() -> None:
    tree = n(
        "w", Role.WINDOW, "W", n("g1", Role.GROUP, None, n("g2", Role.GROUP, None, n("b", Role.BUTTON, "OK", ref=True)))
    )
    out = compact(tree)
    assert out is not None and [x.id for x in out.walk()] == ["w", "b"]


def test_json_shape_matches_agent_desktop() -> None:
    node = UIElementNode(
        id="e4", role=Role.TEXTBOX, raw_role="AXTextField", name="Search", ref=True,
        states=frozenset({State.FOCUSED, State.COLLAPSED}), actions=("SetValue",), native_id="q",
    )  # fmt: skip
    j = to_json(node, "s3", bounds=False)
    assert j == {
        "available_actions": ["SetValue"],
        "name": "Search",
        "native_id": {"kind": "ax_identifier", "value": "q"},
        "ref_id": "@s3:e4",
        "role": "textfield",
        "states": ["focused"],  # collapsed는 agent-desktop 어휘가 아니다
    }


def test_relabel_and_twins() -> None:
    tree = n("#0", Role.WINDOW, "W", n("#1", Role.BUTTON, "Delete", ref=True), n("#2", Role.BUTTON, "Delete", ref=True))
    out = relabel(tree, ["e1", "e7", "e9"])
    assert [x.id for x in out.walk()] == ["e1", "e7", "e9"]
    assert ("AX", None, "Delete") in twins(out)


def test_cli_parse_error_is_an_envelope_with_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "ax", object)
    env, code = cli.run(["snapshot", "--bogus"])
    assert code == 2 and env["ok"] is False and env["command"] == "snapshot"
    assert env["error"]["code"] == "INVALID_ARGS" and env["error"]["disposition"]["retry"] == "safe"
    env, code = cli.run(["get", "@e3"])  # bare ref는 명령 단계에서 거부(exit 1)
    assert code == 1 and env["error"]["code"] == "INVALID_ARGS"


def test_global_flags_anywhere() -> None:
    env, code = cli.run(["version", "--session", "abc"])
    assert code == 0 and env["data"]["os"] == "macos"
    env, code = cli.run(["--format", "text", "version"])
    assert code == 0


IDS = {"OK": "e10", "Cancel": "e11", "Help": "e12", "저장": "e13", "취소": "e14"}


def _tree(names: list[str], extra: int = 0, long: bool = True) -> UIElementNode:
    rows = [n(IDS[nm], Role.BUTTON, nm, ref=True) for nm in names]  # 같은 요소 = 같은 id (토큰 → 안정 번호)
    big = [n(f"e{i + 100}", Role.TEXT, ("글자 " * 20 if long else "") + str(i)) for i in range(extra)]
    return n("e1", Role.WINDOW, "W", n("e2", Role.GROUP, "panel", *rows), n("e3", Role.GROUP, "list", *big))


def test_text_full_then_diff_then_budget() -> None:
    from cuop.desktop import text

    ns = Namespace()
    snap = Snapshot(snapshot_id="sa1", created_at=0, app="App", pid=7, window_id=9, window_title="W")
    first = text.render(ns, snap, _tree(["OK", "Cancel"]), goal=None, want_diff=True)  # 처음이면 전체
    assert first.startswith('[@sa1] App "W" w-9') and 'e10 button "OK"' in first
    snap2 = snap.model_copy(update={"snapshot_id": "sa2"})
    d = text.render(ns, snap2, _tree(["Cancel", "OK", "Help"]), goal=None, want_diff=True)
    assert d.splitlines()[0] == '[@sa2 since @sa1] "W"' and 'e12 button "Help"' in d and "^ e2: e11 e10" in d
    big = text.render(ns, snap2, _tree(["OK"], extra=400), goal=None, want_diff=True)
    head = big.splitlines()[0]  # 예산을 넘으면 이름 있는 조작 요소 먼저(filtered), 그래도 넘으면 skeleton
    assert ("(filtered:" in head or "skeleton" in head) and text.tokens(big) <= text.BUDGET
    assert (
        "since" not in head
    )  # 필터 없는 뷰 → 자동 필터로 바뀌면 Diff가 아니라 전체(걸러진 노드를 '삭제'로 보내지 않게)


def test_text_rerenders_full_when_another_call_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    from cuop.desktop import text

    monkeypatch.setattr(text, "tokens", len)
    ns = Namespace()
    other = Namespace()
    snap = Snapshot(snapshot_id="sa1", created_at=0, app="App", pid=7, window_id=9, window_title="W")
    text.render(ns, snap, _tree(["OK"]), goal=None)
    winner = snap.model_copy(update={"snapshot_id": "sa2"})
    slow = snap.model_copy(update={"snapshot_id": "sa3"})
    results: list[str] = []
    revisions: list[int | None] = []
    save = ns.save_view

    def interleave(key: str, data: dict[str, object], *, expected_revision: int | None) -> bool:
        revisions.append(expected_revision)
        if len(revisions) == 1:
            results.append(text.render(other, winner, _tree(["Cancel"]), goal=None, want_diff=True))
        return save(key, data, expected_revision=expected_revision)

    monkeypatch.setattr(ns, "save_view", interleave)
    full = text.render(ns, slow, _tree(["OK", "Help"]), goal=None, want_diff=True)
    assert results[0].startswith('[@sa2 since @sa1] "W"')
    assert full.startswith('[@sa3] App "W" w-9\n')
    assert 'e10 button "OK"' in full and 'e12 button "Help"' in full
    assert revisions == [1, 2]
    follow = snap.model_copy(update={"snapshot_id": "sa4"})
    delta = text.render(other, follow, _tree(["Help"]), goal=None, want_diff=True)
    assert delta.startswith('[@sa4 since @sa3] "W"') and "- e10" in delta


def test_view_revision_is_per_key_and_supports_legacy_json(home: Path) -> None:
    ns = Namespace()
    views = home / "global" / "views"
    views.mkdir()
    (views / "old.json").write_text('{"seen":["e1"]}', encoding="utf-8")
    assert ns.view("old") == ({"seen": ["e1"]}, 0)
    assert ns.save_view("new", {}, expected_revision=None)
    assert not ns.save_view("new", {"seen": ["e2"]}, expected_revision=None)
    assert ns.view("new") == ({"revision": 1}, 1)
    assert ns.save_view("old", {"seen": []}, expected_revision=0)
    assert ns.view("old") == ({"seen": [], "revision": 1}, 1)


def test_text_goal_filter_hides_unrelated() -> None:
    from cuop.desktop import text

    snap = Snapshot(snapshot_id="sb1", created_at=0, app="App", pid=8, window_id=3, window_title="W")
    out = text.render(Namespace(), snap, _tree(["저장", "취소"], extra=60, long=False), goal="저장")
    assert '"저장"' in out and "… +" in out and len(out.splitlines()) < 40  # 예산 30 + 조상
