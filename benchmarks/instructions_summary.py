# pyright: basic
"""지시문 전/후 비교표(INSTRUCTIONS_EXP) — 숫자만. run_id는 각 실험의 로그 표에서 고른다.

uv run python benchmarks/instructions_summary.py
"""

import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"
OBS = ("snapshot", "find", "get")
# 전: 이전 본 실험(같은 작업 · 같은 하네스). 후: 지시문 반영 뒤 이 실험
BEFORE_CMP = ("a8971f12", "1466af17", "f2683e92", "5424b203", "ffd4f573", "e5ba0ece")
BEFORE_OFFICE = [f"office-main-{x}.log" for x in ("b", "b2", "c", "d", "e", "f")]
OFFICE_SKIP = {"5c813be0"}  # Office 본 실험에서 다시 돌려 대체된 run


def _ids(*names: str) -> set[str]:
    missing = [n for n in names if not (LOGS / n).exists()]
    if missing:
        raise SystemExit(f"로그 없음: {missing}")
    text = "\n".join((LOGS / n).read_text() for n in names)
    return set(re.findall(r"^\| (?:\S+ \| \S+ \| )?([0-9a-f]{8}) \|", text, re.M))


def _rows(path: Path) -> dict[str, dict]:
    return {r["run_id"]: r for r in (json.loads(x) for x in path.read_text().splitlines() if x.strip())}


def _check(ids: set[str], rows: dict[str, dict], what: str) -> None:
    """두 기록(하네스 runs.jsonl · 비교 compare/runs.jsonl) 어디에도 없는 run만 알린다(로그 하나에 두 실험이 섞임)."""
    known = rows.keys() | _rows(LOGS / "runs.jsonl").keys() | _rows(LOGS / "compare" / "runs.jsonl").keys()
    if lost := ids - known:
        print(f"경고: {what}의 run {len(lost)}개가 기록에 없다: {sorted(lost)}", file=sys.stderr)


def _steps(path: Path) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for x in path.read_text().splitlines() if path.exists() else []:
        s = json.loads(x)
        out.setdefault(s["run_id"], Counter())[s["tool"].removeprefix("desktop_")] += 1
    return out


def _mean(vals: list[float]) -> str:
    return f"{statistics.mean(vals):,.1f}" if vals else "—"


def compare() -> list[str]:
    rows = _rows(LOGS / "compare" / "runs.jsonl")
    phases = [("전", set(BEFORE_CMP)), ("후", _ids("instr-compare.log")), ("후(반복1)", _ids("instr-compare-r1.log")),
              ("후(리뷰 문구)", _ids("instr-final.log")), ("후(반복2)", _ids("instr-r2.log"))]  # fmt: skip
    out = ["| 묶음 | 모드 | 성공 | 누적 입력 | 토큰 합 | API 환산 $ | 모델 호출 | snapshot | find | get | 이미지 |",
           "|" + "---|" * 11]  # fmt: skip
    for phase, ids in phases:
        _check(ids, rows, f"복합 작업 {phase}")
        for mode in ("cu", "cuop"):
            rs = [rows[i] for i in ids if i in rows and rows[i]["mode"] == mode and rows[i].get("valid")]
            if not rs:
                continue

            def tool(r: dict, k: str) -> float:
                return float(sum(n for t, n in (r.get("tools") or {}).items() if t.endswith(f"desktop_{k}")))

            cells = [_mean([float(r["input"]) for r in rs]), _mean([float(r["total"]) for r in rs]),
                     f"{statistics.mean(float(r['cost']) for r in rs):.3f}",
                     _mean([float(r["model_calls"]) for r in rs]), *[_mean([tool(r, k) for r in rs]) for k in OBS],
                     _mean([float(r["images"]) for r in rs])]  # fmt: skip
            ok = sum(bool(r["success"]) for r in rs)
            out.append(f"| {phase} | {mode} | {ok}/{len(rs)} | " + " | ".join(cells) + " |")
    return out


def harness(tasks: tuple[str, ...], phases: list[tuple[str, set[str]]]) -> list[str]:
    rows = _rows(LOGS / "runs.jsonl")
    steps = _steps(LOGS / "steps-cuop.jsonl")
    out = ["| 작업 | 묶음 | 성공 | 누적 입력 | 비용 $ | 턴 | snapshot | find | get | 이미지 |", "|" + "---|" * 10]
    for t in tasks:
        for phase, ids in phases:
            _check(ids, rows, phase)
            rs = [rows[i] for i in ids if i in rows and rows[i]["task"] == t and rows[i]["mode"] == "cuop"
                  and not rows[i].get("excluded")]  # fmt: skip
            if not rs:
                continue
            cost = statistics.mean(float(r["cost_usd"] or 0) for r in rs)
            cells = [_mean([float(r["input_total"] or 0) for r in rs]), f"{cost:.3f}",
                     _mean([float(r["turns"] or 0) for r in rs]),
                     *[_mean([float(steps.get(r["run_id"], Counter())[k]) for r in rs]) for k in OBS],
                     _mean([float(r["vision"] or 0) for r in rs])]  # fmt: skip
            ok = sum(bool(r["success"]) for r in rs)
            out.append(f"| {t} | {phase} | {ok}/{len(rs)} | " + " | ".join(cells) + " |")
    return out


def main() -> None:
    print("## 복합 작업\n", *compare(), sep="\n")
    office_before = _ids(*BEFORE_OFFICE) - OFFICE_SKIP
    print(
        "\n## Office\n",
        *harness(
            ("word", "excel", "powerpoint"),
            [
                ("전", office_before),
                ("전(F회차)", _ids("office-main-f.log")),
                ("후", _ids("instr-office.log")),
                ("후(리뷰 문구)", _ids("instr-final.log")),
                ("후(반복2)", _ids("instr-r2.log")),
            ],
        ),
        sep="\n",
    )
    m9 = harness(("calculator", "clock", "activity"), [("전", _ids("m9-main.log")), ("후", _ids("instr-m9.log")),
                                                       ("후(반복1)", _ids("instr-m9-r1.log")),
                                                       ("후(리뷰 문구)", _ids("instr-final.log")),
                                                       ("후(반복2)", _ids("instr-r2.log"))])  # fmt: skip
    print("\n## M9 짧은 작업\n", *m9, sep="\n")


if __name__ == "__main__":
    main()
