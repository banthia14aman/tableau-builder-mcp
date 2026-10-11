# tableau-builder spec reference

A spec is one JSON object. `build_workbook(spec)` turns it into a `.twbx` (Hyper extracts packaged, as Tableau Public requires); `open_in_tableau(path)` verifies it loads. Paths must be absolute (or `~/...`).

```jsonc
{
  "workbook":   {"name": "Sales Review", "output_dir": "/abs/out", "animate": true, "hide_sheets": true},
  "data":       [ /* one entry per CSV */ ],
  "params":     [ /* optional */ ],
  "calcs":      [ /* optional */ ],
  "sheets":     [ /* worksheets */ ],
  "dashboards": [ /* optional */ ]
}
```
- `animate` (default true) turns on Tableau's mark animations (used by Pages playback and filter changes).
- `hide_sheets` (default true) hides every worksheet that appears on a dashboard, so the tab bar shows only dashboards.
- `file_name` overrides the output file name.

## data
```json
{"id": "sales", "csv": "/abs/sales.csv", "country": "United States",
 "fields": {"State": {"geo": "state"}, "Sales": {"format": "$#,##0", "caption": "Revenue"},
            "Year": {"role": "dimension", "type": "ordinal"},
            "Region": {"colors": {"East": "#2a78d6", "West": "#eb6834"}, "aliases": {"E": "East"}}}}
```
- Types are inferred from **every row** (integer, real, date, datetime, boolean, string). Year-like integers become continuous dimensions; `*_id`/`code`/`zip` integers become dimensions.
- Field overrides: `role` (dimension|measure), `type` (nominal|ordinal|quantitative), `datatype`, `caption`, `format`, `aggregation` (Sum|Avg|…, the default for bare use), `geo` (country|state|city|county|zip|latitude|longitude|iso2|iso3), `colors` (value → hex: a fixed categorical palette), `aliases`, `hidden`.
- `country`: the default country for geocoding state/city names (required when names are ambiguous, for example US states on a machine set to India).
- Wide tables (one column per year) should be pivoted to long format first; `profile_data` warns about them.

## params
```json
{"name": "Top N", "type": "integer", "value": 10, "range": {"min": 3, "max": 25, "step": 1}}
{"name": "Metric", "type": "string", "value": "Sales", "list": ["Sales", "Profit"]}
{"name": "Target", "type": "real", "value": 0.12, "range": {"min": 0, "max": 0.3, "step": 0.01}, "format": "0%"}
```
Use them in formulas by name: `[Top N]`. Show a control on a dashboard with `{"param": "Top N", "mode": "slider"}`.

## calcs
```json
{"name": "Profit Ratio", "formula": "SUM([Profit]) / SUM([Sales])", "format": "0.0%"}
{"name": "Above Target", "formula": "[Profit Ratio] >= [Target]"}
{"name": "State Total",  "formula": "{FIXED [State] : SUM([Sales])}"}
{"name": "Running Sales", "formula": "RUNNING_SUM(SUM([Sales]))"}
```
- Reference fields, other calcs and parameters **by their display name**; the compiler rewrites them to Tableau's internal names. It also detects aggregates, table calcs and LODs (aggregation propagates through nested calcs), infers the datatype (override with `datatype`), and rejects unknown names and circular references with a suggestion.
- Optional keys: `ds` (when there are several data sources), `datatype`, `role`, `type`, `format`, `colors`.

## Field references (pills)
Write them the way Tableau shows a pill: `AGG(Field)` or a bare `Field`, with an optional `:d` / `:c` suffix (discrete or continuous).
- Aggregations: `SUM AVG MIN MAX COUNT COUNTD MEDIAN ATTR STDEV STDEVP VAR VARP`
- Date parts (discrete): `YEAR QUARTER MONTH WEEK DAY WEEKDAY HOUR MINUTE`
- Date truncations (continuous): `TYEAR TQUARTER TMONTH TWEEK TDAY`
- Exact date: `EXACT(Order Date)`
- A bare measure uses its default aggregation (SUM). A bare dimension is used as-is. A bare date uses YEAR. A calc that already aggregates must be used bare (`Profit Ratio`, not `SUM(Profit Ratio)`).
- Specials: `Measure Names`, `Measure Values`.

## sheets
```jsonc
{"name": "Sales by State", "ds": "sales", "mark": "bar",
 "rows": ["State"], "cols": ["SUM(Sales)"],
 "color": "Region", "size": "SUM(Quantity)", "label": "SUM(Sales)", "shape": null, "angle": null, "path": null,
 "detail": ["Customer"], "tooltip": ["SUM(Profit)", "Profit Ratio"],
 "filters": [ /* see below */ ],
 "sort": [{"field": "State", "by": "SUM(Sales)", "dir": "desc"}],
 "pages": "TMONTH(Order Date)", "trails": true,
 "axes": {"SUM(Sales)": {"title": "Revenue ($)", "min": 0, "max": 1000000, "scale": "log", "hide": false}},
 "formats": {"SUM(Sales)": "$#,##0"},
 "title": "Sales by state", "subtitle": "FY 2025", "title_size": 13,
 "labels": true, "mark_size": 1.5, "mark_color": "#2a78d6", "opacity": 0.85, "palette": "tableau-orange-blue-diverging",
 "font_size": 12, "header_width": {"State": 120}, "hide_field_labels": ["rows"], "fit": "entire"}
```
- **mark**: `auto bar line area circle shape square text pie gantt polygon map filled_map`
  - `text` with no rows or cols and a `label` gives a big-number **KPI tile** (centred, bold, 26 pt by default).
  - `pie` takes `color` + `angle`.
  - `map` / `filled_map` need a geo field on `detail` or `color`; latitude and longitude are generated.
- **Shelves**: lists of pills. Discrete pills nest, continuous pills make axes. The compiler writes Tableau's shelf grammar for you.
- **pages**: the Pages shelf (animation with a ▶ play control and trails). Pair it with fixed `axes` ranges so nothing jumps between frames.
- **tooltip**: dimensions are wrapped in ATTR automatically.
- **palette** (continuous colour): a Tableau palette name such as `tableau-orange-blue-diverging`, `blue_10_0`. Categorical colours belong in `data.fields.<f>.colors`.
- **fit**: `entire` (the default for charts), `width`, `height`, `standard` (the default for KPI tiles).

### filters
```json
{"field": "Region", "values": ["East", "West"]}                    // keep these
{"field": "Region", "values": ["South"], "exclude": true}          // drop these
{"field": "State", "top": 10, "by": "SUM(Sales)"}                  // top N ("bottom": true for bottom N)
{"field": "SUM(Sales)", "min": 1000}                                // range on an aggregate ("max" too)
{"field": "Order Date", "min": "2025-01-01", "max": "2025-12-31"}   // range on a date
{"field": "YEAR(Order Date)", "values": [2025]}                     // date part
{"field": "Category"}                                               // all values (pair with a dashboard filter card)
```

## dashboards
```jsonc
{"name": "Overview", "title": "Sales are up 12% on last year", "size": [1280, 760],
 "layout": {"col": [
   {"row": ["KPI Sales", "KPI Profit", "KPI Orders"], "size": 110},
   {"row": [{"sheet": "Trend", "weight": 2},
            {"col": [{"legend": "color", "sheet": "Trend", "size": 120},
                     {"filter": "Category", "sheet": "Trend", "mode": "checkdropdown", "size": 80},
                     {"param": "Top N", "mode": "slider", "size": 70},
                     {"blank": true}], "size": 200}]},
   {"row": ["Top States", "Mix"]}
 ]},
 "actions": [{"type": "filter", "source": "Top States", "targets": ["Trend"]},
             {"type": "highlight", "source": "Mix", "on": "hover"}]}
```
- `size`: `[w, h]` pixels (fixed; 1280×760 suits laptops and projectors) or `"auto"`.
- `layout`: a tree of `{"row": [...]}` / `{"col": [...]}`. A child takes `size` (fixed pixels along the parent's axis) or `weight` (a share of the remaining space; default 1). Every child but the last is pinned to its computed size so Tableau doesn't re-flow tiles.
- **Leaves**:
  - a sheet name, or `{"sheet": "X", "show_title": false}`
  - `{"legend": "color"|"size"|"shape", "sheet": "X", "layout": "horz"|"vert"}`
  - `{"filter": "Field", "sheet": "X", "mode": "checkdropdown"|"dropdown"|"radiolist"|"typeinlist"|"slider"}`: a matching filter is added to the sheet automatically if it has none
  - `{"param": "Name", "mode": "slider"|"type_in"|"compact"|"list"}`
  - `{"pages": "X"}`: the play control for a sheet with `pages`
  - `{"text": "…", "font_size": 14, "bold": true, "color": "#52514e"}`
  - `{"blank": true}`
- `actions`: `filter` or `highlight`; `source` is one sheet or a list; `targets` default to the other sheets on the dashboard; `on`: `select` (default) or `hover`.
- The first dashboard opens when the workbook opens. Names must be unique across sheets **and** dashboards.

## Number formats
Excel-style codes: `#,##0` · `0.0%` · `$#,##0` · `0.0"K"` · `"Rs "0.0" lakh cr"`.
- An unquoted `$` is forced to a literal dollar sign (Tableau would otherwise show the OS currency).
- Scale big numbers in the data (for example a `Sales (M)` column) rather than in the format.

## Escape hatches
- `patch_workbook(path, ops)`: set attributes, replace or insert raw XML, remove nodes, by ElementTree XPath.
- `diff_workbooks(a, b)`: learn any feature's XML. Change it once in Tableau's UI, save a copy, diff, then patch.
- `inspect_workbook(path)`: read any workbook, including your teammates' .twbx files.

## Design rules that make dashboards readable (learned from real feedback)
1. **One message per dashboard, at most 3–4 charts.** Split a story across 2–3 dashboards (tabs) instead of cramming.
2. **Titles state the takeaway** ("Equity took half of the new money"), and every axis or title names its **unit** ("Rs lakh cr").
3. **KPI tiles**: 3–4 `text` sheets in a fixed-height row (about 100–120 px) with short titles (≤ 25 characters, or they wrap).
4. **Sort bars by value** and label them; show top-N rather than 40 bars; zero-based value axes.
5. **One colour meaning per field, everywhere**: set categorical colours once in `data.fields.<f>.colors`. Use a single `mark_color` when colour carries no meaning.
6. **No dense heatmaps or 36-column tables** on a summary dashboard. Aggregate (quarters, top categories) first.
7. **Animation**: put `pages` on a time field, fix both axis ranges, and add a `{"pages": sheet}` control plus a legend in a narrow fixed column.
8. Pre-compute derived columns (shares, cumulative sums, scaled units) in the CSV when that is simpler than a calc. It keeps the workbook fast and the formulas obvious.
9. Validate every build with `open_in_tableau`, and look at it before calling it done.
