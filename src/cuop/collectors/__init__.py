"""UI 접근 계층(Adapter). 플랫폼 SDK는 각 수집기 모듈 안에서만 import한다."""

from typing import Protocol

from cuop.schema import Bounds, Platform, UISnapshot


class Collector(Protocol):
    async def snapshot(self, app: str | None = None) -> UISnapshot:
        """대상 앱(생략 시 맨 앞 앱)의 UI 트리를 공통 스키마로 수집한다."""
        ...

    async def click(self, element_id: str) -> None: ...

    async def type_text(self, element_id: str | None, text: str, submit: bool = False) -> None: ...

    async def select(self, element_id: str, option: str) -> None: ...

    async def capture(self, region: Bounds, width: int, height: int, only_target: bool = False) -> bytes:
        """region(플랫폼 좌표)의 화면을 width×height PNG로 — Partial Vision · 비교군 ①. 좌표 변환은 서버가 한다."""
        ...

    async def click_at(self, x: int, y: int) -> None:
        """플랫폼 좌표 클릭 — Fallback · 비교군 ①."""
        ...

    async def owner(self, element_id: str) -> str | None:
        """요소가 속한 앱(번들 ID) — 액션 뒤엔 이 앱을 관찰한다. 따로 추적하지 않으면 None."""
        ...

    async def display(self) -> Bounds:
        """주 디스플레이 영역(플랫폼 좌표) — 비교군 ① 전체 화면 캡처."""
        ...

    async def type_keys(self, text: str, submit: bool = False) -> None:
        """대상 앱의 포커스된 칸에 키보드 이벤트로 입력 — 비교군 ①(요소 id 없음)."""
        ...


class Browser(Collector, Protocol):
    """웹 수집기. 서버는 observe의 url · 브라우저 이름 app과 `w`로 시작하는 element_id를 이쪽으로 보낸다."""

    def handles(self, app: str) -> bool:
        """app 인자가 이 브라우저를 가리키는가 ("Edge", "msedge", 번들 ID …)."""
        ...

    async def open(self, url: str) -> None:
        """새 탭에서 url을 연다. 이후 관찰 대상은 그 탭."""
        ...

    @property
    def os_app(self) -> str:
        """OS 수집기가 이 브라우저 창을 찾을 이름(번들 ID)."""
        ...


def get_collector(platform: Platform) -> Collector:
    """플랫폼 SDK가 없는 OS에서도 패키지를 import할 수 있도록 지연 import한다."""
    if platform == "macos":
        from cuop.collectors.macos import MacCollector

        return MacCollector()
    if platform == "web":
        raise NotImplementedError("web은 --platform이 아니라 observe(url=…) · app=브라우저 이름으로 쓴다 (--browser)")
    raise NotImplementedError(f"{platform} 수집기는 아직 없음 (windows: 6–8주)")
