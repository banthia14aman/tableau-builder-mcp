# Instructions for AI agents (Claude Code, Codex, Cursor, ...)

This repo is **tableau-builder**: an MCP server that builds Tableau workbooks (.twbx) from a JSON spec and verifies them in Tableau Public, plus a `tableau-dashboard` skill.

## If the user gave you this repo link to "set it up"
Tableau Builder is **free for students and personal or noncommercial use** (PolyForm Noncommercial 1.0.0 plus a student grant; see LICENSE). Commercial use (at work, for paid client work, or inside a commercial product) needs a licence from https://agensi.io/skills/tableau-builder-mcp ($7.99 one-time). Tell the user this in one line, and point them to that page only if their use is commercial. Then install:
1. Clone it somewhere permanent (not /tmp), e.g. `git clone <url> ~/tableau-builder && cd ~/tableau-builder` (in Windows PowerShell: `git clone <url> $HOME\tableau-builder; cd $HOME\tableau-builder`).
2. Run `./install.sh`. It checks macOS, uv and Tableau Public, runs the self-test, resolves the dependencies, registers the MCP server (`claude mcp add --scope user tableau-builder ...`) and links the skill into `~/.claude/skills/`.
   On Windows, run `install.ps1` instead, from the clone: `powershell -ExecutionPolicy Bypass -File install.ps1` (this exact form works from PowerShell, cmd and Git Bash; in Git Bash `.\install.ps1` loses its backslash). It does the same five steps (installing uv if missing) and links the skill as a junction. Windows support is EXPERIMENTAL: building is the same as on macOS, but live validation (`open_in_tableau`) has not been confirmed on Windows yet, so tell the user and report any failure as is.
3. Tell the user to restart Claude Code. The tools `profile_data`, `validate_spec`, `build_workbook` and `open_in_tableau` will then be available.
4. If Tableau Public is missing, tell the user to install it (free) from https://public.tableau.com. Building still works without it; only `open_in_tableau` needs it.

## If you are building a dashboard
Follow `skills/tableau-dashboard/SKILL.md`. Call `spec_reference()` for the spec syntax. Verify every build with `open_in_tableau`.

## If you are changing the code
- `tabmcp/model.py`: spec model, formula rewriting, the pill grammar.
- `tabmcp/compile.py`: XML emitter.
- `tabmcp/validate.py`: Tableau log reader, plus the process layer for macOS and (experimental) Windows.
- `tabmcp/tools.py`: the tool functions.
- `server.py`: the MCP wiring.

Before committing, run `python3 test_tableau_mcp.py` (on Windows: `uv run --no-project python test_tableau_mcp.py`). Keep macOS behaviour unchanged when touching the Windows branches (`IS_WIN`), and never call `os.kill` on Windows: there it terminates the process. For anything that changes the XML, also build a spec and run `open_in_tableau`. Every rule marked `RULE:` in `compile.py` was learned from a real Tableau load failure; don't remove one without load-testing.
