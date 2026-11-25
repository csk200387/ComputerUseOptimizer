"""앱 · 창 · 클립보드 · 스크린샷 · surface 명령 (agent-desktop commands-system.md · commands-observation.md)."""

import base64
import json
import os
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any

from cuop.desktop.actions import Act, not_front
from cuop.desktop.commands import DIALOGS, Ctx, ax
from cuop.desktop.errors import CuopError


def _sys() -> Any:
    from cuop.collectors import ax_system

    return ax_system


def _io() -> Any:
    from cuop.collectors import ax_actions

    return ax_actions


# --- launch · close-app ---
def launch(ctx: Ctx, app: str, timeout: int = 30000, arg: list[str] | None = None, env: list[str] | None = None,
           cwd: str | None = None, no_attach: bool = False, activate: bool = False,
           cdp: int | None = None) -> dict[str, Any]:  # fmt: skip
    """프로세스가 뜨면 돌아온다. 기본은 이미 떠 있는 인스턴스에 붙고, 앞으로 가져오지 않는다(--activate만)."""
    del ctx
    m, s = ax(), _sys()
    path = s.app_path(app)
    bid = s.bundle_id(path)
    m.allowed_bundle(bid)  # 허용 목록 세션(하네스)은 그 밖의 앱을 띄우지 않는다
    envs = dict(kv.split("=", 1) for kv in env or [] if "=" in kv)
    if len(envs) != len(env or []):
        raise CuopError("INVALID_ARGS", "--env takes KEY=VALUE")
    args = list(arg or [])
    before = {a.pid for a in s.instances(bid, path)}
    host = _io().host_pid()
    if host is not None and (
        host in before or (bid and (m.app_by_pid(host) or m.App(0, "", None, 0, None, None)).bundle_id == bid)
    ):
        raise CuopError(
            "POLICY_DENIED", "refusing to launch or activate the app that runs this agent", details={"kind": "host_app"}
        )
    if cdp is not None:
        if cdp and not 1024 <= cdp <= 65535:
            raise CuopError("INVALID_ARGS", "--cdp port must be 1024-65535")
        if before:
            raise CuopError("ACTION_FAILED", f"{app} is already running; --cdp needs a fresh process",
                            details={"kind": "cdp_requires_fresh_launch"})  # fmt: skip
        if any(x.startswith(("--remote-debugging", "--remote-allow-origins")) for x in args):
            raise CuopError("INVALID_ARGS", "--arg must not carry --remote-debugging-* switches",
                            details={"kind": "cdp_switch_conflict"})  # fmt: skip
        cdp = cdp or _free_port()
        if not _port_free(cdp):
            raise CuopError("INVALID_ARGS", f"port {cdp} is in use", details={"kind": "cdp_port_in_use"})
        args += [f"--remote-debugging-port={cdp}", "--remote-debugging-address=127.0.0.1"]
    deadline = time.monotonic() + timeout / 1000
    attached = bool(before) and not no_attach
    if not attached:
        s.open_app(path, activate=activate, new=no_attach and bool(before), args=args, env=envs, cwd=cwd,
                   timeout=deadline - time.monotonic())  # fmt: skip
    elif activate:
        s.open_app(path, activate=True, new=False, args=[], env={}, cwd=None, timeout=deadline - time.monotonic())
    proc = None
    while time.monotonic() < deadline:
        now = s.instances(bid, path)
        fresh = [a for a in now if a.pid not in before]
        proc = (fresh or (now if attached else []) or [None])[0]
        if proc is not None:
            break
        time.sleep(0.2)
    if proc is None:
        raise CuopError("TIMEOUT", f"{app} did not start within {timeout} ms", delivery="delivered_unverified")
    data: dict[str, Any] = {"app": proc.name, "pid": proc.pid, "bundle_id": proc.bundle_id, "attached": attached}
    win = _first_window(m, proc.pid, wait=activate, deadline=deadline)
    if win is not None:
        data["window"] = {"id": win.ref_id, "title": win.title, "visible": win.on_screen}
    if m.renderer(proc) == "chromium":
        data["renderer"] = "chromium"
        data["suggestion"] = "Chromium/Electron app: cuop enables its web accessibility on the first snapshot"
    if cdp is not None:
        data["cdp"] = _cdp_endpoint(cdp, deadline)
    return data


def _first_window(m: Any, pid: int, wait: bool, deadline: float) -> Any:
    while True:
        wins = [w for w in m.app_windows(pid) if not w.minimized]
        if wins or not wait or time.monotonic() > deadline:
            return next((w for w in wins if w.on_screen), wins[0] if wins else None)
        time.sleep(0.3)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _cdp_endpoint(port: int, deadline: float) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/json/version", timeout=1) as r:  # noqa: S310 — 루프백 고정
                info = json.loads(r.read())
            ws = str(info.get("webSocketDebuggerUrl") or "")
            if not ws.startswith(f"ws://127.0.0.1:{port}/devtools/browser/"):
                time.sleep(0.3)  # 다른 리스너거나 아직 준비 전
                continue
            return {"port": port, "http_endpoint": url, "websocket_url": info.get("webSocketDebuggerUrl"),
                    "product": info.get("Browser")}  # fmt: skip
        except OSError:
            time.sleep(0.3)
    details = {"kind": "cdp_endpoint_unavailable", "port": port}
    raise CuopError("ACTION_FAILED", "no DevTools endpoint answered; the app stays running", details=details,
                    delivery="delivered_unverified")  # fmt: skip


def close_app(ctx: Ctx, app: str, force: bool = False, timeout_ms: int = 10000) -> dict[str, Any]:
    """종료 요청 후 프로세스가 사라진 것을 본 뒤에만 성공. 저장 창이 뜨면 delivered_unverified로 그 창을 알린다."""
    del ctx
    m, s = ax(), _sys()
    try:
        proc = m.find_app(app)
    except CuopError as e:
        if e.code != "APP_NOT_FOUND":
            raise
        act = Act("close-app")
        act.extra.update(method="force" if force else "graceful", target={"kind": "app", "name": app})
        return act.data()  # 이미 꺼져 있다 — not_delivered
    if s.protected(proc):
        raise CuopError("INVALID_ARGS", f"{proc.name} is a protected system process", details={"kind": "protected"})
    twins = [a for a in m.running_apps() if a.bundle_id and a.bundle_id == proc.bundle_id]
    if len(twins) > 1:
        raise CuopError("AMBIGUOUS_TARGET", f"{len(twins)} instances of {proc.name} are running",
                        details={"candidates": [{"pid": a.pid} for a in twins]})  # fmt: skip
    act = Act("close-app")
    act.extra.update(method="force" if force else "graceful",
                     target={"kind": "app", "name": proc.name, "process": {"pid": proc.pid}})  # fmt: skip
    sent = s.terminate(proc.pid, force)
    act.step("terminate" if not force else "force_terminate", sent)
    if s.wait_until(lambda: not s.alive(proc.pid), timeout_ms / 1000):
        act.delivery = "delivered_verified"
        return act.data()
    surfaces = _io().open_surfaces(proc.pid, None)
    raise act.fail("TIMEOUT", f"{proc.name} is still running (a save prompt may be open)", kind="close_pending",
                   surfaces=surfaces)  # fmt: skip


# --- 창 ---
def _window(app: str | None, window_id: str | None, title: str | None = None) -> Any:
    w = _find_window(app, window_id, title)
    ax().allowed(w.pid)
    return w


def _find_window(app: str | None, window_id: str | None, title: str | None = None) -> Any:
    m = ax()
    if window_id is not None:
        w = m.window_by_id(int(window_id.removeprefix("w-")))
        if app is not None and w.pid != m.find_app(app).pid:
            raise CuopError("INVALID_ARGS", f"--window-id {window_id} does not belong to {app!r}")
        return w
    if app is None and title is None:
        raise CuopError("INVALID_ARGS", "give --window-id, --app or --title")
    apps = [m.find_app(app)] if app else [a for a in m.running_apps() if a.policy == 0 and m._permitted(a.pid)]  # pyright: ignore[reportPrivateUsage]
    hits: list[Any] = []
    for a in apps:
        wins = m.app_windows(a.pid)
        if title is not None:
            wins = [w for w in wins if w.title and title.casefold() in w.title.casefold()]
            hits += wins
        elif wins:
            pick = m.pick_window(a.pid, a.name)
            return pick if pick is not None else wins[0]  # 보이는 창이 없으면(최소화 등) 첫 창
    if len(hits) == 1:
        return hits[0]
    if hits:  # 제목이 여러 창에 맞으면 고르지 않는다
        cands = [{"id": w.ref_id, "title": w.title, "pid": w.pid} for w in hits]
        raise CuopError("AMBIGUOUS_TARGET", f"{len(hits)} windows match {title!r}; pass --window-id",
                        details={"candidates": cands})  # fmt: skip
    raise CuopError("WINDOW_NOT_FOUND", "no matching window", details={"app": app, "title": title})


def _target(w: Any) -> dict[str, Any]:
    return {"kind": "window", "id": w.ref_id, "title": w.title, "process": {"pid": w.pid}}


def focus_window(
    ctx: Ctx, window_id: str | None = None, app: str | None = None, title: str | None = None
) -> dict[str, Any]:
    del ctx
    w = _window(app, window_id, title)
    owner = ax().app_by_pid(w.pid)
    app_name = owner.name if owner is not None else app or str(w.pid)
    act = Act("focus-window")
    ok = _io().raise_window(w.el, w.pid)
    act.step("focus_window", ok)
    fw = ax().attr(ax().app_element(w.pid), "AXFocusedWindow")
    if not ok or (fw is not None and fw != w.el):  # raise_window는 AXMain만 본다 — 포커스 창이 정확히 그 창이어야
        raise not_front(act, w.pid, w.el, "ACTION_FAILED", "the OS did not report the window as focused")
    focused: dict[str, Any] = {
        "id": w.ref_id or "",
        "title": w.title or app_name,
        "app_name": app_name,
        "pid": w.pid,
        "is_focused": True,
        "accessible": True,
        "minimized": w.minimized,
        "visible": w.on_screen,
    }
    if b := w.bounds:
        focused["bounds"] = {"x": b.x, "y": b.y, "width": b.w, "height": b.h}
    return {"focused": focused}


def _geometry(name: str, app: str | None, window_id: str | None, pos: tuple[float, float] | None,
              size: tuple[float, float] | None) -> dict[str, Any]:  # fmt: skip
    s = _sys()
    w = _window(app, window_id)
    _io().guard(w.pid)
    act = Act(name)
    act.extra["target"] = _target(w)
    if not act.ax(s.set_geometry(w.el, pos=pos, size=size), "ax_set_" + ("size" if size else "position")):
        raise act.fail("ACTION_NOT_SUPPORTED", "the window is not resizable/movable", kind="geometry_not_settable")
    want = size or pos
    assert want is not None

    def reached() -> bool:
        p, sz = s.window_geometry(w.el)
        got = sz if size else p
        return bool(got) and abs(got[0] - want[0]) <= 2 and abs(got[1] - want[1]) <= 2

    scope = "window_size" if size else "window_position"
    if not s.wait_until(reached, 1.0):
        raise act.fail("TIMEOUT", "the window did not reach the requested geometry", kind="postcondition_timeout")
    act.delivery = "delivered_verified"
    act.extra["details"] = {"verification_scope": scope, "tolerance": 2, "units": "points"}
    return act.data()


def resize_window(
    ctx: Ctx, width: float, height: float, app: str | None = None, window_id: str | None = None
) -> dict[str, Any]:
    del ctx
    data = _geometry("resize-window", app, window_id, None, (width, height))
    return {**data, "width": width, "height": height}


def move_window(ctx: Ctx, x: float, y: float, app: str | None = None, window_id: str | None = None) -> dict[str, Any]:
    del ctx
    return {**_geometry("move-window", app, window_id, (x, y), None), "x": x, "y": y}


def _fills(s: Any, el: Any, tol: float = 8) -> bool:
    """창이 주 화면의 보이는 영역을 채우는가(확대 상태). ponytail: 주 화면 기준 — 다른 화면의 창은 그 화면으로."""
    p, sz = s.window_geometry(el)
    if not p or not sz:
        return False
    x, y, w, h = s.visible_frame()
    return all(abs(a - b) <= tol for a, b in ((p[0], x), (p[1], y), (sz[0], w), (sz[1], h)))


def _window_state(name: str, app: str | None, window_id: str | None) -> dict[str, Any]:
    m, s = ax(), _sys()
    w = _window(app, window_id)
    _io().guard(w.pid)
    act = Act(name)
    act.extra["target"] = _target(w)
    minimized = m.attr(w.el, "AXMinimized") is True
    if name == "minimize" or (name == "restore" and minimized):
        want = name == "minimize"
        if minimized == want:
            act.extra["already"] = True
            return act.data()
        if not act.ax(_io().set_attr(w.el, "AXMinimized", want), "ax_minimize" if want else "ax_unminimize"):
            raise act.fail("ACTION_NOT_SUPPORTED", "the window cannot be minimized", kind="not_minimizable")
        if s.wait_until(lambda: (m.attr(w.el, "AXMinimized") is True) == want, 2.0):
            act.delivery = "delivered_verified"
        act.extra["details"] = {"verification_scope": "window_minimized_state"}
        return act.data()
    # maximize: 보이는 영역을 채운다(확대 버튼 → 모자라면 프레임 직접)
    # restore(최소화 아님): 채운 창만 확대 버튼으로 되돌린다
    want = name == "maximize"
    if _fills(s, w.el) == want:
        act.extra["already"] = True
        return act.data()
    zoom = s.window_button(w.el, "AXZoomButton")
    if zoom is not None:
        act.ax(s.press_button(zoom), "ax_zoom")
    if want and not s.wait_until(lambda: _fills(s, w.el), 1.0):
        x, y, width, height = s.visible_frame()  # 확대 버튼이 내용 크기로만 키우는 앱(텍스트 편집기)
        if not act.ax(s.set_geometry(w.el, pos=(x, y), size=(width, height)), "ax_set_frame"):
            raise act.fail("ACTION_NOT_SUPPORTED", "the window cannot be resized", kind="geometry_not_settable")
    if s.wait_until(lambda: _fills(s, w.el) == want, 1.5):
        act.delivery = "delivered_verified"
    elif not act.steps:
        raise act.fail("ACTION_NOT_SUPPORTED", "the window has no zoom button", kind="no_zoom_button")
    act.extra["details"] = {"verification_scope": "window_fills_visible_frame"}
    return act.data()


def minimize(ctx: Ctx, app: str | None = None, window_id: str | None = None) -> dict[str, Any]:
    del ctx
    return _window_state("minimize", app, window_id)


def maximize(ctx: Ctx, app: str | None = None, window_id: str | None = None) -> dict[str, Any]:
    del ctx
    return _window_state("maximize", app, window_id)


def restore(ctx: Ctx, app: str | None = None, window_id: str | None = None) -> dict[str, Any]:
    del ctx
    return _window_state("restore", app, window_id)


# --- 클립보드 ---
def clipboard_get(ctx: Ctx, format: str = "text", out: str | None = None) -> dict[str, Any]:  # noqa: A002
    del ctx
    if format not in ("text", "auto", "image", "file-urls"):
        raise CuopError("INVALID_ARGS", "--format must be text, auto, image or file-urls")
    return _sys().clipboard_read(format, out)


def clipboard_set(ctx: Ctx, text: str | None = None, stdin: bool = False, image: str | None = None,
                  file_url: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    from cuop.desktop.actions import VALUE_LIMIT, _text_arg  # pyright: ignore[reportPrivateUsage]

    del ctx
    if stdin and (image or file_url):
        raise CuopError("INVALID_ARGS", "--stdin cannot combine with --image or --file-url")
    for f in file_url or []:
        if not Path(f).exists():
            raise CuopError("INVALID_ARGS", f"--file-url path does not exist: {f}")
    if image and not Path(image).exists():
        raise CuopError("INVALID_ARGS", f"--image path does not exist: {image}")
    body = None if (image or file_url) else _text_arg(text, stdin, VALUE_LIMIT)
    act = Act("clipboard-set")
    act.extra["target"] = {"kind": "clipboard"}
    ok = _sys().clipboard_write(text=body, image=image, files=file_url)
    act.step("pasteboard_write", ok)
    if not ok:
        raise act.fail("ACTION_FAILED", "the pasteboard refused the write")
    if body is not None and _sys().clipboard_text() == body:
        act.delivery = "delivered_verified"
    return act.data()


def clipboard_clear(ctx: Ctx) -> dict[str, Any]:
    del ctx
    act = Act("clipboard-clear")
    act.extra["target"] = {"kind": "clipboard"}
    act.step("pasteboard_clear", _sys().clipboard_write())
    if _sys().clipboard_text() is None:
        act.delivery = "delivered_verified"
    return act.data()


# --- 스크린샷 · surface ---
def _scale_at(displays: list[dict[str, Any]], bounds: Any) -> float:
    """창 가운데가 놓인 화면의 배율(없으면 주 화면)."""
    if bounds is not None:
        cx, cy = bounds.x + bounds.w / 2, bounds.y + bounds.h / 2
        for d in displays:
            b = d["bounds"]
            if b["x"] <= cx < b["x"] + b["width"] and b["y"] <= cy < b["y"] + b["height"]:
                return float(d["scale"])
    return float(displays[0]["scale"])


def inline_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """응답으로 보내는 스크린샷 크기: 1px = 1pt(Retina 원본의 1/4 토큰), 긴 변 1,568px · 1.15MP 상한(CLAUDE.md §2.5).
    Retina 원본(예: 2940×1846 ≈ 1MB base64)은 Codex가 이미지로 받지 않고 텍스트로 넘겼다(CODEX_CU 본 실험 5424b203)."""
    import math

    pw, ph = width / (scale or 1), height / (scale or 1)
    f = min(1.0, 1568 / max(pw, ph, 1), math.sqrt(1.15e6 / max(pw * ph, 1)))
    return (min(width, max(1, math.floor(pw * f))), min(height, max(1, math.floor(ph * f))))  # 내림 — 상한을 넘지 않게


def screenshot(ctx: Ctx, path: str | None = None, app: str | None = None, window_id: str | None = None,
               screen: int | None = None) -> dict[str, Any]:  # fmt: skip
    """PNG. 경로가 없으면 base64 data. 화면 기록 권한이 필요하다(없으면 PERM_DENIED)."""
    import tempfile

    del ctx
    m, s = ax(), _sys()
    if screen is not None and (app or window_id):
        raise CuopError("INVALID_ARGS", "--screen cannot combine with --app or --window-id")
    if os.environ.get("CUOP_ALLOW_APPS") and not (app or window_id):  # 허용 목록이 있으면 화면 전체는 찍지 않는다
        raise CuopError("POLICY_DENIED", "full-screen capture is off in this session; pass --app or --window-id",
                        details={"kind": "app_not_allowed"})  # fmt: skip
    if os.environ.get("CUOP_ALLOW_APPS") and path:  # 하네스 세션은 디스크에 이미지를 쓰지 않는다(이미지는 응답으로)
        raise CuopError("POLICY_DENIED", "writing screenshots to a path is off in this session; omit --path",
                        details={"kind": "path_not_allowed"})  # fmt: skip
    if not m.screen_recording():
        raise CuopError("PERM_DENIED", "Screen Recording is not granted to the app that launches cuop",
                        details={"permission": "screen_recording"})  # fmt: skip
    displays = m.displays()
    if path and Path(path).exists():
        raise CuopError("INVALID_ARGS", f"{path} exists; screenshot writes new files only")
    if path is None:
        fd, out = tempfile.mkstemp(prefix="cuop-shot-", suffix=".png")  # 0600, 이 프로세스만
        os.close(fd)
    else:
        out = path
    try:
        if screen is not None:
            if not 0 <= screen < len(displays):
                raise CuopError("INVALID_ARGS", f"--screen must be 0-{len(displays) - 1}")
            b = displays[screen]["bounds"]
            s.capture(out, rect=(b["x"], b["y"], b["width"], b["height"]))
            scale, origin = displays[screen]["scale"], (b["x"], b["y"])
        elif app or window_id:
            w = _window(app, window_id)
            if not w.id:  # CG 창 번호가 없으면 전체 화면이 찍힌다 — 다른 창이 드러나지 않게 거부
                raise CuopError("WINDOW_NOT_FOUND", "the window has no capturable window id")
            s.capture(out, window_id=w.id)
            scale = _scale_at(displays, w.bounds)
            origin = (w.bounds.x, w.bounds.y) if w.bounds else None
            after = _window(None, w.ref_id).bounds  # 찍는 사이 창이 움직였으면 origin을 믿을 수 없다 → 주지 않는다
            if after is None or origin != (after.x, after.y):
                origin = None
        else:
            b = displays[0]["bounds"]
            s.capture(out, rect=(b["x"], b["y"], b["width"], b["height"]))
            scale, origin = displays[0]["scale"], (b["x"], b["y"])
        width, height = s.png_size(out)
        if path is None and (size := inline_size(width, height, scale)) != (width, height):
            s.shrink(out, *size)  # 응답(모델)으로 가는 이미지만 줄인다 — 파일(--path)은 원본 그대로
            scale *= size[0] / width if width >= height else size[1] / height  # 긴 변 기준 — 오차는 짧은 변의 반 픽셀
            width, height = size
        data: dict[str, Any] = {"format": "png", "width": width, "height": height, "scale_factor": scale}
        if origin is not None:  # 확장: 이미지 왼쪽 위의 화면 point — mouse-click xy = origin + 픽셀 / scale_factor
            data["origin"] = {
                "x": origin[0],
                "y": origin[1],
            }  # (창 스크린샷의 픽셀을 그대로 xy로 써 33pt 빗나감, Office E회차)
        if path:
            data["path"] = out
        else:
            data["data"] = base64.b64encode(Path(out).read_bytes()).decode()
    except BaseException:
        if path is None or Path(out).exists() and s.png_size(out) == (0, 0):
            Path(out).unlink(missing_ok=True)  # 실패하면 만든 파일을 남기지 않는다
        raise
    finally:
        if path is None:
            Path(out).unlink(missing_ok=True)
    return data


def list_surfaces(ctx: Ctx, app: str) -> dict[str, Any]:
    del ctx
    m = ax()
    a = m.find_app(app)
    app_el = m.AXUIElementCreateApplication(a.pid)
    out: list[dict[str, Any]] = []
    if m.attr(app_el, "AXMenuBar") is not None:
        out.append({"id": "menubar", "type": "menubar"})
    from cuop.desktop.commands import _open_menu  # pyright: ignore[reportPrivateUsage]

    if _open_menu(m, app_el) is not None:
        out.append({"id": "menu", "type": "menu"})
    if m.attr(app_el, "AXExtrasMenuBar") is not None:
        out.append({"id": "menu-bar-extras", "type": "menu-bar-extras"})
    for w in m.app_windows(a.pid):
        try:  # open_surfaces의 alert는 앱 단위(대화상자 창이 하나라도 있으면) → 그 대화상자 창에만 붙인다
            kinds = [
                k for k in _io().open_surfaces(a.pid, w.el) if k != "menu" and (k != "alert" or w.subrole in DIALOGS)
            ]
        except Exception:  # noqa: BLE001 — 바쁜 앱: 그 창은 미분류로
            out.append({"type": "window", "id": w.ref_id, "title": w.title, "unclassified": ["sheet", "popover"]})
            continue
        out.append({"type": "window", "id": w.ref_id, "title": w.title})
        if w.ref_id is None:  # CG 창 번호가 없는 대화상자 — 앱 단위 selector(snapshot --surface alert)
            out += [{"id": k, "type": k} for k in kinds if k == "alert"]
            continue
        out += [{"id": f"{k}@{w.ref_id}", "type": k, "window_id": w.ref_id} for k in kinds]
    return {"app": a.name, "surfaces": out}


def open_system_surface(ctx: Ctx, surface: str) -> dict[str, Any]:
    """macOS의 dock · menu-bar-extras는 늘 화면에 있다 → 아무것도 띄우지 않고 그대로 돌려준다."""
    del ctx
    if surface not in ("dock", "menu-bar-extras"):
        details = {"supported_surfaces": ["dock", "menu-bar-extras"], "requested_surface": surface}
        raise CuopError("INVALID_ARGS", f"{surface!r} is not a macOS shell surface", details=details)
    return {"surface": surface}


# --- 알림 ---
def _nc_session(ctx: Ctx) -> bool:
    """알림 센터가 닫혀 있으면 headless는 거부, headed는 열고(True) 나중에 닫는다. 배너만 있으면 그대로 읽는다."""
    s = _sys()
    if s.center_open():
        return False
    if not ctx.headed:
        raise CuopError("POLICY_DENIED", "Notification Center is closed; pass --headed to open it",
                        details={"kind": "surface_closed"})  # fmt: skip
    if s.toggle_center() != 0 or not s.wait_until(s.center_open, 2.0):
        raise CuopError("ACTION_FAILED", "could not open Notification Center", delivery="delivery_uncertain")
    return True


def _nc_close(opened: bool) -> None:
    if opened:
        _sys().toggle_center()


def list_notifications(
    ctx: Ctx, app: str | None = None, text: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    opened = _nc_session(ctx)
    try:
        rows: list[dict[str, Any]] = [r for _, r in _sys().notifications()]
    finally:
        _nc_close(opened)
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        if app and app.casefold() not in (r["app_name"] or "").casefold():
            continue
        if text and not any(text.casefold() in (r[k] or "").casefold() for k in ("title", "body")):
            continue
        out.append({"index": i, **r})
    out = out[:limit] if limit else out
    return {"count": len(out), "notifications": out}


def _fp(row: dict[str, Any]) -> tuple[str, str, str]:
    """알림 지문: 앱 · 제목 · 본문(같은 알림인지 다시 찾을 때만 쓰고 출력하지 않는다)."""
    return (row["app_name"] or "", row["title"] or "", row["body"] or "")


def _pick(index: int, expected_app: str | None, expected_title: str | None) -> tuple[Any, dict[str, Any]]:
    if not (expected_app or expected_title):
        raise CuopError("INVALID_ARGS", "pass a nonempty --expected-app or --expected-title from the same listing")
    rows = _sys().notifications()
    if not 1 <= index <= len(rows):
        raise CuopError("NOTIFICATION_NOT_FOUND", f"no notification #{index}")
    el, row = rows[index - 1]
    if (expected_app and (row["app_name"] or "") != expected_app) or (
        expected_title and (row["title"] or "") != expected_title
    ):
        raise CuopError("NOTIFICATION_NOT_FOUND", f"notification #{index} no longer matches; list again")
    return el, row


def dismiss_notification(ctx: Ctx, index: int, app: str | None = None, expected_app: str | None = None,
                         expected_title: str | None = None) -> dict[str, Any]:  # fmt: skip
    if not ctx.headed:
        raise CuopError("POLICY_DENIED", "notification mutations need --headed", details={"kind": "headed_required"})
    s = _sys()
    opened = _nc_session(ctx)
    try:
        el, row = _pick(index, expected_app, expected_title)
        if app and app.casefold() not in (row["app_name"] or "").casefold():
            raise CuopError("NOTIFICATION_NOT_FOUND", f"notification #{index} is not from {app!r}")
        act = Act("dismiss-notification")
        if not act.ax(s.perform_custom(el, s.CLOSE_NAMES), "dismiss") and not act.ax(
            _io().perform(el, "AXDismiss"), "dismiss"
        ):
            raise act.fail("ACTION_NOT_SUPPORTED", "no dismiss path on that notification", kind="no_dismiss_action")
        if s.wait_until(lambda: _fp(row) not in [_fp(r) for _, r in s.notifications()], 2.0):
            act.delivery = "delivered_verified"
        act.extra["dismissed"] = {"index": index, "app_name": row["app_name"]}
        return act.data()
    finally:
        _nc_close(opened)


def dismiss_all_notifications(ctx: Ctx, app: str | None = None) -> dict[str, Any]:
    if not ctx.headed:
        raise CuopError("POLICY_DENIED", "notification mutations need --headed", details={"kind": "headed_required"})
    s = _sys()
    opened = _nc_session(ctx)
    done, failures = 0, list[dict[str, Any]]()
    act = Act("dismiss-all-notifications")

    def mine() -> list[tuple[Any, dict[str, Any]]]:
        return [(el, r) for el, r in s.notifications() if not app or app.casefold() in (r["app_name"] or "").casefold()]

    try:
        targets = [_fp(r) for _, r in mine()]  # 처음 본 알림만 — 지우는 사이 새로 온 알림은 건드리지 않는다
        for fp in targets[:50]:
            el = next((e for e, r in mine() if _fp(r) == fp), None)
            if el is None:
                continue  # 이미 사라짐(묶음이 함께 지워짐 등)
            if not act.ax(s.perform_custom(el, s.CLOSE_NAMES), "dismiss"):
                failures.append({"app_name": fp[0], "reason": "no_dismiss_action"})
                continue
            if s.wait_until(lambda: fp not in [_fp(r) for _, r in mine()], 2.0):  # noqa: B023 — 바로 기다린다
                done += 1
            else:
                failures.append({"app_name": fp[0], "reason": "still_present"})
    finally:
        _nc_close(opened)
    if done and not failures:
        act.delivery = "delivered_verified"
    act.extra.update(dismissed_count=done, failures=failures, failed_count=len(failures))
    return act.data()


def notification_action(ctx: Ctx, index: int, action: str, expected_app: str | None = None,
                        expected_title: str | None = None) -> dict[str, Any]:  # fmt: skip
    if not ctx.headed:
        raise CuopError("POLICY_DENIED", "notification mutations need --headed", details={"kind": "headed_required"})
    s = _sys()
    opened = _nc_session(ctx)
    try:
        el, row = _pick(index, expected_app, expected_title)
        if action not in row["actions"]:
            raise CuopError("ACTION_NOT_SUPPORTED", f"notification #{index} has no action {action!r}",
                            details={"actions": row["actions"]})  # fmt: skip
        act = Act("notification-action")
        if not act.ax(s.perform_custom(el, action), "custom_action"):
            raise act.fail("ACTION_NOT_SUPPORTED", f"action {action!r} is no longer offered", kind="action_absent")
        return act.data()
    finally:
        _nc_close(opened)
