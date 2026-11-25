# pyright: basic
"""Jev 실험 요약(JEV_EXP) — 규칙(cuop) vs cuop+jev, 기존 스크린샷 기준선(날짜 다름) 옆에. 숫자만.

uv run python benchmarks/jev_summary.py [logs/jev-main-a.log logs/jev-main-b.log logs/jev-main-ppt.log]
"""

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"
OBS = ("snapshot", "find", "get")
MODES = ("cuop", "cuop+jev")
# 스크린샷 기준선: M9 본 실험 · Office 본 실험(5c813be0은 다시 돌려 대체된 run)
BASE_LOGS = ["m9-main.log", "m9-main-web.log", *(f"office-main-{x}.log" for x in ("b", "b2", "c", "d", "e", "f"))]
BASE_SKIP = {"5c813be0"}


def _ids(paths: list[Path]) -> set[str]:
    text = "\n".join(p.read_text() for p in paths if p.exists())
    return set(re.findall(r"^\| \S+ \| \S+ \| ([0-9a-f]{8}) \|", text, re.M))


def _mean(vals: list[float]) -> float:
    return statistics.mean(vals) if vals else 0.0


def main() -> None:
    logs = [Path(a) for a in sys.argv[1:]] or [
        LOGS / n for n in ("jev-main-a.log", "jev-main-b.log", "jev-main-ppt.log")
    ]
    ids = _ids(logs)
    runs = {r["run_id"]: r for r in (json.loads(x) for x in (LOGS / "runs.jsonl").read_text().splitlines() if x)}
    steps: dict[str, list[dict]] = defaultdict(list)
    for x in (LOGS / "steps-cuop.jsonl").read_text().splitlines():
        s = json.loads(x)
        if s["run_id"] in ids:
            steps[s["run_id"]].append(s)
    rows = [runs[i] for i in ids if i in runs and not runs[i].get("excluded")]
    excluded = sorted(f"{runs[i]['task']}/{runs[i]['mode']}" for i in ids if i in runs and runs[i].get("excluded"))
    base_ids = _ids([LOGS / n for n in BASE_LOGS]) - BASE_SKIP
    shots = [runs[i] for i in base_ids if i in runs and runs[i]["mode"] == "screenshot" and not runs[i].get("excluded")]

    def jev(r: dict, k: str) -> float:
        return float(sum(s.get(k, 0) for s in steps[r["run_id"]]))

    def obs(r: dict, k: str) -> float:
        return float(sum(1 for s in steps[r["run_id"]] if s["tool"] == f"desktop_{k}"))

    head = ["작업", "모드", "성공", "누적 입력", "비용 $", "턴", "snapshot", "find", "get", "이미지", "시간 s",
            "Jev 호출", "Jev 입력", "Jev ms"]  # fmt: skip
    print("| " + " | ".join(head) + " |\n|" + "---|" * len(head))
    tasks = sorted({r["task"] for r in rows})
    tot: dict[str, Counter] = {m: Counter() for m in MODES}
    for t in tasks:
        for m in MODES:
            rs = [r for r in rows if r["task"] == t and r["mode"] == m]
            if not rs:
                continue
            ok = sum(bool(r["success"]) for r in rs)
            cells = [f"{ok}/{len(rs)}", f"{_mean([r['input_total'] for r in rs]):,.0f}",
                     f"{_mean([r['cost_usd'] or 0 for r in rs]):.3f}", f"{_mean([r['turns'] or 0 for r in rs]):.1f}",
                     *[f"{_mean([obs(r, k) for r in rs]):.1f}" for k in OBS], f"{_mean([r['vision'] for r in rs]):.1f}",
                     f"{_mean([r['seconds'] for r in rs]):.0f}", f"{_mean([jev(r, 'jev_calls') for r in rs]):.1f}",
                     f"{_mean([jev(r, 'jev_in') for r in rs]):,.0f}",
                     f"{_mean([jev(r, 'jev_ms') for r in rs]):,.0f}"]  # fmt: skip
            print(f"| {t} | {m} | " + " | ".join(cells) + " |")
            for r in rs:
                tot[m].update({"n": 1, "ok": int(bool(r["success"])), "input": r["input_total"],
                               "cost": round((r["cost_usd"] or 0) * 1e6), "turns": r["turns"] or 0,
                               "jev_calls": int(jev(r, "jev_calls")), "jev_in": int(jev(r, "jev_in")),
                               "jev_ms": int(jev(r, "jev_ms")), "obs": int(sum(obs(r, k) for k in OBS))})  # fmt: skip
    print("\n| 모드 | 성공 | 누적 입력 합 | 비용 합 $ | 턴 합 | 관찰 호출 합 | Jev 호출 | Jev 입력 토큰 | Jev 시간 s |")
    print("|---|---|---|---|---|---|---|---|---|")
    for m in MODES:
        c = tot[m]
        print(f"| {m} | {c['ok']}/{c['n']} | {c['input']:,} | {c['cost'] / 1e6:.2f} | {c['turns']} | {c['obs']} | "
              f"{c['jev_calls']} | {c['jev_in']:,} | {c['jev_ms'] / 1000:.1f} |")  # fmt: skip
    if tot["cuop"]["n"] and tot["cuop+jev"]["n"] and tot["cuop"]["input"]:  # 한쪽이 0회면 비교하지 않는다
        a, b = tot["cuop"], tot["cuop+jev"]
        d = {k: (b[k] - a[k]) / a[k] for k in ("input", "cost", "turns")}
        print(f"\n- cuop+jev vs cuop: 누적 입력 {d['input']:+.1%} · 비용 {d['cost']:+.1%} · 턴 {d['turns']:+.1%}")
    if excluded:
        print(f"- 시작 전 제외(사용자 앱 실행 중 등): {Counter(excluded)}")

    print("\n## 발표용 그래프 숫자(작업별 평균) — 스크린샷은 기존 본 실험(11-21 M9 · 11-22 Office, 날짜 다름)")
    print(
        "| 작업 | 규칙 입력 | Jev 입력 | 스크린샷 입력 | 규칙 $ | Jev $ | 스크린샷 $ | 규칙 턴 | Jev 턴 | 스크린샷 턴 |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for t in tasks:
        cells = []
        for k, fmt in (("input_total", "{:,.0f}"), ("cost_usd", "{:.3f}"), ("turns", "{:.1f}")):
            for group in ([r for r in rows if r["task"] == t and r["mode"] == "cuop"],
                          [r for r in rows if r["task"] == t and r["mode"] == "cuop+jev"],
                          [r for r in shots if r["task"] == t]):  # fmt: skip
                cells.append(fmt.format(_mean([float(r[k] or 0) for r in group])) if group else "—")
        print(f"| {t} | " + " | ".join(cells) + " |")

    today = _ids([LOGS / "jev-shot.log"])
    same = [runs[i] for i in today if i in runs and not runs[i].get("excluded")]
    if same:
        print("\n## 같은 날 스크린샷 기준선(logs/jev-shot.log)")
        print("| 작업 | 성공 | 누적 입력 | 비용 $ | 턴 | 이미지 | 시간 s |\n|---|---|---|---|---|---|---|")
        for t in sorted({r["task"] for r in same}):
            rs = [r for r in same if r["task"] == t]
            ok = sum(bool(r["success"]) for r in rs)
            print(f"| {t} | {ok}/{len(rs)} | {_mean([r['input_total'] for r in rs]):,.0f} | "
                  f"{_mean([r['cost_usd'] or 0 for r in rs]):.3f} | {_mean([r['turns'] or 0 for r in rs]):.1f} | "
                  f"{_mean([r['vision'] for r in rs]):.1f} | {_mean([r['seconds'] for r in rs]):.0f} |")  # fmt: skip

    print("\n## Jev 경로 판단(창마다 첫 snapshot) — 작업별 (경로, 확신도, 힌트 표시)")
    for t in tasks:
        ev = [(e["route"], e["conf"], e["shown"]) for r in rows if r["task"] == t and r["mode"] == "cuop+jev"
              for s in steps[r["run_id"]] for e in s.get("jev_events", [])]  # fmt: skip
        print(f"- {t}: " + ", ".join(f"{r} {c} {'힌트' if shown else '-'}" for r, c, shown in ev))


if __name__ == "__main__":
    main()
