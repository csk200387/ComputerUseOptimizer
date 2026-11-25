"""오버레이 HUD 클라이언트(OS 무관) — 명령이 단계 이벤트를 유닉스 데이터그램 소켓으로 보낸다. 실패해도 명령은 그대로.

켜기: `cuop cursor-overlay enable`(세션이면 그 세션, 아니면 전역) · `session start --cursor` · `CUOP_OVERLAY=1`.
렌더러(`cuop overlay serve`, macOS AppKit)는 처음 이벤트를 보낼 때 백그라운드로 띄운다.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cuop.desktop.store import Namespace, state_root


def sock_path() -> Path:
    """AF_UNIX 경로는 104바이트 제한 → 상태 루트의 해시로 짧게(/tmp, 사용자별)."""
    import hashlib

    h = hashlib.sha1(str(state_root()).encode()).hexdigest()[:10]
    d = Path("/tmp") / f"cuop-{os.getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    st = d.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:  # 남이 만든 폴더 · 열린 권한이면 쓰지 않는다
        raise OSError(f"unsafe overlay socket directory {d}")
    return d / f"{h}.sock"


def _flag() -> Path:
    return state_root() / "overlay.json"


def enabled(ns: Namespace) -> bool:
    if os.environ.get("CUOP_OVERLAY") in ("1", "on", "true"):
        return True
    if os.environ.get("CUOP_OVERLAY") in ("0", "off", "false"):
        return False
    m = ns.manifest
    if m is not None and "overlay" in m:
        return bool(m["overlay"])
    try:
        return bool(json.loads(_flag().read_text()).get("enabled"))
    except OSError, ValueError:
        return False


def set_enabled(ns: Namespace, on: bool) -> dict[str, Any]:
    m = ns.manifest
    if m is not None:
        m["overlay"] = on
        ns._write("session.json", m)  # pyright: ignore[reportPrivateUsage]
        scope = "session"
    else:
        _flag().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _flag().write_text(json.dumps({"enabled": on}))
        scope = "global"
    if not on:
        send({"kind": "quit"}, spawn=False)
    return {"enabled": on, "scope": scope, "rendered": on and ensure_running()}


def ensure_running(wait: float = 3.0) -> bool:
    """렌더러가 없으면 띄운다(분리된 세션, 앞으로 나오지 않음). 소켓이 생기면 True."""
    if _alive():
        return True
    if sys.platform != "darwin" or wait <= 0:
        return False
    failed = state_root() / "overlay.failed"
    if failed.exists() and time.time() - failed.stat().st_mtime < 60:
        return False  # 방금 못 띄웠다 — 명령마다 기다리지 않게 1분 쉬었다 다시
    log = state_root() / "overlay.log"
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log.open("ab") as out:
        subprocess.Popen(  # noqa: S603 — 자기 자신(python -m cuop.overlay.macos)
            [sys.executable, "-m", "cuop.overlay.macos", str(sock_path())],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )  # fmt: skip
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _alive():
            failed.unlink(missing_ok=True)
            return True
        time.sleep(0.05)
    failed.touch()
    return False


def _alive() -> bool:
    p = sock_path()
    if not p.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(str(p))
        return True
    except OSError:
        return False


def send(msg: dict[str, Any], spawn: bool = True) -> bool:
    """이벤트 하나. 렌더러가 없으면(spawn이면 띄운 뒤) 보낸다. 어떤 실패도 명령을 막지 않는다."""
    try:
        if spawn and not ensure_running():
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps(msg, ensure_ascii=False).encode()[:60000], str(sock_path()))
        return True
    except OSError:
        return False
