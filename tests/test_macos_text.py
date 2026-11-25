"""macOS 수집기의 순수 함수 — PyObjC가 있는 macOS에서만."""

import pytest

macos = pytest.importorskip("cuop.collectors.macos")


def test_small_runs_become_superscripts() -> None:
    mark = macos._mark_superscripts  # pyright: ignore[reportPrivateUsage]
    # 계산기 공학용 2¹⁰ + 3: 밑수 30.6pt · 지수 21.4pt (LRM 구간은 빈 문자열로 들어온다)
    assert mark([("", 13.0), ("2", 30.6), ("", 13.0), ("10", 21.4), ("+", 30.6), ("3", 30.6)]) == "2^(10)+3"
    assert mark([("", 13.0), ("1,024", 30.6)]) is None  # 구간 하나 — 그대로
    assert mark([("12", 30.6), ("34", 30.0)]) is None  # 크기가 거의 같으면 위첨자 아님
