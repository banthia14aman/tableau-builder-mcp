---
name: tableau-dashboard
description: Build clean, readable Tableau (Public/Desktop) dashboards and .twbx workbooks from CSV/Excel data with the tableau-builder MCP server. Covers profiling the data, choosing the right chart for each question, writing the workbook spec, compiling, verifying the load in Tableau, and polishing. Use this whenever the user wants a Tableau dashboard, workbook, viz, .twb/.twbx, a Tableau Public submission, KPI tiles, an animated (Pages) chart in Tableau, or asks to "visualise this data in Tableau", even if they don't name the MCP server. Also use it to fix, inspect or extend an existing Tableau workbook.
---

# Tableau dashboards with tableau-builder

You author Tableau workbooks by writing a JSON **spec**. The `tableau-builder` MCP server compiles it into a `.twbx` that Tableau Public opens without errors, then verifies the load by reading Tableau's own logs. Your job is the analyst's job: find the story in the data, choose a few honest charts, and lay them out so a reader understands each dashboard in about 10 seconds.

If the MCP tools (`build_workbook`, `open_in_tableau`, …) are missing, the server isn't installed. Run `./install.sh` from the tableau-builder repo (see its README), then restart Claude Code.

## Workflow (do it in this order; it avoids the usual 5 rounds of rework)

1. **Understand the question.** What decision or assignment is this for? Who reads it (professor, manager)? Note the explicit asks. Each ask becomes a dashboard section or a title.
2. **Profile the data.** Call `profile_data(csv)` for every file. Check types, nulls, cardinality and date ranges. Look for the *wide-table hint* (one column per year), which must be pivoted to long format.
3. **Prepare tidy CSVs with Python before touching Tableau.** One row per entity per period. Pre-compute what makes charts simple:
   - derived metrics (shares, growth %, cumulative sums, "market gain = end − start − net flow")
   - readable units (₹ lakh cr, $M)
   - short category names
   - small summary tables, for example one row per category, or a 1-row KPI table
   Name columns the way you want axis titles to read, units included, e.g. `AUM (Rs lakh cr)`. Pre-computing beats clever calcs: the workbook stays fast and obviously correct.
4. **Write the story first, as text.** For each dashboard, write its one-sentence takeaway and which 2–4 charts prove it. Pick each chart with `references/chart-guide.md`. Split the story into 2–3 dashboards (tabs) rather than one crowded wall.
5. **Write the spec.** Call `spec_reference()` once per session for the exact syntax. Start from the patterns below.
6. **Check, then build.** Run `validate_spec(spec)` and fix any errors (they say "did you mean …"). Then `build_workbook(spec)`.
7. **Verify in Tableau.** `open_in_tableau(twbx)` must return `ok: true`.
   - A `dialogs` entry means Tableau rejected the XML.
   - `invalid_calcs` means a formula is broken.
   - `sheets_not_computed` lists broken views.
8. **Look at it.** If you can take screenshots (computer use), view every dashboard tab and check for truncated labels, squashed charts and wrong scales. Fix them in the spec and rebuild. Never hand over a workbook you haven't verified.
9. **Deliver.** Give the `.twbx` path, what is on each tab, one line per insight, and any caveats. Offer the cleaned data as an `.xlsx` with a README sheet if it's for a submission.

## Design rules (these came from real reviewer feedback; follow them by default)
- **One message per dashboard, at most 3–4 charts.** More than that reads as "chaotic and data-heavy". Use extra tabs instead.
- **Titles state the takeaway and the unit**: "Equity took half of the ₹21 lakh cr of new money", not "Net flow by group".
- **KPI tiles**: one `text` sheet per number (mark `text`, a single `label`, no rows or cols), in a top row about 100–120 px tall. Keep titles short (≤ 25 characters, or they wrap) and format numbers with units.
- **Bars**: sorted by value (`sort` with `by`), labelled (`label`), top-N rather than 40 bars, axes starting at zero. Use a single `mark_color` when colour carries no meaning.
- **Colour**: give each categorical field one fixed palette in `data.fields.<f>.colors` and use the same colours for the same values on every chart. Use about 5 colours at most; fold the rest into "Other". Use a diverging palette only for +/- values.
- **Avoid on a summary dashboard**: dense heatmaps, 36-column tables, dual axes, pie charts with more than 4 slices, 3-D anything. Aggregate (quarters, top categories) first.
- **Scales**: zero-based for bars and areas. A log axis only when values span 2+ orders of magnitude, and say so in the title. Fix axis ranges for animations.
- **Hide helper sheets**: this is the default (`workbook.hide_sheets`). The tab bar should show only the dashboards.
- **Size**: `[1280, 760]` fits laptops and projectors. Pin legends and filter cards in a narrow fixed column (`size` 120–200 px).

## Spec patterns

KPI row + three charts:
```json
{"layout": {"col": [
  {"row": ["KPI Total", "KPI Growth", "KPI Share", "KPI Count"], "size": 110},
  {"row": [{"sheet": "By Category", "weight": 3}, {"sheet": "Trend", "weight": 4},
           {"col": [{"legend": "color", "sheet": "Trend", "size": 140}, {"blank": true}], "size": 150}]}
]}}
```
KPI tile sheet: `{"name": "KPI Total", "ds": "kpi", "mark": "text", "label": "SUM(Total)", "title": "Total AUM, Jul 2026", "title_size": 11, "formats": {"SUM(Total)": "\"Rs \"0.0\" lakh cr\""}}`

Sorted, labelled bar: `{"name": "By Category", "mark": "bar", "rows": ["Category"], "cols": ["SUM(Value)"], "label": "SUM(Value)", "sort": [{"field": "Category", "by": "SUM(Value)"}], "mark_color": "#2a78d6"}`

Animated bubble race (Pages + trails + fixed axes + play control):
```json
{"name": "Race", "mark": "circle", "cols": ["SUM(Cumulative X)"], "rows": ["SUM(Y)"], "size": "SUM(Z)",
 "color": "Group", "detail": ["Entity"], "label": "Entity", "pages": "TMONTH(Month)", "mark_size": 2,
 "axes": {"SUM(Cumulative X)": {"min": -150, "max": 260}, "SUM(Y)": {"min": 0, "max": 11}}}
```
Then on the dashboard: `{"row": ["Race", {"col": [{"pages": "Race", "size": 150}, {"legend": "color", "sheet": "Race"}], "size": 190}]}`

Time series: `"cols": ["TMONTH(Date)"]` for a continuous month axis, `"EXACT(Date)"` when the data is already monthly and you want the axis titled with the field name, `"YEAR(Date)"` for discrete year headers.

Interactivity: a filter card `{"filter": "Region", "sheet": "Trend", "mode": "checkdropdown"}`, a parameter control `{"param": "Top N", "mode": "slider"}`, and actions `[{"type": "filter", "source": "By Category", "targets": ["Trend"]}]`.

## When something isn't in the spec
Use `inspect_workbook` to read any workbook, including a teammate's `.twbx`. To learn an unsupported feature: build the closest spec, open it in Tableau, make the change in the UI, save a copy, `diff_workbooks(original, copy)`, then reproduce it with `patch_workbook`. Always re-run `open_in_tableau` afterwards.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `dialogs`: "Errors occurred while trying to load the workbook" | XML Tableau rejects (usually from a `patch_workbook` op). Revert the patch, or diff against a workbook Tableau saved. |
| `invalid_calcs` | A formula references something that doesn't exist. Use display names in formulas. Aggregating calcs must be used bare. |
| A chart is tiny in the corner | Set the sheet's `fit` to `entire` (the default for charts). |
| A tile collapses in a row | Give it a `size` or `weight`. Axis-less sheets (pie, text) need room. |
| Currency shows the wrong symbol | Write `$#,##0` (the compiler quotes `$`) or `"Rs "0.0`. |
| A map shows axes instead of a map | Set `data.fields.<col>.geo` (state, country, …) and `data.country` for ambiguous names. |
| Numbers too long (8,07,02,20,400) | Pre-scale in the CSV (a `(bn)` column) and add a `formats` entry. |
