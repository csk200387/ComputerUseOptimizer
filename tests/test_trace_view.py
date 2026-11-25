import json
from pathlib import Path
from typing import Any

import pytest

from cuop.desktop.trace_view import export_html


def test_export_html(tmp_path: Path) -> None:
    malicious_name = "</script><script>alert(1)</script>"
    secret = "secret text from the trace writer"
    events: list[dict[str, Any]] = [
        {
            "type": "trace.meta",
            "schema": 1,
            "version": "0.1.0",
            "os": "macos",
            "pid": 123,
            "session_id": "ses-ab12cd34",
            "ts_ms": 1795225550123,
        },
        {
            "type": "command",
            "step": 1,
            "ts_ms": 1795225550500,
            "command": "snapshot",
            "ok": True,
            "duration_ms": 50,
            "out_tokens": 200,
        },
        {
            "type": "command",
            "step": 3,
            "ts_ms": 1795225551000,
            "command": "click",
            "app": "Calculator",
            "ok": True,
            "delivery": "delivered_unverified",
            "duration_ms": 412,
            "out_tokens": 18,
            "target": {
                "role": "button",
                "ref": "@sa7:e14",
                "name": malicious_name,
                "bounds": {"x": 408, "y": 478, "width": 48, "height": 48},
            },
            "window": {"id": "w-33532", "bounds": {"x": 398, "y": 291, "width": 230, "height": 408}},
        },
        {
            "type": "command",
            "step": 4,
            "ts_ms": 1795225551500,
            "command": "type",
            "ok": False,
            "code": "STALE_REF",
            "duration_ms": 10,
            "args": {"text": {"redacted": True, "value": secret}},
            "extra": ["preserve this field", {"redacted": True, "data": secret}],
        },
    ]
    out = tmp_path / "trace.html"
    result = export_html(events, out, title="Trace <review>")

    assert out.is_file()
    assert result == {"path": str(out), "events": 3, "bytes": out.stat().st_size}
    page = out.read_text(encoding="utf-8")
    commands = [page.index(f"<td>{command}</td>") for command in ("snapshot", "click", "type")]
    assert commands == sorted(commands)
    assert "<title>Trace &lt;review&gt;</title>" in page
    assert "<dd>ses-ab12cd34</dd>" in page
    assert "<dt>OK / failed</dt><dd>2 / 1</dd>" in page
    assert "<dt>Total duration</dt><dd>472 ms</dd>" in page
    assert "<dt>Out tokens</dt><dd>218</dd>" in page
    assert "+877 ms" in page
    assert '<span class="badge failure">STALE_REF</span>' in page
    assert page.count('<span class="redacted" aria-label="redacted">•••</span>') == 2
    assert secret not in page
    assert malicious_name not in page
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in page

    embedded = page.split('<script type="application/json" id="events">', 1)[1].split("</script>", 1)[0]
    payload = json.loads(embedded)
    assert payload[2]["target"]["name"] == malicious_name
    assert payload[3]["args"]["text"] == {"redacted": True}
    assert payload[3]["extra"] == ["preserve this field", {"redacted": True}]
    assert events[3]["args"]["text"]["value"] == secret

    with pytest.raises(FileExistsError):
        export_html([], out)
    assert out.read_text(encoding="utf-8") == page
    second = tmp_path / "same-trace.html"
    export_html(events, second, title="Trace <review>")
    assert second.read_bytes() == out.read_bytes()


def test_missing_fields_and_unknown_events(tmp_path: Path) -> None:
    out = tmp_path / "sparse.html"
    result = export_html([{}, {"type": "future"}, {"type": "command"}], out)

    assert result["events"] == 1
    page = out.read_text(encoding="utf-8")
    assert page.count('<tr class="step">') == 1
    assert 'aria-controls="detail-1">1</button>' in page
    assert "<dt>OK / failed</dt><dd>0 / 0</dd>" in page
