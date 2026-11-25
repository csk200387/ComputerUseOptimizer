from cuop.schema import AddedNode, Bounds, NodeChange, Role, State, UIDiffResult, UIElementNode, UISnapshot

TREE = UIElementNode(
    id="e1",
    role=Role.WINDOW,
    raw_role="AXWindow",
    name="제목 없음",
    bounds=Bounds(x=0, y=25, w=800, h=600),
    children=[
        UIElementNode(
            id="e2",
            role=Role.TEXTBOX,
            raw_role="AXTextArea",
            value="Hello",
            states=frozenset({State.FOCUSED, State.EDITABLE}),
        )
    ],
)


def test_json_round_trip() -> None:
    snap = UISnapshot(id="s1", platform="macos", app="com.apple.TextEdit", window="제목 없음", root=TREE)
    diff = UIDiffResult(
        base_id="s1",
        snapshot_id="s2",
        added=[AddedNode(parent_id="e1", node=TREE)],
        removed=["e9"],
        changed=[NodeChange(id="e2", value=("Hello", "Hello!"), states_off=frozenset({State.FOCUSED}))],
    )
    assert UISnapshot.model_validate_json(snap.model_dump_json()) == snap
    assert UIDiffResult.model_validate_json(diff.model_dump_json()) == diff
    assert not diff.is_empty
    assert UIDiffResult(base_id="s1", snapshot_id="s2").is_empty
    assert [n.id for n in TREE.walk()] == ["e1", "e2"]


def test_states_serialize_sorted() -> None:
    assert '"states":["editable","focused"]' in TREE.children[0].model_dump_json()
