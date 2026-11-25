"""설치 앱 커버리지 probe — 앱마다 트리를 읽어 '쓸 만한가'를 숫자로만 기록한다 (BRIEF §3-4).

`uv run python benchmarks/coverage.py [--only 이름…]` → logs/coverage.jsonl(앱마다 한 줄) → `--report`가
docs/parity/COVERAGE.md를 만든다.

- 분모: /Applications · /System/Applications · /System/Applications/Utilities · ~/Applications 의 최상위 .app 전부.
- 제외(이유 기록): LSBackgroundOnly(화면이 없는 앱), 설치 · 제거 · 설정 도우미(실행 자체가 시스템을 바꿀 수 있음),
  대화형 캡처 UI(Screenshot.app — 무인 실행 불가). 재는 동안 새로 생긴 screencapture 프로세스는 끝낸다.
- 읽기 전용: 클릭 · 입력 없음. 꺼져 있던 앱만 백그라운드로 띄우고(`open -g`) 끝나면 그 앱만 종료한다.
- 개인 정보: 트리는 메모리에서 개수 · 토큰만 세고 버린다. 상태 폴더는 앱마다 새 임시 폴더 → 그 앱을 재자마자 지움.
- 쓸 만함(엄격, 기본 보고) = 스냅샷 성공 · 에이전트가 받은 텍스트 줄에 이름이 찍힌(또는 바로 아래 두 단계 안의
  자식 글자로 이름이 보이는 — 목록 칸 · 행) 조작 가능 ref ≥ 5
  (창 전체를 skeleton 없이 받았고 그 안의 조작 가능 ref가 모두 이름이 있으면 5개 미만도) · 텍스트 토큰 < 스크린샷 토큰.
- 느슨한 기준(함께 보고) = refmap 전체에서 셈, 내용 칸(textbox · scrollarea)은 이름 없어도 셈.
"""
# pyright: basic

import json
import math
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DIRS = ["/Applications", "/System/Applications", "/System/Applications/Utilities", str(Path.home() / "Applications")]
UNSAFE = re.compile(r"install|uninstall|setup assistant|boot camp assistant|migration assistant", re.I)
OUT = Path("logs/coverage.jsonl")
LINE = re.compile(r'^\s*(e\d+) \S+( ")?')  # 텍스트 줄 `eN role "이름" …` — 이름이 찍혔나
WAIT = 12.0


def shot_tokens(w: float, h: float) -> int:
    if w <= 0 or h <= 0:
        return 0
    s = min(1.0, 1568 / max(w, h), math.sqrt(1_150_000 / (w * h)))
    return round(w * s * h * s / 750)


def apps() -> list[Path]:
    out: list[Path] = []
    for d in DIRS:
        if Path(d).is_dir():
            out += sorted(p for p in Path(d).iterdir() if p.suffix == ".app")
    return out


def info(app: Path) -> dict[str, Any]:
    try:
        return plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except OSError, plistlib.InvalidFileException, ValueError:
        return {}


# 띄우면 무인으로 다룰 수 없는 앱(번들 ID): Screenshot.app은 SystemUIServer가 대화형 캡처 오버레이를 열어
# 클릭을 가로채고, 앱 프로세스가 아니라 probe가 닫지 못한다(2026-11-21, 16분 동안 남음 — DECISIONS D12)
INTERACTIVE = {"com.apple.screenshot.launcher": "interactive capture overlay — cannot run unattended"}


def exclusion(app: Path, plist: dict[str, Any]) -> str | None:
    if plist.get("LSBackgroundOnly") in (True, 1, "1", "YES"):
        return "LSBackgroundOnly — no GUI by design"
    if reason := INTERACTIVE.get(str(plist.get("CFBundleIdentifier"))):
        return reason
    if UNSAFE.search(app.stem):
        return "installer / setup assistant — unsafe to launch unattended"
    return None


def _shown_names(text: str, actionable: set[str]) -> dict[str, bool]:
    """받은 텍스트의 eN → 이름이 보이는가. 이름 없는 목록 칸 · 행은 바로 아래(두 단계 안) 글자의 이름을 쓰되,
    그 글자는 가장 가까운 조작 가능 조상 하나에만 센다 — 행과 칸이 같은 글자로 둘 다 세지지 않게(Codex 리뷰)."""
    rows: list[tuple[int, str, str, bool]] = []
    for line in text.splitlines()[1:]:
        if m := LINE.match(line):
            parts = line.split()
            rows.append((len(line) - len(line.lstrip(" ")), m[1], parts[1] if len(parts) > 1 else "", bool(m[2])))
    out = {eid: named for _, eid, _, named in rows}
    for i, (depth, _, role, named) in enumerate(rows):
        if role not in ("text", "heading") or not named or rows[i][1] in actionable:  # 스스로 조작 가능하면 자기만
            continue
        level = depth
        for j in range(i - 1, -1, -1):  # 위로 가며 가장 가까운 조상(글자에서 두 단계 안)
            d, eid, _, _ = rows[j]
            if d >= level:
                continue
            if depth - d > 2:
                break
            if eid in actionable:
                out[eid] = True
                break
            level = d  # 조작 불가 조상(그룹)을 지나 한 단계 더 위로
    return out


def _captures() -> set[int]:
    r = subprocess.run(["pgrep", "-x", "screencapture"], capture_output=True, text=True)
    return {int(x) for x in r.stdout.split()}


def _kill_new_captures(before: set[int]) -> None:
    """재는 동안 새로 생긴 screencapture(앱이 띄운 대화형 캡처 UI)를 끝낸다 — 화면에 남아 클릭을 가로채지 않게."""
    import signal

    for pid in _captures() - before:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def probe(app: Path, dpr: float) -> dict[str, Any]:
    from cuop.collectors import ax, ax_system
    from cuop.desktop import commands as C
    from cuop.desktop.errors import CuopError
    from cuop.desktop.text import tokens

    plist = info(app)
    bid = plist.get("CFBundleIdentifier")
    row: dict[str, Any] = {"app": app.stem, "dir": str(app.parent), "bundle_id": bid}
    if reason := exclusion(app, plist):
        return row | {"status": "excluded", "reason": reason}
    row["menu_bar_only"] = plist.get("LSUIElement") in (True, 1, "1", "YES")
    row["renderer"] = ax.renderer(ax.App(0, app.stem, bid, 0, str(app), None)) or "native"
    before = {a.pid for a in ax_system.instances(bid, str(app))}
    row["was_running"] = bool(before)
    launched: list[int] = []
    captures = _captures()  # 이 앱을 재기 전부터 있던 screencapture는 건드리지 않는다
    try:
        if not before:
            subprocess.run(["open", "-g", "-a", str(app)], capture_output=True, timeout=20)
        deadline, pid = time.monotonic() + WAIT, None
        while time.monotonic() < deadline:
            now = ax_system.instances(bid, str(app))
            if now:
                pid = now[0].pid
                launched = [a.pid for a in now if a.pid not in before]
                if ax.app_windows(pid) or row["menu_bar_only"]:
                    break
            time.sleep(0.5)
        if pid is None:
            return row | {"status": "fail", "reason": "process did not start"}
        time.sleep(1.0 if not launched else 2.0)  # 첫 화면이 그려질 시간
        ctx = C.Ctx(fmt="text")
        data, err = None, None
        for attempt in range(3):
            try:
                running = ax.app_by_pid(pid)
                target = bid if bid and ax_system.instances(bid, "") else (running.name if running else app.stem)
                data = C.snapshot(ctx, app=target, timeout_ms=6000)  # Info.plist와 실행 중 번들 ID가 다르면 이름으로
            except CuopError as e:
                err = e
                cands = (e.details or {}).get("candidates") or []
                if e.code == "WINDOW_NOT_FOUND":  # 다른 데스크톱 · 가려진 창 — AX에는 있으니 그 창을 직접
                    hidden = [w for w in ax.app_windows(pid) if w.id]
                    cands = [{"id": hidden[0].ref_id}] if hidden else []
                if e.code in ("AMBIGUOUS_TARGET", "WINDOW_NOT_FOUND") and cands and cands[0].get("id"):
                    try:
                        data = C.snapshot(ctx, window_id=cands[0]["id"], timeout_ms=6000)
                    except CuopError as e2:
                        err = e2
            if data is not None and (data["ref_count"] >= 5 or attempt == 2):
                break
            time.sleep(3.0)  # Chromium · Electron은 접근성을 켠 뒤 트리가 늦게 찬다
        if data is None:
            return row | {"status": "fail", "reason": f"{err.code}: {err.message[:80]}" if err else "no snapshot"}
        ns = ctx.ns
        snap = ns.load(data["snapshot_id"])
        acts = [r for r in snap.refs.values() if r.actions or r.role in C.INTERACTIVE_ROLES]
        named = [r for r in acts if r.name or r.role in ("textbox", "scrollarea")]
        text = data.get("text", "")
        text_t = tokens(text)
        head = text.split("\n", 1)[0]
        skeleton = "skeleton" in head or "(filtered:" in head  # 창 전체를 받지 못한 뷰
        # 엄격 기준(감사 #10): 에이전트가 실제로 받은 텍스트 줄에 이름이 찍힌 조작 가능 ref만 — 내용 칸 예외 없음
        acts_all = {eid for eid, r in snap.refs.items() if r.actions or r.role in C.INTERACTIVE_ROLES}
        shown = _shown_names(text, acts_all)
        strict = [r for eid, r in snap.refs.items() if shown.get(eid) and (r.actions or r.role in C.INTERACTIVE_ROLES)]
        acts_shown = [
            eid for eid, r in snap.refs.items() if eid in shown and (r.actions or r.role in C.INTERACTIVE_ROLES)
        ]
        row |= {"ref_count": data["ref_count"], "named_refs": len(named), "strict_named": len(strict),
                "complete": data["complete"], "text_tokens": text_t, "skeleton": skeleton}  # fmt: skip
        b = None
        if snap.window_id is not None:
            b = next((w.bounds for w in ax.app_windows(snap.pid) if w.id == snap.window_id), None)
        elif snap.refs:  # 창 없는 앱(메뉴 막대 앱): 그 surface 요소들의 영역을 합친다
            boxes = [r.bounds for r in snap.refs.values() if r.bounds and r.bounds.w > 0]
            if boxes:
                x0, y0 = min(r.x for r in boxes), min(r.y for r in boxes)
                x1, y1 = max(r.x + r.w for r in boxes), max(r.y + r.h for r in boxes)
                b = type(boxes[0])(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
        if b is None:
            return row | {"status": "fail", "reason": "no bounds for the observed surface"}
        shot = shot_tokens(b.w * dpr, b.h * dpr)
        row |= {"size": f"{b.w}x{b.h}", "shot_tokens": shot, "shot_tokens_1x": shot_tokens(b.w, b.h)}
        enough = len(named) >= 5 or (bool(acts) and len(named) == len(acts))  # 창 전체가 그보다 작으면 전부
        cheap = text_t < shot
        whole = data["complete"] and not skeleton  # 작은 창 예외는 창 전체를 받았을 때만
        strict_ok = len(strict) >= 5 or (whole and bool(acts_shown) and len(strict) == len(acts_shown))
        row["strict"] = "usable" if strict_ok and cheap else "fail"
        row["status"] = "usable" if enough and cheap else "fail"
        if not enough:
            row["reason"] = f"only {len(named)} named actionable refs"
        elif not cheap:
            row["reason"] = "text tokens >= screenshot tokens"
        return row
    finally:
        for pid in launched:  # 이 probe가 띄운 앱만 끈다
            ax_system.terminate(pid, force=False)
            if not ax_system.wait_until(lambda p=pid: not ax_system.alive(p), 5):
                ax_system.terminate(pid, force=True)
        _kill_new_captures(captures)


def run(only: list[str]) -> None:
    from cuop.collectors import ax

    dpr = float(ax.displays()[0]["scale"])
    os.environ["CUOP_OVERLAY"] = "0"
    OUT.parent.mkdir(exist_ok=True)
    done = set()
    if OUT.exists() and not only:
        done = {json.loads(line)["app"] for line in OUT.read_text().splitlines() if line.strip()}
    for app in apps():
        if (only and app.stem not in only) or app.stem in done:
            continue
        t0 = time.monotonic()
        row: dict[str, Any]
        # 앱마다 새 임시 상태 폴더 → 그 앱을 재자마자 지운다(refmap · 뷰에 이름이 있으므로 디스크에 남기지 않게)
        with tempfile.TemporaryDirectory(prefix="cuop-coverage-") as tmp:
            os.environ["CUOP_HOME"] = tmp
            try:
                row = probe(app, dpr)
            except Exception as e:  # noqa: BLE001 — 한 앱의 실패가 전체를 멈추지 않게
                row = {"app": app.stem, "status": "fail", "reason": f"probe error {type(e).__name__}: {str(e)[:80]}"}
        row["seconds"] = round(time.monotonic() - t0, 1)
        with OUT.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{row['app'][:30]:30} {row.get('strict', row['status']):8} {row.get('reason', '')}", flush=True)


def report() -> None:
    rows = {}
    for line in OUT.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["app"]] = r  # 같은 앱을 다시 재면 마지막 값
    rows = dict(sorted(rows.items(), key=lambda kv: kv[0].lower()))
    total = len(apps())
    excl = [r for r in rows.values() if r["status"] == "excluded"]
    usable = [r for r in rows.values() if r["status"] == "usable"]
    strict = [r for r in rows.values() if r.get("strict") == "usable"]
    base = total - len(excl)
    lines = [
        "# 설치 앱 커버리지 (macOS)",
        "",
        f"측정: `uv run python benchmarks/coverage.py` → `--report` ({time.strftime('%Y-%m-%d %H:%M')} KST, 이 Mac).",
        "쓸 만함 = 스냅샷 성공 · 이름 있는 조작 가능 ref ≥ 5(작은 창은 전부) · CUOP 텍스트 토큰 < 그 창 스크린샷 토큰"
        "(기기 픽셀 → 긴 변 1568 · 1.15MP 축소 후 w·h/750). 토큰 = tiktoken o200k. 숫자만 기록(트리 내용 저장 안 함).",
        "",
        f"- 분모(최상위 .app): **{total}**, 제외 {len(excl)} → 대상 **{base}**",
        f"- **엄격 기준**(받은 텍스트에 이름이 찍힌 조작 가능 ref ≥ 5, 작은 창 예외는 창 전체를 받았을 때만): "
        f"**{len(strict)} / {base} = {len(strict) / max(base, 1):.1%}** (제외 후) · "
        f"원래 분모 기준 {len(strict)} / {total} = {len(strict) / max(total, 1):.1%}",
        f"- 느슨한 기준(스냅샷 refmap 전체 · 내용 칸은 이름 없어도 셈): {len(usable)} / {base} = "
        f"{len(usable) / max(base, 1):.1%} · 원래 분모 기준 {len(usable)} / {total}",
        "",
        "| 앱 | 엄격 | 느슨 | 렌더러 | refs | 이름 ref(엄격/느슨) | CUOP 텍스트 | 스크린샷 | 비율 | 창(pt) | 비고 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows.values():
        ratio = f"{r['text_tokens'] / r['shot_tokens']:.2f}" if r.get("shot_tokens") and r.get("text_tokens") else "—"
        note = (
            r.get("reason", "")
            + (" · skeleton" if r.get("skeleton") else "")
            + (" · 실행 중이던 앱" if r.get("was_running") else "")
        )
        lines.append(
            f"| {r['app']} | {r.get('strict', r['status'] if r['status'] == 'excluded' else '—')} | {r['status']} | "
            f"{r.get('renderer', '—')} | {r.get('ref_count', '—')} | "
            f"{r.get('strict_named', '—')}/{r.get('named_refs', '—')} | {r.get('text_tokens', '—')} | "
            f"{r.get('shot_tokens', '—')} | {ratio} | "
            f"{r.get('size', '—')} | {note.strip(' ·')} |"
        )
    missing = [a.stem for a in apps() if a.stem not in rows]
    if missing:
        lines += ["", "아직 재지 않음: " + ", ".join(missing)]
    lines += ["", "## 제외 목록", "", *[f"- {r['app']}: {r['reason']}" for r in excl]]
    Path("docs/parity/COVERAGE.md").write_text("\n".join(lines) + "\n")
    print(f"strict {len(strict)}/{base} ({len(strict) / max(base, 1):.1%}) · loose {len(usable)}/{base}, raw {total}")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        run([a for a in sys.argv[1:] if not a.startswith("--")])
