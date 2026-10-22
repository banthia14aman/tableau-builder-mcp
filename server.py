# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.20,<2", "tableauhyperapi"]
# ///
"""tableau-builder MCP server: author Tableau Public workbooks (.twbx) from a declarative JSON spec.

Run:  uv run --script server.py        (stdio MCP server)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from tabmcp import tools  # noqa: E402
from tabmcp.model import SpecError  # noqa: E402

mcp = FastMCP("tableau-builder")
SPEC_DOC = Path(__file__).resolve().parent / "SPEC.md"


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except SpecError as e:
        return {"ok": False, "error": f"spec error: {e}"}
    except (FileNotFoundError, ValueError, KeyError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _spec(spec):
    return json.loads(spec) if isinstance(spec, str) else spec


@mcp.tool()
def spec_reference() -> str:
    """Return the full workbook spec reference (fields, marks, filters, parameters, dashboards, actions)
    with examples and the design rules. Read this before writing your first spec in a session."""
    return SPEC_DOC.read_text(encoding="utf-8")


@mcp.tool()
def profile_data(path: str) -> dict:
    """Profile a CSV: per-column datatype, inferred role (dimension/measure), nulls, distinct count,
    min/max/mean, top values, geographic role, plus a hint when the table is wide and needs pivoting."""
    return _safe(tools.profile_data, path)


@mcp.tool()
def validate_spec(spec: dict | str) -> dict:
    """Dry-run a spec: resolves every field, calc, parameter, sheet and dashboard reference and compiles
    the XML without writing files. Returns actionable errors (with 'did you mean' suggestions)."""
    return _safe(tools.validate_spec, _spec(spec))


@mcp.tool()
def build_workbook(spec: dict | str) -> dict:
    """Compile a spec into <output_dir>/<name>.twbx (Hyper extracts packaged, as Tableau Public requires)
    and a sibling .twb. Then call open_in_tableau on the .twbx to verify it loads."""
    return _safe(tools.build_workbook, _spec(spec))


@mcp.tool()
def open_in_tableau(path: str, keep_open: bool = True) -> dict:
    """Open a .twb/.twbx in Tableau Public (a uniquely named copy) and read Tableau's own log to verify the
    load: ok=true only if the workbook loaded, no error dialog, no invalid calculations, and every sheet on
    the opening dashboard computed. Takes ~3-5 s. Needs Tableau Public installed (macOS; Windows is experimental)."""
    return _safe(tools.open_in_tableau, path, keep_open)


@mcp.tool()
def inspect_workbook(path: str) -> dict:
    """Summarise ANY .twb/.twbx (including ones saved by Tableau): datasources and fields (internal name,
    caption, formula), worksheets (mark, shelves, encodings, filters), dashboards and their zones."""
    return _safe(tools.inspect_workbook, path)


@mcp.tool()
def patch_workbook(path: str, ops: list[dict], output: str | None = None) -> dict:
    """Escape hatch for features the spec does not model: edit workbook XML in place (twb or twbx).
    ops: [{"op": "set_attr", "xpath": ".//worksheet[@name='X']/table/panes/pane/mark", "attrs": {"class": "Area"}},
          {"op": "append_xml" | "insert_before_xml" | "replace_xml", "xpath": ..., "xml": "<.../>"},
          {"op": "remove", "xpath": ...}, {"op": "set_text", "xpath": ..., "text": ...}]
    Always run open_in_tableau afterwards."""
    return _safe(tools.patch_workbook, path, ops, output)


@mcp.tool()
def diff_workbooks(a: str, b: str) -> dict:
    """Normalised XML diff of two workbooks. To learn the XML for any Tableau feature: open a workbook in
    Tableau, change one thing in the UI, save a copy, and diff the two."""
    return _safe(tools.diff_workbooks, a, b)


if __name__ == "__main__":
    if "--check" in sys.argv:          # used by install.sh / install.ps1: proves deps resolve, then exits
        import tableauhyperapi  # noqa: F401
        from tabmcp.validate import IS_WIN, LOG_DIR

        if IS_WIN:                     # a piped Windows stdout is cp1252: never fail on a non-Latin user name
            sys.stdout.reconfigure(errors="backslashreplace")
        print("tableau-builder ok:", len(mcp._tool_manager.list_tools()), "tools; Tableau app:", tools.APP if tools.APP.exists() else "not found")
        plat = {"darwin": "macOS", "win32": "Windows (live validation EXPERIMENTAL)"}.get(sys.platform, "unsupported for live validation")
        print(f"  platform: {sys.platform} ({plat})")
        print(f"  app path: {tools.APP} ({'found' if tools.APP.exists() else 'not found'})")
        print(f"  log dir:  {LOG_DIR} ({'found' if LOG_DIR.is_dir() else 'not found yet: Tableau creates it on first launch'})")
        sys.exit(0)
    mcp.run()
