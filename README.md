# tableau-builder

> **Free for students and personal use.** Clone this repo and run `install.sh`: coursework, case competitions, research and hobby projects are all covered ([LICENSE](LICENSE): PolyForm Noncommercial 1.0.0 plus a student grant). **Commercial use** (at work, for paid client work, or in a product) needs a licence: **[Tableau Builder MCP on Agensi: $7.99](https://agensi.io/skills/tableau-builder-mcp)**, which also gives a one-click install. The companion [tableau-dashboard skill](https://agensi.io/skills/tableau-dashboard-builder) is free.

**Let an AI agent build real Tableau dashboards.** tableau-builder is an MCP server plus a Claude Code skill. It turns a compact JSON spec into a Tableau workbook (`.twbx`, Hyper extracts packaged, ready for Tableau Public), opens it in Tableau, and **verifies the load by reading Tableau's own logs**. The agent knows the workbook works before you ever see it.

- KPI tiles, bars, lines, areas, scatter/bubbles, pies, filled maps, text tables
- Calculated fields with formulas written by display name: aggregates, LODs and table calcs are detected and rewritten for you
- Parameters with controls, all filter types (values, exclude, top-N, ranges), filter cards, legends
- Animated Pages playback with trails, fixed axes, number formats, fixed categorical palettes
- Multi-dashboard layouts from a row/column tree, filter and highlight actions, sheets hidden from the tab bar
- An escape hatch for everything else: `patch_workbook` (raw XML edits) and `diff_workbooks` (learn any feature by changing it once in Tableau's UI)

## Get it

**Students and personal use (free):** paste this repo's link into Claude Code and ask it to set it up, or run it yourself:
```bash
git clone https://github.com/banthia14aman/tableau-builder-mcp ~/tableau-builder && ~/tableau-builder/install.sh
```

**Commercial use:** buy the licence on [Agensi](https://agensi.io/skills/tableau-builder-mcp) ($7.99 one-time). Your agent then writes the package to `~/tableau-builder` and runs `install.sh`.

The installer:
1. checks macOS, uv and Tableau Public;
2. runs the self-test;
3. registers the MCP server;
4. installs the skill.

Restart Claude Code, then ask:

> Build a Tableau dashboard from ~/Downloads/sales.csv that shows where profit comes from

**Requirements:**
- macOS with [Tableau Public](https://public.tableau.com) (free) for opening and validating. Building `.twbx` files works on any OS.
- Python 3.10+ and [uv](https://docs.astral.sh/uv/).

**Dependencies:** `mcp` (<2) and `tableauhyperapi`, both resolved automatically.

Other MCP clients can run the server with: `uv run --quiet --script ~/tableau-builder/server.py` (stdio).

## Tools

| Tool | What it does |
|---|---|
| `spec_reference()` | The full spec syntax + design rules ([SPEC.md](SPEC.md)) |
| `profile_data(path)` | Per-column datatype, role, nulls, distinct, min/max, top values, geo role, wide-table hint |
| `validate_spec(spec)` | Dry run: resolves every field, calc, parameter and sheet; actionable errors ("did you mean …") |
| `build_workbook(spec)` | Writes `name.twbx` (+ `.twb`, Hyper extracts) |
| `open_in_tableau(path)` | Opens a copy in Tableau Public; `ok` only if it loaded with no error dialog, no invalid calcs, and every sheet on the opening dashboard computed |
| `inspect_workbook(path)` | Summarises any `.twb/.twbx`: fields, formulas, shelves, filters, dashboard zones |
| `patch_workbook(path, ops)` | XPath edits on workbook XML (set_attr, replace/insert/append XML, remove) |
| `diff_workbooks(a, b)` | Normalised XML diff, used to learn unsupported features from the Tableau UI |

## A spec in 30 seconds
```json
{
  "workbook": {"name": "Sales Review", "output_dir": "~/Desktop/out"},
  "data": [{"id": "s", "csv": "~/data/sales.csv",
            "fields": {"Region": {"colors": {"East": "#2a78d6", "West": "#eb6834"}}}}],
  "calcs": [{"name": "Profit Ratio", "formula": "SUM([Profit]) / SUM([Sales])", "format": "0.0%"}],
  "sheets": [
    {"name": "KPI Sales", "mark": "text", "label": "SUM(Sales)", "title": "Total sales", "formats": {"SUM(Sales)": "$#,##0"}},
    {"name": "By Region", "mark": "bar", "rows": ["Region"], "cols": ["SUM(Sales)"], "color": "Region",
     "label": "SUM(Sales)", "sort": [{"field": "Region", "by": "SUM(Sales)"}]},
    {"name": "Trend", "mark": "line", "cols": ["TMONTH(Order Date)"], "rows": ["Profit Ratio"]}
  ],
  "dashboards": [{"name": "Overview", "title": "East leads on sales",
                  "layout": {"col": [{"row": ["KPI Sales"], "size": 110}, {"row": ["By Region", "Trend"]}]}}]
}
```
Field references are written like Tableau pills: `SUM(Sales)`, `YEAR(Order Date)`, `TMONTH(Order Date)`, `Region`, `Profit Ratio:d`. See [SPEC.md](SPEC.md) for everything, and [examples/feature_matrix.json](examples/feature_matrix.json) for a spec that uses every feature (set `base_dir` to your clone).

## How it works
Tableau has no automation API, so tableau-builder works through files and logs:

1. It **writes** the workbook exactly the way Tableau would.
2. It **opens** it with `open -a "Tableau Public"`.
3. It **reads** Tableau's JSON-lines log for that workbook's process.

There it finds the load result, invalid-field warnings, error dialogs and which sheets computed. Class and sequence diagrams are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

The XML follows Tableau's legacy content model, load-tested on Tableau Public 2026.2. Every hard-won compatibility rule is marked `RULE:` in [`tabmcp/compile.py`](tabmcp/compile.py), for example:
- formulas must reference internal names, never captions
- aggregating calcs must be `usr:` instances
- dashboards need per-sheet `zoom` viewpoints
- flow-container tiles must be pinned or Tableau re-flows them
- maps need `<mapsources>` and a default country

## The `tableau-dashboard` skill
[`skills/tableau-dashboard`](skills/tableau-dashboard/SKILL.md) teaches the agent the analyst workflow:

> profile → tidy the data → write the story → spec → validate → build → verify in Tableau → look at it

It also carries design rules distilled from real reviewer feedback (one message per dashboard, takeaway titles with units, KPI tiles, sorted and labelled bars, consistent colours, no dense heatmaps) and a [chart-choice guide](skills/tableau-dashboard/references/chart-guide.md) by question and dataset type.

## Development
```bash
python3 test_tableau_mcp.py     # stdlib-only self-check: parsers + full compile + cross-reference checks
```
Live load-testing uses `open_in_tableau` (needs Tableau Public).

## Limitations
- `open_in_tableau` is macOS-only: it reads the logs under `~/Documents/My Tableau Repository`.
- Not modelled yet: dual axes, reference lines, stories, sets and groups, manual sort orders, URL and parameter actions. Use `patch_workbook`, guided by `diff_workbooks`.
- Data sources are CSV files: one table each, no joins or blending. Prepare and join the data in Python first; the skill tells the agent to.
- Publishing to Tableau Public is left to you (File → Save to Tableau Public).

## Credits
The Hyper extract, packaging and ID helpers are adapted from [tableau-public-authoring](https://github.com/andyyaro/tableau-public-authoring-mcp) (MIT, Andy Yaro). Its generator bugs (caption references, aggregate derivations, missing viewpoints, …) are fixed here. Not affiliated with Tableau or Salesforce. Tableau is a trademark of Salesforce, Inc.

Licence: free for students and noncommercial use under the PolyForm Noncommercial License 1.0.0 plus a student grant; commercial licences on [Agensi](https://agensi.io/skills/tableau-builder-mcp). See [LICENSE](LICENSE). Versions published on GitHub before 2026-10-18 were MIT.
