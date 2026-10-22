#!/usr/bin/env bash
# One-command setup for tableau-builder (safe to re-run).
#   ./install.sh            register the MCP server + install the skill for Claude Code
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)   # Git Bash / MSYS2 / Cygwin on Windows: paths, symlinks and python3 differ here
    warn "Windows shell detected. On Windows use the PowerShell installer (EXPERIMENTAL) instead:"
    echo "   powershell -ExecutionPolicy Bypass -File \"$(cygpath -m "$DIR/install.ps1" 2>/dev/null || echo "$DIR/install.ps1")\""   # C:/... works in bash and PowerShell
    exit 1 ;;
esac

say "1/5 Checking prerequisites"
[[ "$(uname)" == "Darwin" ]] || warn "Not macOS: building workbooks works, but open_in_tableau (live validation) needs macOS + Tableau Public."
if ! command -v uv >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then brew install uv; else curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; fi
fi
command -v uv >/dev/null || { echo "uv is required (https://docs.astral.sh/uv/)"; exit 1; }
ls -d /Applications/Tableau\ Public*.app >/dev/null 2>&1 || warn "Tableau Public not found in /Applications - install it from https://public.tableau.com/app/discover (free) to open and validate workbooks."

say "2/5 Self-test (stdlib only)"
python3 "$DIR/test_tableau_mcp.py"

say "3/5 Resolving server dependencies (mcp, tableauhyperapi) - first run downloads ~100 MB"
uv run --quiet --script "$DIR/server.py" --check

say "4/5 Registering the MCP server with Claude Code"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove tableau-builder --scope user >/dev/null 2>&1 || true
  claude mcp add --scope user tableau-builder -- uv run --quiet --script "$DIR/server.py"
else
  warn "claude CLI not found. Add this MCP server to your client manually:"
  echo "   command: uv   args: run --quiet --script $DIR/server.py"
fi

say "5/5 Installing the tableau-dashboard skill"
mkdir -p "$HOME/.claude/skills"
ln -sfn "$DIR/skills/tableau-dashboard" "$HOME/.claude/skills/tableau-dashboard"
echo "   linked ~/.claude/skills/tableau-dashboard -> $DIR/skills/tableau-dashboard"

say "Done. Restart Claude Code, then ask: \"Build a Tableau dashboard from <path/to/data.csv>\"."
