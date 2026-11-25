# pyright: basic
# pyright: reportAttributeAccessIssue=false
"""HUD 렌더러 — `python -m cuop.overlay.macos <socket>`. 클릭이 통과하고, 앞으로 나오지 않고, 화면 캡처에 찍히지 않는다.

- 오른쪽 위 패널: 단계 번호 · 명령 · 대상(role · 이름) · 결과, 최근 단계 5줄.
- 대상 강조: 대상 영역에 색 테두리 창을 잠깐(1.2초) 띄운다.
- accessory 앱(Dock 아이콘 없음) · 비활성 패널 · ignoresMouseEvents · sharingType none(스크린샷에 안 찍힘).
- 10분 동안 이벤트가 없으면 끝난다. 이벤트 = JSON 데이터그램 {kind, phase, step, command, label, bounds, ok, code}.
"""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSTextField,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from PyObjCTools import AppHelper

IDLE_EXIT = 600
W, LINE = 380, 18
ACCENT = (0.25, 0.55, 1.0)


class _Rounded(NSView):
    """둥근 사각형을 직접 그린다(CALayer 색은 PyObjC에서 CGColor 포인터라 쓰지 않는다)."""

    def initWithFrame_fill_stroke_(self, frame, fill, stroke):  # noqa: N802 — Objective-C 선택자
        self = objc.super(_Rounded, self).initWithFrame_(frame)
        if self is not None:
            self.fill, self.stroke = fill, stroke
        return self

    def drawRect_(self, rect):  # noqa: N802
        b = self.bounds()
        inner = ((b.origin.x + 2, b.origin.y + 2), (b.size.width - 4, b.size.height - 4))
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inner, 9, 9)
        if self.fill is not None:
            self.fill.setFill()
            path.fill()
        if self.stroke is not None:
            self.stroke.setStroke()
            path.setLineWidth_(3)
            path.stroke()


def _screen_h() -> float:
    return NSScreen.screens()[0].frame().size.height


def _panel(rect) -> NSPanel:
    p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered, False
    )
    p.setLevel_(NSStatusWindowLevel)
    p.setOpaque_(False)
    p.setBackgroundColor_(NSColor.clearColor())
    p.setIgnoresMouseEvents_(True)
    p.setHasShadow_(False)
    p.setHidesOnDeactivate_(False)
    p.setCanHide_(False)
    p.setSharingType_(0)  # NSWindowSharingNone — 화면 캡처 · 녹화에 나오지 않는다
    p.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorIgnoresCycle | NSWindowCollectionBehaviorFullScreenAuxiliary
    )  # fmt: skip
    return p


def _label(text: str, y: float, size: float, alpha: float = 1.0, bold: bool = False) -> NSTextField:
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(12, y, W - 24, LINE + 2))
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha))
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.monospacedSystemFontOfSize_weight_(size, 0.0))
    f.cell().setLineBreakMode_(4)  # 끝을 …로
    return f


class HUD:
    def __init__(self) -> None:
        self.recent: list[str] = []
        self.last = time.monotonic()
        frame = NSScreen.screens()[0].visibleFrame()
        h = LINE * 8 + 16
        self.panel = _panel(NSMakeRect(frame.origin.x + frame.size.width - W - 12,
                                       frame.origin.y + frame.size.height - h - 12, W, h))  # fmt: skip
        self.box = _Rounded.alloc().initWithFrame_fill_stroke_(
            NSMakeRect(0, 0, W, h), NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.82), None
        )
        self.panel.setContentView_(self.box)
        self.head = _label("CUOP · waiting for the agent", h - LINE - 10, 12, bold=True)
        self.now = _label("", h - 2 * LINE - 14, 12)
        self.rows = [_label("", h - (3 + i) * LINE - 18, 11, 0.6) for i in range(5)]
        for v in (self.head, self.now, *self.rows):
            self.box.addSubview_(v)
        self.mark = _panel(NSMakeRect(0, 0, 10, 10))
        mv = _Rounded.alloc().initWithFrame_fill_stroke_(
            NSMakeRect(0, 0, 10, 10),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*ACCENT, 0.12),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*ACCENT, 0.95),
        )
        self.mark.setContentView_(mv)
        self.mark_until = 0.0
        self.panel.orderFrontRegardless()  # 활성화하지 않고 앞에 둔다

    def handle(self, msg: dict) -> None:
        self.last = time.monotonic()
        if msg.get("kind") == "quit":
            AppHelper.stopEventLoop()
            return
        step, cmd, label = msg.get("step"), msg.get("command", "?"), msg.get("label") or ""
        if msg.get("phase") == "start":
            self.head.setStringValue_(f"CUOP · step {step}" + (f" · {msg['session']}" if msg.get("session") else ""))
            self.now.setStringValue_(f"▶ {cmd} {label}")
            if b := msg.get("bounds"):
                self._highlight(b)
            return
        badge = "✓" if msg.get("ok") else f"✗ {msg.get('code') or ''}"
        line = f"{step:>3} {badge} {cmd} {label}"
        self.now.setStringValue_(f"{badge} {cmd} {label}" + (f"  {msg['ms']} ms" if msg.get("ms") else ""))
        self.recent = [line, *self.recent][:5]
        for row, text in zip(self.rows, [*self.recent, *[""] * 5], strict=False):
            row.setStringValue_(text)
        self.panel.orderFrontRegardless()

    def _highlight(self, b: dict) -> None:
        pad = 4
        x, y, w, h = (
            b["x"] - pad,
            b["y"] - pad,
            b.get("width", b.get("w", 0)) + 2 * pad,
            b.get("height", b.get("h", 0)) + 2 * pad,
        )
        self.mark.setFrame_display_(NSMakeRect(x, _screen_h() - y - h, w, h), True)  # AX(왼쪽 위 원점) → Cocoa
        self.mark.contentView().setFrame_(NSMakeRect(0, 0, w, h))
        self.mark.orderFrontRegardless()
        self.mark_until = time.monotonic() + 1.2

    def tick(self) -> None:
        if self.mark_until and time.monotonic() > self.mark_until:
            self.mark.orderOut_(None)
            self.mark_until = 0.0
        if time.monotonic() - self.last > IDLE_EXIT:
            AppHelper.stopEventLoop()
            return
        AppHelper.callLater(0.2, self.tick)


def main() -> None:
    path = Path(sys.argv[1])
    if path.exists():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    os.chmod(path, 0o600)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory: Dock · 앱 전환기에 없음, 활성화하지 않는다
    hud = HUD()

    def pump() -> None:
        while True:
            data = sock.recv(65536)
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            AppHelper.callAfter(hud.handle, msg)

    threading.Thread(target=pump, daemon=True).start()
    AppHelper.callLater(0.2, hud.tick)
    try:
        AppHelper.runEventLoop(installInterrupt=True, pdb=False)
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
