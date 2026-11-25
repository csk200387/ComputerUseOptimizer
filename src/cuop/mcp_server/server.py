"""CUOP MCP 서버 (stdio) — observe · click · type · select."""

import argparse
import functools
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import get_args

import tiktoken
from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

from cuop.collectors import Browser, Collector, get_collector
from cuop.optimizer import fallback
from cuop.optimizer.compress import render_diff, render_snapshot
from cuop.optimizer.diff import diff
from cuop.optimizer.filter import prune, task_filter
from cuop.schema import Bounds, FallbackReason, Mode, Platform, StepLog, UISnapshot, VisionFrame

Args = dict[str, str | int | bool | None]
Output = str | list[str | Image]  # 관찰 텍스트, Partial Vision이면 + 이미지


@functools.cache
def _encoder() -> tiktoken.Encoding:
    # ponytail: OpenAI o200k 근사 — 모드 간 비율 비교용. Claude 실제 토큰은 하네스가 CLI usage로 잰다
    return tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_encoder().encode_ordinary(text))


def _append(path: Path, entry: StepLog) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(entry.model_dump_json() + "\n")


def build_server(
    collector: Collector,
    mode: Mode = "full",
    log_path: Path | None = None,
    web: Browser | None = None,
    run_id: str | None = None,
) -> MCPServer:
    """tool 이름·스키마는 모든 모드에서 같다 — 모드는 관찰 표현만 바꾼다.

    수집기 라우팅: observe의 url · 브라우저 이름 app과 `w`로 시작하는 element_id는 web, 나머지는 collector(OS).

    tool 설명·에러 메시지는 매 턴 LLM 입력에 들어가므로 짧은 영어로 쓴다.
    """
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    mcp = MCPServer("cuop")
    run_id = run_id or uuid.uuid4().hex[:8]  # 하네스는 CUOP_RUN_ID로 정해 Claude usage와 조인한다
    step = 0
    current: Collector = collector  # 마지막으로 관찰한 수집기 — 액션 후 같은 곳을 다시 관찰한다
    apps: dict[Collector, str | None] = {}  # 수집기별 마지막 관찰 앱 (web은 무시 — 보이는 탭)
    # UI State Manager — 앱·탭(platform, app)별. 다른 앱을 보고 돌아와도 그 앱의 Diff를 이어 간다
    # ponytail: 서버 지역 변수로 충분, Fallback 좌표계 등 상태가 늘면 optimizer/state.py로 분리
    # sent: 에이전트가 마지막으로 받은 뷰 — proposed는 이것과의 Diff만 보낸다
    # seen: 같은 goal로 한 직전 관찰의 (가지치기 후) 노드 — Filter가 숨긴 노드를 계속 숨긴다
    sent: dict[tuple[str, str], UISnapshot] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    last_goal = ""  # 마지막 observe의 goal — 액션 후 관찰에도 같은 필터를 쓴다
    shown: tuple[Collector, VisionFrame] | None = None  # 직전 관찰의 이미지 좌표계 — click(x, y)는 이것 기준

    def refuse() -> None:
        """① screenshot 모드엔 요소 id가 없다 — 좌표 클릭과 키보드 입력만."""
        if mode == "screenshot":
            raise ValueError("screenshot mode has no element ids; click x, y on the image and type into the focus")

    def route(target: str) -> Collector:
        return web if web is not None and target.startswith("w") else collector

    async def follow(col: Collector, target: str) -> None:
        """액션 뒤 관찰은 요소가 속한 앱 — 계산기 버튼을 누르고 텍스트 편집기를 다시 읽던 문제(2026-11-04 하네스)."""
        if (owner := await col.owner(target)) is not None:
            apps[col] = owner

    async def observe_after(
        col: Collector,
        tool: str,
        args: Args,
        action: Callable[[], Awaitable[None]] | None = None,
        full: bool = False,
        vision: str | None = None,
    ) -> Output:
        """(액션 →) 스냅샷 → 모드별 뷰 → 직렬화 (→ Partial Vision). 성공·실패와 관계없이 Step 로그 한 줄."""
        nonlocal step, current, shown
        step += 1
        t0 = time.perf_counter()
        text, nodes, sent_nodes, error = "", 0, 0, None
        reason: FallbackReason | None = None
        tokens_img = 0
        try:
            if action:
                await action()
            snap = await col.snapshot(apps.get(col))
            current, apps[col] = col, snap.app
            nodes = sum(1 for _ in snap.root.walk())
            if mode == "screenshot":  # ① 전체 화면 이미지만 — 트리는 대상 앱 확인 · 노드 수 기록에만
                area = await col.display()
                f = fallback.frame(area)
                # 대상 앱을 앞으로 가져온 뒤 찍는다 — 대상 앱 창 · 메뉴 막대만(다른 앱 창은 비움, D13)
                png = await col.capture(area, f.width, f.height, only_target=True)
                shown, tokens_img = (col, f), fallback.image_tokens(f)
                text = f"[{snap.id}] {snap.app} screen {f.width}x{f.height}: click x, y are pixels of this image"
                return [text, Image(data=png, format="png")]
            view = prune(snap) if mode in ("compact", "proposed") else snap
            key = (view.platform, view.app)
            base = sent.get(key)
            if base is not None and base.root.id != view.root.id:
                base = None  # 같은 주소의 새 문서(페이지 다시 열기) → ID가 모두 새것이라 전체 뷰
            present = {n.id for n in view.root.walk()}
            if mode == "proposed":
                known = {n.id for n in base.root.walk()} if base is not None else set[str]()
                view = task_filter(view, last_goal, known, seen.get(key, set()) if base is not None else set())
            d = diff(base, view) if base is not None else None
            if mode == "proposed" and d is not None and not full:
                sent_nodes = len(d.changed) + len(d.removed) + sum(1 for a in d.added for _ in a.node.walk())
                text = render_diff(view, d)
            else:
                sent_nodes = sum(1 for _ in view.root.walk()) - 1  # 루트(앱)는 헤더로만 표시
                text = render_snapshot(view)
            # Fallback 판정은 원본 스냅샷으로 → 모든 모드가 같은 판단
            win = fallback.window(snap)
            area: Bounds | None = None
            if vision is not None:
                reason, area = "visual_region", fallback.region(snap, vision)
            elif win is not None and win.bounds is not None and fallback.sparse(win):
                reason, area = "empty_tree", win.bounds
            elif tool != "observe" and d is not None and d.is_empty:
                reason, text = "no_diff", f"{text}\nhint: no change; {fallback.HINT}"
            f = fallback.frame(area) if area is not None else None
            png = None
            if area is not None and f is not None:
                try:
                    png = await col.capture(area, f.width, f.height)
                except Exception as e:
                    if vision is not None:
                        raise  # 에이전트가 요청한 이미지 — 실패를 그대로 알린다
                    text += f"\nimage unavailable: {e}"  # 자동 이미지가 실패해도 관찰은 돌려준다
            # 상태는 에이전트에게 실제로 보낼 때만 바꾼다 — 실패한 호출 뒤 Diff가 받지 못한 뷰를 기준 삼지 않게
            sent[key] = view
            if mode == "proposed":
                seen[key] = present
            shown = None
            if png is None or f is None:
                return text
            shown, tokens_img = (col, f), fallback.image_tokens(f)
            why = " (tree has few named elements)" if reason == "empty_tree" else ""
            label = vision or (win.id if win else "")
            text += f"\nimage {label} {f.width}x{f.height}{why}: click x, y are pixels of this image"
            return [text, Image(data=png, format="png")]
        except Exception as e:
            if isinstance(e, LookupError) and tool != "observe":
                reason, e = "target_missing", LookupError(f"{e}; {fallback.HINT}")
            error = f"{type(e).__name__}: {e}"
            raise ToolError(error) from e  # ToolError가 아니면 SDK가 메시지를 가린다("Error executing tool X")
        finally:
            if log_path:
                elapsed = round((time.perf_counter() - t0) * 1000, 1)
                entry = StepLog(
                    run_id=run_id, step=step, mode=mode, tool=tool, args=args, latency_ms=elapsed,
                    raw_nodes=nodes, sent_nodes=sent_nodes, out_chars=len(text),
                    out_tokens=count_tokens(text), fallback=reason, image_tokens=tokens_img, error=error,
                )  # fmt: skip
                _append(log_path, entry)

    @mcp.tool(structured_output=False)
    async def observe(
        goal: str, app: str | None = None, url: str | None = None, full: bool = False, vision: str | None = None
    ) -> Output:
        """Observe the UI as an indented tree of `id role "name" ="value" [states]`.
        Later views may list only changes since your last view: `~ id` new values, `+ parent` new subtree, `- ids` gone.
        goal: your current sub-goal in one sentence, in the words the UI shows (`… +N` = N hidden; name them in goal).
        app: app name or bundle id (default: frontmost app, or the browser tab you were on);
        a browser name observes its visible tab.
        url: open this page in a new browser tab. full: resend the whole view instead of changes.
        vision: "window" or an element id; also returns an image of that area (canvas, color, layout)."""
        nonlocal last_goal
        if goal != last_goal:
            seen.clear()  # 새 goal로 다시 고른다
        last_goal = goal
        if url is not None and web is None:
            raise ToolError("no browser connected")
        browser = web  # 람다 안에서도 None이 아님을 타입 검사기가 알게
        if browser and (url is not None or (app and browser.handles(app))):
            col = browser
        elif vision not in (None, "window"):
            col = route(vision)  # 요소 id의 수집기 — 액션과 같은 접두어 규칙
        else:  # app 생략 = 마지막으로 관찰한 수집기(웹이면 그 탭, OS는 맨 앞 앱) — 2026-11-04 사용자 결정
            col = current if app is None else collector
        apps[col] = app
        if mode == "screenshot" and browser and col is browser:  # ①은 화면 픽셀만 — 브라우저 창도 OS 수집기로 본다
            col, apps[collector] = collector, browser.os_app
        action = (lambda: browser.open(url)) if browser and url is not None else None
        args: Args = {"goal": goal, "app": app, "url": url, "full": full, "vision": vision}
        return await observe_after(col, "observe", args, action, full=full, vision=vision)

    @mcp.tool(structured_output=False)
    async def click(target: str | None = None, x: int | None = None, y: int | None = None) -> Output:
        """Click element `target` (an id from observe, e.g. e42), or pixel x, y of the image
        your last observation returned. Returns the updated UI."""
        if target is not None:
            col, element = route(target), target

            async def press() -> None:
                refuse()
                await follow(col, element)
                await col.click(element)

            return await observe_after(col, "click", {"target": target}, press)
        image = shown  # 이 호출의 관찰이 shown을 바꾸기 전 값

        async def at() -> None:  # 검증도 액션 안에서 → 잘못된 좌표 클릭도 Step 로그에 남는다
            if image is None:
                raise RuntimeError('x, y need an image from your last observation; call observe(vision="window")')
            if x is None or y is None:
                raise ValueError("pass target, or both x and y")
            await image[0].click_at(*fallback.to_screen(image[1], x, y))

        return await observe_after(image[0] if image else current, "click", {"x": x, "y": y}, at)

    @mcp.tool(name="type", structured_output=False)
    async def type_text(text: str, target: str | None = None, submit: bool = False) -> Output:
        """Set the text of element `target` (default: focused element), replacing its content;
        `submit` presses Enter. Returns the updated UI."""
        args: Args = {"text": f"<{len(text)} chars>", "target": target, "submit": submit}  # 입력값은 로그에 남기지 않음
        col = route(target) if target else current

        async def act() -> None:
            if mode == "screenshot":
                if target:
                    refuse()
                await col.type_keys(text, submit)  # 포커스된 칸에 키보드 이벤트
            else:
                if target:
                    await follow(col, target)
                await col.type_text(target, text, submit)

        return await observe_after(col, "type", args, act)

    @mcp.tool(structured_output=False)
    async def select(target: str, option: str) -> Output:
        """Select the option named `option` in popup/combobox/list `target`. Returns the updated UI."""
        args: Args = {"target": target, "option": option}
        col = route(target)

        async def choose() -> None:
            refuse()
            await follow(col, target)
            await col.select(target, option)

        return await observe_after(col, "select", args, choose)

    return mcp


def main() -> None:
    p = argparse.ArgumentParser(prog="cuop-mcp", description="CUOP MCP 서버 (stdio)")
    p.add_argument("--platform", choices=get_args(Platform), default="macos")
    p.add_argument("--mode", choices=get_args(Mode), default="full")
    p.add_argument(
        "--browser",
        default="msedge",
        help="web: 연결할 Chromium 채널(msedge · chrome) · launch(하네스 전용 새 브라우저)",
    )
    p.add_argument("--log", type=Path, default=os.environ.get("CUOP_LOG"), help="Step 로그 JSONL (기본: $CUOP_LOG)")
    a = p.parse_args()
    from cuop.collectors.web import WebCollector  # 브라우저 연결은 처음 쓸 때

    os_collector = get_collector(a.platform)
    # 보이는 탭 = 맨 앞 창 제목. 하네스 전용 브라우저(launch)는 서버가 연 탭만 다루므로 창 제목이 필요 없다
    title = None if a.browser == "launch" else getattr(os_collector, "window_title", None)
    web = WebCollector(a.browser, title)
    build_server(os_collector, a.mode, a.log, web, os.environ.get("CUOP_RUN_ID")).run()
