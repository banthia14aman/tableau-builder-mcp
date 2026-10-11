# Which chart for which question (Tableau mark → spec)

Start from the question the reader asks, not from the data's shape. One chart answers one question.

| Reader's question | Data shape | Best chart | Spec sketch | Avoid |
|---|---|---|---|---|
| "What's the headline number?" | 1 value | **KPI tile** | `mark: text`, `label: SUM(x)`, no rows/cols | gauges, donuts |
| "Which is biggest / smallest?" (ranking) | category × value | **Sorted horizontal bar**, top-N | `rows:[Cat]`, `cols:[SUM(v)]`, `sort`, `label`, top filter | unsorted bars, pie with >4 slices |
| "How did it change over time?" | date × value (1–5 series) | **Line** (area if the parts add up to a total) | `cols:[TMONTH(Date)]`, `rows:[SUM(v)]`, `color:Series` | 10+ lines (facet, or show top 5) |
| "What makes up the total, over time?" | date × part × value | **Stacked area** | `mark: area`, `color: Part` | more than about 6 parts |
| "What makes up the total, right now?" | part × value | **Stacked bar** (one bar) or sorted bar | `color: Part` on one bar | 3-D pie |
| "Where did the growth come from?" (bridge) | start + components = end | **Stacked horizontal bar** per entity: start (grey) + inflow + gain | long CSV `Entity, Component, Value`; `color: Component` | waterfall with dozens of steps |
| "In vs out?" | period × flow type | **Grouped bars** (`rows:[Period, Flow]`) or diverging bar (+/-) | colour In = blue, Out = orange, Net = green | dual axis |
| "Is X related to Y?" | entity × 2 measures (+size) | **Scatter / bubble** | `cols:[SUM(x)]`, `rows:[SUM(y)]`, `detail:[Entity]`, `size` | labelling every point (cull, or label the top few) |
| "How did the relationship evolve?" | entity × 2 measures × time | **Animated bubble race** | `pages: TMONTH(Date)`, fixed axes, `{"pages": sheet}` control | moving axes |
| "Where?" | geography × value | **Filled map** (rates/shares) or symbol map (counts) | `mark: filled_map`, `detail:[State]`, `geo` + `country` | a filled map of raw totals (big areas dominate) |
| "How is it distributed?" | many values | Histogram (pre-bin in the CSV) or box-plot-like dot strip | bar on a binned column | mean-only bars |
| "Pattern across two categories?" | cat × cat × value (small) | **Highlight table** (square marks + colour), ≤ 12×12 | `mark: square`, `color: SUM(v)` | 40×36 heatmaps on a summary dashboard |
| "Compare against a target" | actual vs target | Bar + reference value in the title, or bullet-style bar | parameter for the target, calc for `Above Target` | traffic-light overload |

## Dashboard recipes by dataset type
- **Financial flows (funds, budgets, transactions)**
  - KPIs: total, net flow, growth %, share from new money.
  - Charts: net flow by group (sorted bar), in vs out by year (grouped bar), growth bridge (stacked bar), AUM over time (area), optional animated race.
- **Sales / e-commerce**
  - KPIs: revenue, profit ratio, orders, AOV.
  - Charts: trend by month (line), top products/regions (sorted bar), profit by state (filled map), segment mix (stacked bar).
  - Interactivity: category filter, top-N parameter.
- **Survey results**
  - KPIs: n, top-2-box intent, % reporting the pain.
  - Charts: one sorted bar per key question (share of respondents, `0%` format), cross-tab highlight table for segment × answer.
  - State n and sampling caveats in a text zone.
  - Use % of respondents, not counts, and say "multi-select" where it applies.
- **Time series of many entities (countries, categories)**
  - Charts: an animated bubble race for engagement, a small set of lines for the top 5, a slope or ranking bar for start vs end.
- **Operations / funnels**
  - KPIs: volume, conversion, time-to-complete.
  - Charts: a funnel as sorted bars (stage order via a sort-key column), trend of conversion (line), breakdown by channel.

## Colour
- **Categorical**: a fixed palette per field, set once in `data.fields.<f>.colors`, at most 5–6 hues. Suggested order: `#2a78d6` blue, `#eb6834` orange, `#1baf7a` teal, `#eda100` amber, `#e87ba4` pink, `#b4b3ad` grey for "other"/baseline.
- **Sequential** (one measure, low → high): a single-hue palette (`blue_10_0`).
- **Diverging** (+/-, vs target): `tableau-orange-blue-diverging`, with 0 as the midpoint.
- Never encode the same thing with two different colours on two charts, and never reuse a status colour (red/green) for a plain category.
