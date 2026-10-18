---
name: tableau-builder-mcp
description: Install the Tableau Builder MCP server, which lets your agent design, build and verify real Tableau workbooks (.twbx) from CSV data. KPI tiles, sorted charts, maps, filters, parameters, animated Pages and multi-dashboard layouts are compiled to Tableau XML and checked against Tableau Public's own logs, so the file opens without errors. Use this when the user wants to install Tableau Builder, set up the Tableau MCP, or build Tableau dashboards with an agent.
---

# Tableau Builder MCP: install

This package is an **MCP server**, so it must live on disk to run. That makes it an install, not a session-only skill. Do it once per machine, with the user's go-ahead:

1. **Write the files.** Write every file in this package, keeping its relative path, under a permanent folder: `~/tableau-builder/` (e.g. `~/tableau-builder/server.py`, `~/tableau-builder/tabmcp/compile.py`). Skip this SKILL.md.
2. **Run the installer:** `bash ~/tableau-builder/install.sh`. It checks macOS, uv and Tableau Public, runs a self-test, resolves the dependencies (`mcp`, `tableauhyperapi`, about 100 MB the first time), registers the server with Claude Code as `tableau-builder`, and links the `tableau-dashboard` skill into `~/.claude/skills/`.
3. **Restart Claude Code.** Tell the user to restart it. The tools `profile_data`, `validate_spec`, `build_workbook`, `open_in_tableau`, `inspect_workbook`, `patch_workbook`, `diff_workbooks` and `spec_reference` then appear.

**Requirements:**
- macOS with Tableau Public (free, https://public.tableau.com) for opening and validating. Building `.twbx` files works on any OS.
- Python 3.10+.
- uv (the installer installs it via Homebrew or the official script if it's missing).

**Other MCP clients:** run `uv run --quiet --script ~/tableau-builder/server.py` as a stdio server.

## Using it
Follow `skills/tableau-dashboard/SKILL.md` (installed by step 2):

> profile the data → tidy CSVs → story first → spec → `validate_spec` → `build_workbook` → `open_in_tableau` (must return ok) → look at it

The full spec syntax is in `SPEC.md`; the architecture is in `docs/ARCHITECTURE.md`.

## Updating
To update, re-run steps 1–2 with the latest version of this package. Re-running `install.sh` is safe.
