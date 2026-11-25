"""Export a desktop session trace as a standalone, offline HTML report."""

import json
import math
from html import escape
from pathlib import Path
from typing import Any, cast

_CHIP = '<span class="redacted" aria-label="redacted">•••</span>'
_COLUMNS = ("Step", "Time", "Elapsed", "Command", "Target", "App", "Outcome", "Delivery", "Duration", "Out tokens")


def _object(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _is_redacted(value: Any) -> bool:
    return isinstance(value, dict) and _object(value).get("redacted") is True


def _redact(value: Any) -> Any:
    if _is_redacted(value):
        return {"redacted": True}
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in _object(value).items()}
    if isinstance(value, list):
        return [_redact(item) for item in cast("list[Any]", value)]
    return value


def _display(value: Any, fallback: str = "—") -> str:
    if _is_redacted(value):
        return _CHIP
    return fallback if value is None or value == "" else escape(str(value))


def _pretty(value: Any, depth: int = 0) -> str:
    if depth and _is_redacted(value):
        return _CHIP
    if isinstance(value, dict):
        left, right = "{", "}"
        items = [
            f"{escape(json.dumps(key, ensure_ascii=False))}: {_pretty(item, depth + 1)}"
            for key, item in _object(value).items()
        ]
    elif isinstance(value, list):
        left, right = "[", "]"
        items = [_pretty(item, depth + 1) for item in cast("list[Any]", value)]
    else:
        return escape(json.dumps(value, ensure_ascii=False))
    if not items:
        return left + right
    indent = "  " * depth
    content = (",\n" + indent + "  ").join(items)
    return f"{left}\n{indent}  {content}\n{indent}{right}"


def _number(value: Any) -> int | float | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _time(ts: int | float | None, *, full: bool = False) -> str:
    if ts is None:
        return "—"
    full_attr = " data-full" if full else ""
    return f'<time data-ms="{escape(str(ts))}"{full_attr}>—</time>'


def _row(event: dict[str, Any], index: int, first_ts: int | float | None) -> str:
    step = _display(event.get("step"), str(index))
    ts = _number(event.get("ts_ms"))
    elapsed = f"{ts - first_ts:+} ms" if ts is not None and first_ts is not None else "—"
    target = _object(event.get("target"))
    ref = target.get("ref", _object(event.get("args")).get("ref"))
    target_text = " ".join(filter(None, (_display(target.get("role"), ""), _display(ref, ""))))
    outcome = "—"
    if event.get("ok") is True:
        outcome = '<span class="badge success">ok</span>'
    elif event.get("ok") is False:
        outcome = f'<span class="badge failure">{_display(event.get("code"), "error")}</span>'
    duration = _display(event.get("duration_ms"), "")
    cells = (
        f'<button type="button" aria-expanded="false" aria-controls="detail-{index}">{step}</button>',
        _time(ts),
        escape(elapsed),
        _display(event.get("command")),
        target_text or "—",
        _display(event.get("app")),
        outcome,
        _display(event.get("delivery")),
        f"{duration} ms" if duration else "—",
        _display(event.get("out_tokens")),
    )
    columns = "".join(f"<td>{cell}</td>" for cell in cells)
    return (
        f'<tr class="step">{columns}</tr>\n'
        f'<tr id="detail-{index}" hidden><td colspan="{len(_COLUMNS)}"><pre>{_pretty(event)}</pre></td></tr>'
    )


def export_html(events: list[dict[str, Any]], out: Path, title: str = "CUOP trace") -> dict[str, Any]:
    """Write a new HTML report and return its path, command count, and byte size."""
    safe_events = [{key: _redact(value) for key, value in event.items()} for event in events]
    meta = _object(next((event for event in safe_events if event.get("type") == "trace.meta"), None))
    commands = [event for event in safe_events if event.get("type") == "command"]
    timestamps = [ts for event in safe_events if (ts := _number(event.get("ts_ms"))) is not None]
    first_ts = timestamps[0] if timestamps else None
    last_ts = timestamps[-1] if timestamps else None
    ok = sum(event.get("ok") is True for event in commands)
    failed = sum(event.get("ok") is False for event in commands)
    summary = (
        ("Session", _display(meta.get("session_id"))),
        ("OS", _display(meta.get("os"))),
        ("Version", _display(meta.get("version"))),
        ("First event (local)", _time(first_ts, full=True)),
        ("Last event (local)", _time(last_ts, full=True)),
        ("Total steps", str(len(commands))),
        ("OK / failed", f"{ok} / {failed}"),
        ("Total duration", f"{sum(_number(event.get('duration_ms')) or 0 for event in commands)} ms"),
        ("Out tokens", str(sum(_number(event.get("out_tokens")) or 0 for event in commands))),
    )
    stats = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in summary)
    headings = "".join(f'<th scope="col">{column}</th>' for column in _COLUMNS)
    rows = "\n".join(_row(event, index, first_ts) for index, event in enumerate(commands, 1))
    # Escaping every '<' also prevents script-parser state changes from literal '<!--'.
    data = json.dumps(safe_events, separators=(",", ":")).replace("<", r"\u003c")
    safe_title = escape(title)
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>{_CSS}</style>
</head>
<body>
<header><h1>{safe_title}</h1><dl>{stats}</dl></header>
<main>
  <h2>Timeline</h2>
  <div class="filters">
    <label for="query">Filter steps</label>
    <input id="query" type="search" placeholder="Command, app, role, ref, or code">
    <label><input id="errors" type="checkbox"> Errors only</label>
    <label><input id="actions" type="checkbox"> Actions only</label>
  </div>
  <section class="visual" aria-labelledby="visual-heading">
    <h2 id="visual-heading">Visual debug</h2>
    <div id="diagram" aria-live="polite">Select a step to see its bounds.</div>
  </section>
  <p id="empty" role="status"{" hidden" if commands else ""}>No matching steps.</p>
  <div class="table-wrap" role="region" aria-label="Timeline table" tabindex="0">
    <table><thead><tr>{headings}</tr></thead><tbody>
{rows}
    </tbody></table>
  </div>
</main>
<script type="application/json" id="events">{data}</script>
<script>{_JS}</script>
</body>
</html>
""".encode("utf-8", errors="backslashreplace")
    with out.open("xb") as file:
        file.write(page)
    return {"path": str(out), "events": len(commands), "bytes": len(page)}


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f1f4f8; --surface: #fff; --ink: #172539; --muted: #4e5b6d; --line: #ccd5e0;
  --hover: #e8f0fc; --accent: #1f5ab3; --ok: #185c3b; --ok-bg: #d7f2e3;
  --error: #991f2c; --error-bg: #fbe1e5; --chip: #e4e7ec;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121925; --surface: #1d2736; --ink: #ebf0f8; --muted: #bbc8d9; --line: #41516a;
    --hover: #293b57; --accent: #a0c7ff; --ok: #a5eac3; --ok-bg: #1e4837;
    --error: #ffc1ca; --error-bg: #542c38; --chip: #394455;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0 auto; max-width: 80rem; padding: clamp(.75rem, 3vw, 2rem);
  background: var(--bg); color: var(--ink); font: 1rem/1.5 system-ui, sans-serif;
}
h1, h2 { margin: 0 0 .65rem; overflow-wrap: anywhere; }
h2 { font-size: 1.15rem; }
dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 10rem), 1fr)); gap: .5rem; }
dl div, .visual { padding: .65rem .8rem; background: var(--surface); border: 1px solid var(--line); }
dt { color: var(--muted); font-size: .8rem; }
dd { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem 1rem; margin-bottom: .75rem; }
input[type=search] {
  width: 18rem; max-width: 100%; padding: .45rem .65rem; font: inherit;
  border: 1px solid var(--line); border-radius: .3rem; background: var(--surface); color: var(--ink);
}
.visual { margin-bottom: .75rem; border-radius: .4rem; }
#diagram { color: var(--muted); overflow-wrap: anywhere; }
figure { margin: 0; }
figcaption { color: var(--ink); margin-bottom: .4rem; font-weight: 600; }
svg { display: block; max-width: 100%; height: auto; border: 1px solid var(--line); }
svg .window { fill: var(--bg); }
svg .target { fill: var(--hover); stroke: var(--accent); stroke-width: 2; vector-effect: non-scaling-stroke; }
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: .4rem; background: var(--surface); }
table { width: 100%; border-collapse: collapse; font-size: .875rem; }
th, td { padding: .5rem .65rem; text-align: left; border-bottom: 1px solid var(--line); }
th { color: var(--muted); white-space: nowrap; }
.step { cursor: pointer; }
.step:hover, .step:focus-within, .step.selected { background: var(--hover); }
button { font: inherit; color: var(--accent); cursor: pointer; }
.badge, .redacted { display: inline-block; padding: 0 .45em; border-radius: .4em; font-weight: 600; }
.success { color: var(--ok); background: var(--ok-bg); }
.failure { color: var(--error); background: var(--error-bg); }
.redacted { color: var(--muted); background: var(--chip); }
pre { margin: 0; max-width: min(65rem, calc(100vw - 3.25rem)); white-space: pre-wrap; overflow-wrap: anywhere; }
"""

_JS = """
const events = JSON.parse(document.querySelector('#events').textContent);
const commands = events.filter(event => event.type === 'command');
const rows = Array.from(document.querySelectorAll('.step'));
const query = document.querySelector('#query');
const errors = document.querySelector('#errors');
const actions = document.querySelector('#actions');
const diagram = document.querySelector('#diagram');
const readOnly = new Set(['snapshot', 'find', 'get', 'is', 'status', 'permissions',
                          'version', 'wait', 'trace', 'session', 'skills']);
let selected = null;

const pad = (value, size = 2) => String(value).padStart(size, '0');
for (const time of document.querySelectorAll('time[data-ms]')) {
  const date = new Date(Number(time.dataset.ms));
  if (!Number.isFinite(date.getTime())) continue;
  const clock = [date.getHours(), date.getMinutes(), date.getSeconds()].map(value => pad(value)).join(':');
  const day = [pad(date.getFullYear(), 4), pad(date.getMonth() + 1), pad(date.getDate())].join('-');
  time.textContent = `${time.hasAttribute('data-full') ? day + ' ' : ''}${clock}.${pad(date.getMilliseconds(), 3)}`;
}

function validBounds(bounds) {
  return bounds && [bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)
    && bounds.width > 0 && bounds.height > 0;
}

function svgElement(tag, attributes) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
}

function showDiagram(event, row) {
  diagram.textContent = 'Select a step to see its bounds.';
  if (!event) return;
  const windowBounds = event.window?.bounds;
  const target = event.target?.bounds;
  if (!validBounds(windowBounds) || !validBounds(target)) {
    diagram.textContent = 'Window or target bounds are unavailable for this step.';
    return;
  }
  const { x, y, width, height } = windowBounds;
  const figure = document.createElement('figure');
  const caption = document.createElement('figcaption');
  caption.textContent = `step ${row.cells[0].textContent} ${row.cells[3].textContent}`;
  const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, width: Math.min(480, width),
                                role: 'img', 'aria-label': 'Target bounds within the window'});
  svg.append(svgElement('rect', {class: 'window', width, height}), svgElement('rect', {
    class: 'target', x: target.x - x, y: target.y - y, width: target.width, height: target.height,
  }));
  figure.append(caption, svg);
  diagram.replaceChildren(figure);
}

function toggle(row, open) {
  row.nextElementSibling.hidden = !open;
  row.querySelector('button').setAttribute('aria-expanded', String(open));
  row.classList.toggle('selected', open);
}

function select(row, event) {
  if (selected) toggle(selected, false);
  selected = selected === row ? null : row;
  if (selected) toggle(selected, true);
  showDiagram(selected ? event : null, selected);
}

function filterRows() {
  const search = query.value.trim().toLowerCase();
  let visible = 0;
  rows.forEach((row, index) => {
    const event = commands[index];
    const command = typeof event.command === 'string' ? event.command : '';
    const isAction = command && !readOnly.has(command) && !command.startsWith('list-');
    const fields = [command, event.app, event.target?.role, event.target?.ref, event.args?.ref, event.code];
    const matches = !search || fields.some(field => typeof field === 'string' && field.toLowerCase().includes(search));
    row.hidden = !matches || (errors.checked && event.ok !== false) || (actions.checked && !isAction);
    if (row.hidden && selected === row) select(row, event);
    if (!row.hidden) visible += 1;
  });
  document.querySelector('#empty').hidden = visible !== 0;
}

rows.forEach((row, index) => row.addEventListener('click', () => select(row, commands[index])));
query.addEventListener('input', filterRows);
errors.addEventListener('change', filterRows);
actions.addEventListener('change', filterRows);
filterRows();
"""
