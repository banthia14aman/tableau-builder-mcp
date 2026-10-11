# Instructions for AI agents (Claude Code, Codex, Cursor, ...)

This repo is **tableau-builder**: an MCP server that builds Tableau workbooks (.twbx) from a JSON spec and verifies them in Tableau Public, plus a `tableau-dashboard` skill.

## If the user gave you this repo link to "set it up"
1. Clone it somewhere permanent (not /tmp), e.g. `git clone <url> ~/tableau-builder && cd ~/tableau-builder`.
2. Run `./install.sh`. It checks macOS, uv and Tableau Public, runs the self-test, resolves the dependencies, registers the MCP server (`claude mcp add --scope user tableau-builder ...`) and links the skill into `~/.claude/skills/`.
3. Tell the user to restart Claude Code. The tools `profile_data`, `validate_spec`, `build_workbook` and `open_in_tableau` will then be available.
4. If Tableau Public is missing, tell the user to install it (free) from https://public.tableau.com. Building still works without it; only `open_in_tableau` needs it.

## If you are building a dashboard
Follow `skills/tableau-dashboard/SKILL.md`. Call `spec_reference()` for the spec syntax. Verify every build with `open_in_tableau`.

## If you are changing the code
- `tabmcp/model.py`: spec model, formula rewriting, the pill grammar.
- `tabmcp/compile.py`: XML emitter.
- `tabmcp/validate.py`: Tableau log reader.
- `tabmcp/tools.py`: the tool functions.
- `server.py`: the MCP wiring.

Before committing, run `python3 test_tableau_mcp.py`. For anything that changes the XML, also build a spec and run `open_in_tableau`. Every rule marked `RULE:` in `compile.py` was learned from a real Tableau load failure; don't remove one without load-testing.
