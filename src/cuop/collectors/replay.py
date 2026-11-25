"""녹화한 스냅샷을 순서대로 재생하는 수집기 — OS 없이 서버 전체 경로로 모드별 토큰을 잰다."""

from collections.abc import Iterable
from pathlib import Path

from cuop.schema import Bounds, UISnapshot


class ReplayCollector:
    """snapshot()은 녹화본을 차례로 돌려주고, 마지막 뒤로는 마지막 것을 반복한다.
    액션은 다음 녹화본으로 넘어가는 계기일 뿐 대상은 검사하지 않는다."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self._snaps = [UISnapshot.model_validate_json(p.read_bytes()) for p in sorted(paths)]
        self._next = 0

    async def snapshot(self, app: str | None = None) -> UISnapshot:
        snap = self._snaps[min(self._next, len(self._snaps) - 1)]
        self._next += 1
        return snap

    async def click(self, element_id: str) -> None:
        pass

    async def type_text(self, element_id: str | None, text: str, submit: bool = False) -> None:
        pass

    async def select(self, element_id: str, option: str) -> None:
        pass

    async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        raise NotImplementedError("replay has no screen")

    async def click_at(self, x: int, y: int) -> None:
        pass

    async def owner(self, element_id: str) -> str | None:
        return None  # web은 조작한 탭을 수집기가 직접 고정한다 · replay는 녹화 순서대로

    async def display(self) -> Bounds:
        raise NotImplementedError("replay has no screen")

    async def type_keys(self, text: str, submit: bool = False) -> None:
        raise NotImplementedError("replay has no screen")
