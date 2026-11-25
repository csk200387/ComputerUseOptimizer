# pyright: basic
"""같은 창에서 agent-desktop(오라클) · CUOP · 스크린샷 토큰을 비교한다 — 숫자만 출력(노드 텍스트는 저장하지 않음).

`uv run python benchmarks/oracle_compare.py Calculator Spotify ChatGPT`

- 같은 범위: 두 CLI 모두 `snapshot --app X -i --compact`(창 하나). CUOP는 JSON과 텍스트(`--format text`).
  CUOP가 고른 창(`data.window.id`)을 오라클에도 `--window-id`로 넘겨 같은 창을 잰다.
- 스크린샷: 그 창의 크기로 계산(캡처 없음, D4). 주 기준 = 기기 픽셀(scale배) → 긴 변 1568 · 1.15MP로 축소 후 w·h/750,
  보조 = 1pt = 1px.
토큰 = tiktoken o200k (Claude 토크나이저 근사, 비교용). 특수 토큰처럼 보이는 UI 글자도 일반 글자로 센다.
"""

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import tiktoken

ORACLE = os.environ.get("CUOP_ORACLE", str(Path.home() / ".local/agent-desktop-oracle/node_modules/.bin/agent-desktop"))
ENC = tiktoken.get_encoding("o200k_base")


def tokens(s: str) -> int:
    return len(ENC.encode(s, disallowed_special=()))


def screenshot_tokens(w: float, h: float) -> int:
    """Anthropic 이미지 비용: 긴 변 ≤ 1568, 픽셀 ≤ 1.15M으로 줄인 뒤 w·h/750. 크기 0이면 0."""
    if w <= 0 or h <= 0:
        return 0
    scale = min(1.0, 1568 / max(w, h), math.sqrt(1_150_000 / (w * h)))
    return round((w * scale) * (h * scale) / 750)


def run(cmd: list[str]) -> tuple[str, dict[str, Any]]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
        return out, json.loads(out)
    except (subprocess.TimeoutExpired, ValueError) as e:
        return "", {"ok": False, "error": {"code": type(e).__name__}}


def scale() -> float:
    _, env = run(["uv", "run", "cuop", "list-displays"])
    disp = env.get("data") or [{}]
    return float(disp[0].get("scale", 2.0))


def measure(app: str, dpr: float) -> dict[str, Any]:
    raw, env = run(["uv", "run", "cuop", "snapshot", "--app", app, "-i", "--compact"])
    row: dict[str, Any] = {"app": app}
    if not env.get("ok"):
        return row | {"error": env.get("error", {}).get("code")}
    data = env["data"]
    win = data.get("window") or {}
    row |= {"cuop_refs": data["ref_count"], "cuop_json": tokens(raw)}
    text_raw, _ = run(["uv", "run", "cuop", "snapshot", "--app", app, "-i", "--compact", "--format", "text"])
    row["cuop_text_i"] = tokens(text_raw)
    full_raw, _ = run(["uv", "run", "cuop", "snapshot", "--app", app, "--format", "text"])  # 기본 텍스트(글자 포함)
    row["cuop_text"] = tokens(full_raw)
    ad_cmd = [ORACLE, "snapshot", "--app", app, "-i", "--compact"] + (["--window-id", win["id"]] if win else [])
    ad_raw, ad = run(ad_cmd)
    row |= {"ad_refs": (ad.get("data") or {}).get("ref_count"), "ad_json": tokens(ad_raw) if ad.get("ok") else None}
    _, wins = run(["uv", "run", "cuop", "list-windows", "--app", app])
    b = next((w.get("bounds") for w in wins.get("data") or [] if w.get("id") == win.get("id")), None)
    if b:
        row["size"] = f"{b['width']}×{b['height']}"
        row["shot"] = screenshot_tokens(b["width"] * dpr, b["height"] * dpr)
        row["shot_1x"] = screenshot_tokens(b["width"], b["height"])
    return row


COLS = [
    "app",
    "ad_refs",
    "ad_json",
    "cuop_refs",
    "cuop_json",
    "cuop_text_i",
    "cuop_text",
    "shot",
    "shot_1x",
    "size",
    "error",
]


def main() -> None:
    import tempfile

    # 두 CLI 모두 refmap(요소 이름 포함)을 상태 폴더에 쓴다 → 임시 폴더에서 재고 지운다(개인 앱 텍스트를 남기지 않게)
    tmp = tempfile.TemporaryDirectory(prefix="cuop-bench-")
    os.environ["CUOP_HOME"] = os.environ["AGENT_DESKTOP_HOME"] = tmp.name
    dpr = scale()
    rows = [measure(app, dpr) for app in sys.argv[1:] or ["Calculator"]]
    print(f"| {' | '.join(COLS)} |\n|{'---|' * len(COLS)}")
    for r in rows:
        print("| " + " | ".join(f"{r[c]:,}" if isinstance(r.get(c), int) else str(r.get(c, "—")) for c in COLS) + " |")
    tmp.cleanup()


if __name__ == "__main__":
    main()
