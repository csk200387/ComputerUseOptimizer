"""trace show · export, cursor-overlay — 사람이 에이전트의 진행을 보는 명령."""

from pathlib import Path
from typing import Any

from cuop.desktop.commands import Ctx
from cuop.desktop.errors import CuopError


def trace(
    ctx: Ctx, action: str, limit: int | None = None, event: str | None = None, out: str | None = None
) -> dict[str, Any]:
    from cuop.desktop import trace as tr

    if action not in ("show", "export"):
        raise CuopError("INVALID_ARGS", "trace action must be show or export")
    from cuop.desktop.store import Namespace

    ns = Namespace(ctx.session)  # 끝난 세션의 기록도 본다(쓰지는 않는다)
    if ns.session is None:
        raise CuopError("INVALID_ARGS", "trace needs a session (--session or CUOP_SESSION)")
    if action == "show":
        events, bad = tr.read(ns, 500 if limit is None else limit, event)
        return {"session_id": ns.session, "events": events, "count": len(events), "skipped_lines": bad}
    from cuop.desktop.trace_view import export_html

    events, bad = tr.read(ns, 5000 if limit is None else limit, event)
    path = Path(out) if out else ns.dir / f"trace-{ns.session}.html"
    if path.exists() and out is None:
        path.unlink()  # 기본 경로는 세션 안 — 다시 내보내면 바꾼다
    try:
        info = export_html(events, path, title=f"CUOP trace · {ns.session}")
    except FileExistsError:
        raise CuopError("INVALID_ARGS", f"{path} exists; trace export writes new files only") from None
    return {**info, "skipped_lines": bad}


def cursor_overlay(ctx: Ctx, action: str) -> dict[str, Any]:
    from cuop.overlay import client

    if action not in ("enable", "disable", "status"):
        raise CuopError("INVALID_ARGS", "cursor-overlay action must be enable, disable or status")
    ns = ctx.ns
    if action == "status":
        return {
            "enabled": client.enabled(ns),
            "running": client.ensure_running(wait=0) if client.enabled(ns) else False,
        }
    return client.set_enabled(ns, action == "enable")


SKILLS = Path(__file__).resolve().parent.parent / "skills"
ALIASES = {"desktop": "cuop", "platform": "cuop-macos", "macos": "cuop-macos"}


def skills(ctx: Ctx, action: str = "list", name: str | None = None, reference: str | None = None,
           full: bool = False) -> dict[str, Any]:  # fmt: skip
    """Bundled skill docs for AI agents: list, get NAME (desktop | platform), path."""
    del ctx, full
    if action not in ("list", "get", "path"):
        raise CuopError("INVALID_ARGS", "skills action must be list, get or path")
    if action == "path":
        return {"path": str(SKILLS)}
    if action == "list":
        rows: list[dict[str, Any]] = []
        for d in sorted(p for p in SKILLS.iterdir() if (p / "SKILL.md").exists()):
            head = (d / "SKILL.md").read_text(encoding="utf-8").split("description:", 1)[-1].split("---", 1)[0]
            rows.append({"name": d.name, "summary": " ".join(head.replace(">", "").split())[:200],
                         "references": sorted(p.stem for p in (d / "references").glob("*.md"))})  # fmt: skip
        return {"skills": rows, "aliases": ALIASES}
    key = ALIASES.get(name or "desktop", name or "desktop")
    path = SKILLS / key / ("SKILL.md" if reference is None else f"references/{reference}.md")
    if not path.resolve().is_relative_to(SKILLS) or not path.exists():
        raise CuopError("INVALID_ARGS", f"no skill {name!r}" + (f" reference {reference!r}" if reference else ""))
    return {"name": key, "reference": reference, "content": path.read_text(encoding="utf-8")}
