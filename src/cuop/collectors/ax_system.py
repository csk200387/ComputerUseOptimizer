# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""macOS 앱 · 창 · 클립보드 · 화면 캡처 원시 연산 (desktop/system.py가 정책 · 결과 모양을 맡는다)."""

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from AppKit import NSURL, NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeString, NSRunningApplication, NSWorkspace

from cuop.collectors.ax import App, attr, running_apps
from cuop.collectors.ax_actions import guard, perform, set_attr
from cuop.desktop.errors import CuopError

APP_DIRS = ("/Applications", "/System/Applications", "/System/Applications/Utilities",
            str(Path.home() / "Applications"), "/System/Library/CoreServices")  # fmt: skip
# 끄면 데스크톱 세션이 깨지는 프로세스 — close-app 거부(번들 ID 마지막 요소 또는 이름, 정확히 일치)
PROTECTED = frozenset({"finder", "dock", "systemuiserver", "loginwindow", "windowserver", "controlcenter",
                       "notificationcenter", "spotlight", "coreservicesuiagent", "launchd"})  # fmt: skip


def app_path(query: str) -> str:
    """표시 이름 · 번들 ID · .app 이름 → 번들 경로."""
    ws = NSWorkspace.sharedWorkspace()
    if query.endswith(".app") and Path(query).is_dir():
        return str(Path(query).resolve())
    if "." in query and (url := ws.URLForApplicationWithBundleIdentifier_(query)) is not None:
        return str(url.path())
    q = query.lower().removesuffix(".app")
    for d in APP_DIRS:
        for p in sorted(Path(d).glob("*.app")) if Path(d).is_dir() else ():
            if p.stem.lower() == q:
                return str(p)
    for d in APP_DIRS:  # 현지화 이름('계산기')
        for p in sorted(Path(d).glob("*.app")) if Path(d).is_dir() else ():
            name = ws.localizedNameForFile_(str(p)) if hasattr(ws, "localizedNameForFile_") else None
            if name and str(name).lower().removesuffix(".app") == q:
                return str(p)
    raise CuopError("APP_NOT_FOUND", f"no application named {query!r} in {', '.join(APP_DIRS[:4])}")


def bundle_id(path: str) -> str | None:
    from Foundation import NSBundle

    b = NSBundle.bundleWithPath_(path)
    return str(b.bundleIdentifier()) if b is not None and b.bundleIdentifier() else None


def open_app(path: str, *, activate: bool, new: bool, args: list[str], env: dict[str, str], cwd: str | None,
             timeout: float = 30) -> None:  # fmt: skip
    """LaunchServices로 연다 — 기본은 앞으로 가져오지 않음(`open -g`). timeout = launch에 남은 시간(초)."""
    cmd = ["open"] + ([] if activate else ["-g"]) + (["-n"] if new else [])
    for k, v in env.items():
        cmd += ["--env", f"{k}={v}"]
    cmd += ["-a", path] + (["--args", *args] if args else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=max(timeout, 0.05))
    except subprocess.TimeoutExpired:
        raise CuopError("TIMEOUT", "open did not return within --timeout", delivery="unknown",
                        details={"kind": "launch_timeout"}) from None  # fmt: skip
    if r.returncode:
        raise CuopError("ACTION_FAILED", f"open failed: {r.stderr.strip()[:200]}", delivery="unknown")


def instances(bid: str | None, path: str) -> list[App]:
    return [a for a in running_apps() if (bid and a.bundle_id == bid) or a.path == path]


def protected(app: App) -> bool:
    keys = {app.name.lower(), (app.executable or "").lower(), (app.bundle_id or "").rsplit(".", 1)[-1].lower()}
    return bool(keys & PROTECTED)


def terminate(pid: int, force: bool) -> bool:
    guard(pid)
    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if ra is None or ra.isTerminated():
        return False
    return bool(ra.forceTerminate() if force else ra.terminate())


def alive(pid: int) -> bool:
    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return ra is not None and not ra.isTerminated()


# --- 창 ---
def window_geometry(win: Any) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    return attr(win, "AXPosition"), attr(win, "AXSize")


def set_geometry(win: Any, *, pos: tuple[float, float] | None = None, size: tuple[float, float] | None = None) -> int:
    from ApplicationServices import AXValueCreate, kAXValueCGPointType, kAXValueCGSizeType

    err = 0
    if size is not None:
        err = set_attr(win, "AXSize", AXValueCreate(kAXValueCGSizeType, size))
    if pos is not None and not err:
        err = set_attr(win, "AXPosition", AXValueCreate(kAXValueCGPointType, pos))
    return err


def visible_frame() -> tuple[float, float, float, float]:
    """주 화면에서 메뉴 막대 · Dock을 뺀 영역(AX 좌표: 왼쪽 위 원점)."""
    from AppKit import NSScreen

    main = NSScreen.screens()[0]
    vf, full = NSScreen.mainScreen().visibleFrame(), main.frame()
    return vf.origin.x, full.size.height - (vf.origin.y + vf.size.height), vf.size.width, vf.size.height


def window_button(win: Any, subrole: str) -> Any:
    return next((c for c in attr(win, "AXChildren") or () if attr(c, "AXSubrole") == subrole), None)


def press_button(el: Any) -> int:
    return perform(el, "AXPress")


# --- 클립보드 ---
def clipboard_read(fmt: str, out: str | None) -> dict[str, Any]:
    pb = NSPasteboard.generalPasteboard()
    order = ["file-urls", "image", "text"] if fmt == "auto" else [fmt]
    for kind in order:
        if kind == "text" and (s := pb.stringForType_(NSPasteboardTypeString)) is not None:
            return {"type": "text", "text": str(s)}
        if kind == "file-urls":
            urls = pb.readObjectsForClasses_options_([NSURL], {"NSPasteboardURLReadingFileURLsOnlyKey": True}) or []
            if urls:
                return {"type": "file_urls", "file_urls": [str(u.path()) for u in urls]}
        if kind == "image" and (data := pb.dataForType_(NSPasteboardTypePNG)) is not None:
            path = Path(out) if out else Path(tempfile.mkstemp(prefix="cuop-clip-", suffix=".png")[1])
            path.write_bytes(bytes(data))
            from AppKit import NSImage

            img = NSImage.alloc().initWithData_(data)
            w, h = (img.size().width, img.size().height) if img is not None else (0, 0)
            return {"type": "image", "path": str(path), "width": round(w), "height": round(h)}
    return {"type": {"file-urls": "file_urls"}.get(fmt, fmt), "found": False}


def clipboard_write(text: str | None = None, image: str | None = None, files: list[str] | None = None) -> bool:
    pb = NSPasteboard.generalPasteboard()
    png = None
    if image:  # 지우기 전에 한 번 읽어 검사하고 그 버퍼를 쓴다 — 파일이 그사이 바뀌어도 클립보드만 비지 않게(감사 #22)
        with Path(image).open("rb") as f:
            png = f.read((64 << 20) + 1)
        if len(png) > 64 << 20 or not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 24:
            raise CuopError("INVALID_ARGS", "--image must be a PNG file of at most 64 MiB")
    pb.clearContents()
    if files:
        return bool(pb.writeObjects_([NSURL.fileURLWithPath_(f) for f in files]))
    if png is not None:
        from Foundation import NSData

        return bool(pb.setData_forType_(NSData.dataWithBytes_length_(png, len(png)), NSPasteboardTypePNG))
    if text is not None:
        return bool(pb.setString_forType_(text, NSPasteboardTypeString))
    return True


def clipboard_text() -> str | None:
    s = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
    return str(s) if s is not None else None


# --- 화면 캡처 ---
def capture(path: str, *, window_id: int | None = None, rect: tuple[int, int, int, int] | None = None) -> None:
    """screencapture(OS 기본 도구) — 화면 기록 권한이 필요하다."""
    cmd = ["screencapture", "-x", "-t", "png"]
    if window_id is not None:
        cmd += ["-o", f"-l{window_id}"]
    elif rect is not None:
        cmd += [f"-R{rect[0]},{rect[1]},{rect[2]},{rect[3]}"]
    r = subprocess.run([*cmd, path], capture_output=True, timeout=15)
    if r.returncode or png_size(path) == (0, 0):  # 미리 만든 빈 임시 파일이 남아 있어도 성공으로 보지 않는다
        raise CuopError("ACTION_FAILED", "screencapture failed", platform_detail=r.stderr.decode()[:200])


def shrink(path: str, width: int, height: int) -> None:
    """sips(OS 기본 도구)로 PNG를 width × height로 줄인다."""
    r = subprocess.run(["sips", "-z", str(height), str(width), path], capture_output=True, timeout=15)
    if r.returncode or png_size(path) != (width, height):
        raise CuopError("ACTION_FAILED", "sips resize failed", platform_detail=r.stderr.decode()[:200])


def png_size(path: str) -> tuple[int, int]:
    """PNG 머리(시그니처 + IHDR)의 가로 · 세로. PNG가 아니면 (0, 0)."""
    try:
        head = Path(path).read_bytes()[:24]
    except OSError:
        return 0, 0
    if not head.startswith(b"\x89PNG\r\n\x1a\n") or len(head) < 24:
        return 0, 0
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def wait_until(done: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.1)
    return bool(done())


# --- 알림 센터 ---
NC_BUNDLE = "com.apple.notificationcenterui"
CLOSE_NAMES = frozenset({"close", "clear", "dismiss", "닫기", "지우기", "삭제"})


def _custom(el: Any) -> list[tuple[str, str]]:
    """사용자 정의 AX 액션 'Name:<표시 이름>\\nTarget:…' → (표시 이름, 원래 액션 문자열)."""
    from ApplicationServices import AXUIElementCopyActionNames

    err, names = AXUIElementCopyActionNames(el, None)
    out = []
    for raw in names or () if not err else ():
        raw = str(raw)
        if raw.startswith("Name:"):
            out.append((raw[5:].split("\n", 1)[0].strip(), raw))
    return out


def notifications() -> list[tuple[Any, dict[str, Any]]]:
    """화면에 있는 알림(배너 · 알림 센터 목록). (요소, {app_name, title, body, actions}) 앞→뒤 순."""
    from ApplicationServices import AXUIElementCreateApplication

    from cuop.collectors.ax import find_app

    nc = find_app(NC_BUNDLE)
    out: list[tuple[Any, dict[str, Any]]] = []
    queue = list(attr(AXUIElementCreateApplication(nc.pid), "AXWindows") or ())
    while queue:
        el = queue.pop(0)
        sub = attr(el, "AXSubrole")
        if isinstance(sub, str) and sub.startswith("AXNotificationCenter") and sub != "AXNotificationCenterAlertStack":
            texts = [
                t
                for t in (
                    attr(c, "AXValue") for c in attr(el, "AXChildren") or () if attr(c, "AXRole") == "AXStaticText"
                )
                if isinstance(t, str)
            ]
            desc = str(attr(el, "AXDescription") or "")
            app = desc.split(",", 1)[0].strip() if desc else None
            row = {"app_name": app, "title": texts[0] if texts else None, "body": " ".join(texts[1:]) or None,
                   "actions": [n for n, _ in _custom(el) if n.lower() not in CLOSE_NAMES]}  # fmt: skip
            out.append((el, row))
            continue
        queue.extend(attr(el, "AXChildren") or ())
    return out


def center_open() -> bool:
    from ApplicationServices import AXUIElementCreateApplication

    from cuop.collectors.ax import find_app

    nc = find_app(NC_BUNDLE)
    return any(
        attr(w, "AXSubrole") == "AXSystemDialog" for w in attr(AXUIElementCreateApplication(nc.pid), "AXWindows") or ()
    )


def toggle_center() -> int:
    """메뉴 막대 시계를 눌러 알림 센터를 열고 닫는다(AXPress — 포인터는 움직이지 않음)."""
    from ApplicationServices import AXUIElementCreateApplication

    from cuop.collectors.ax import find_app

    cc = find_app("com.apple.controlcenter")
    bar = attr(AXUIElementCreateApplication(cc.pid), "AXExtrasMenuBar")
    clock = next(
        (c for c in attr(bar, "AXChildren") or () if attr(c, "AXIdentifier") == "com.apple.menuextra.clock"), None
    )
    return -1 if clock is None else perform(clock, "AXPress")


def perform_custom(el: Any, names: frozenset[str] | str) -> int | None:
    """표시 이름이 맞는 사용자 정의 액션을 실행. 없으면 None."""
    want = {names.lower()} if isinstance(names, str) else names
    for name, raw in _custom(el):
        if name.lower() in want:
            return perform(el, raw)
    return None
