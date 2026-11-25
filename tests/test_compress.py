from cuop.optimizer.compress import MAX_TEXT, node_line
from cuop.schema import Role, State, UIElementNode


def test_node_line_quotes_strips_truncates_and_sorts_states() -> None:
    node = UIElementNode(
        id="e2",
        role=Role.TEXTBOX,
        raw_role="AXTextArea",
        name='say "hi"',
        value="‎" + "x" * (MAX_TEXT + 20),  # 계산기 디스플레이처럼 LRM이 앞에 붙은 값
        states=frozenset({State.FOCUSED, State.EDITABLE}),
    )
    assert node_line(node, depth=1) == f' e2 textbox "say \\"hi\\"" ="{"x" * (MAX_TEXT - 1)}…" [editable,focused]'
