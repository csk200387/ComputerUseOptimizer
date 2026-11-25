# pyright: basic
"""M9 본 실험 요약 — logs/m9-main.log에 찍힌 run_id만 logs/runs.jsonl에서 골라 작업 × 모드별로 묶는다(숫자만).

uv run python benchmarks/m9_summary.py   # 마크다운 표를 출력 → benchmarks/results.md에 붙인다
uv run python benchmarks/m9_summary.py logs/office-main-b.log … --exclude=5c813be0   # 다른 실험(Office)의 로그만
"""

import json
import re
import statistics
import sys
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"


def main() -> None:
    args = sys.argv[1:]
    skip = {a.removeprefix("--exclude=") for a in args if a.startswith("--exclude=")}  # 다시 돌려 대체된 run
    files = [Path(f) for f in args if not f.startswith("--")] or [LOGS / "m9-main.log", LOGS / "m9-main-web.log"]
    text = "".join(f.read_text() for f in files if f.exists())
    ids = set(re.findall(r"^\| \S+ \| \S+ \| ([0-9a-f]{8}) \|", text, re.M)) - skip
    rows = [r for line in (LOGS / "runs.jsonl").read_text().splitlines() if line.strip()
            for r in [json.loads(line)] if r["run_id"] in ids]  # fmt: skip
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["task"], r["mode"]), []).append(r)

    def mean(rs: list[dict], k: str) -> float:
        vals = [float(r[k] or 0) for r in rs if not r.get("excluded")]
        return statistics.mean(vals) if vals else 0.0

    head = [
        "작업",
        "모드",
        "성공",
        "누적 입력(평균)",
        "비용 $(평균)",
        "시간 s(평균)",
        "도구 호출(평균)",
        "서버 관찰 토큰(평균)",
        "이미지",
    ]
    print("| " + " | ".join(head) + " |")
    print("|---|---|---|---|---|---|---|---|---|")
    tasks = sorted({t for t, _ in groups})
    total = {"cuop": [0.0, 0.0, 0, 0], "screenshot": [0.0, 0.0, 0, 0]}  # 입력 · 비용 · 성공 · 대상 run
    fair = {
        "cuop": [0.0, 0.0],
        "screenshot": [0.0, 0.0],
    }  # 두 모드 모두 비교 가능한 작업만(battery: 스크린샷 모드 불가)
    for t in tasks:
        for m in ("cuop", "screenshot"):
            rs = groups.get((t, m), [])
            ok = sum(1 for r in rs if r["success"])
            n = sum(1 for r in rs if not r.get("excluded"))
            skipped = len(rs) - n
            note = f" (+{skipped} 제외)" if skipped else ""
            print(f"| {t} | {m} | {ok}/{n}{note} | {mean(rs, 'input_total'):,.0f} | {mean(rs, 'cost_usd'):.3f} | "
                  f"{mean(rs, 'seconds'):.1f} | {mean(rs, 'tool_calls'):.1f} | {mean(rs, 'server_tokens'):,.0f} | "
                  f"{mean(rs, 'vision'):.1f} |")  # fmt: skip
            tot = total[m]
            tot[0] += sum(float(r["input_total"] or 0) for r in rs if not r.get("excluded"))
            tot[1] += sum(float(r["cost_usd"] or 0) for r in rs if not r.get("excluded"))
            tot[2] += ok
            tot[3] += n
            if t != "battery":
                fair[m][0] += sum(float(r["input_total"] or 0) for r in rs if not r.get("excluded"))
                fair[m][1] += sum(float(r["cost_usd"] or 0) for r in rs if not r.get("excluded"))
    c, s = total["cuop"], total["screenshot"]
    print()
    print(f"- 성공: cuop {c[2]}/{c[3]} · screenshot {s[2]}/{s[3]}")
    if s[0] and s[1]:
        print(f"- 누적 입력 합계: cuop {c[0]:,.0f} vs screenshot {s[0]:,.0f} ({(c[0] - s[0]) / s[0]:+.1%})")
        print(f"- 비용 합계: cuop ${c[1]:.2f} vs screenshot ${s[1]:.2f} ({(c[1] - s[1]) / s[1]:+.1%})")
        fc, fs = fair["cuop"], fair["screenshot"]
        if (fc[0], fs[0]) != (c[0], s[0]):  # battery가 있을 때만
            print(f"- battery 제외(두 모드 모두 가능한 작업): 누적 입력 {(fc[0] - fs[0]) / fs[0]:+.1%} · "
                  f"비용 {(fc[1] - fs[1]) / fs[1]:+.1%}")  # fmt: skip


if __name__ == "__main__":
    main()
