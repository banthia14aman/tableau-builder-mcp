"""Self-check: python3 test_tableau_mcp.py   (no Tableau, no network, stdlib only)

Covers the parsers (pills, formulas, formats, shelves) and compiles a spec touching every feature,
then checks the cross-references Tableau's loader requires. Live load-testing is open_in_tableau.
"""
import csv
import random
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tabmcp.compile import Compiler  # noqa: E402
from tabmcp.data import _kind  # noqa: E402
from tabmcp.model import SpecError, Workbook, is_aggregate_formula, tableau_format  # noqa: E402


def make_csv(d: Path) -> Path:
    random.seed(1)
    p = d / "sales.csv"
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Order Date", "State", "Region", "Category", "Sales", "Profit", "Quantity"])
        for i in range(300):
            w.writerow([f"2025-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}", random.choice(["Texas", "Ohio", "Utah"]),
                        random.choice(["East", "West"]), random.choice(["A", "B", "C"]), round(random.uniform(5, 500), 2),
                        round(random.uniform(-50, 120), 2), random.randint(1, 9)])
    return p


def spec(csv_path: Path) -> dict:
    return {
        "workbook": {"name": "Self Test"},
        "data": [{"id": "s", "csv": str(csv_path), "country": "United States",
                  "fields": {"State": {"geo": "state"}, "Sales": {"format": "$#,##0"}, "Category": {"colors": {"A": "#2a78d6", "B": "#eb6834"}}}}],
        "params": [{"name": "Top N", "type": "integer", "value": 2, "range": {"min": 1, "max": 3, "step": 1}},
                   {"name": "Target", "type": "real", "value": 0.1, "range": {"min": 0, "max": 0.5, "step": 0.05}}],
        "calcs": [{"name": "Profit Ratio", "formula": "SUM([Profit]) / SUM([Sales]) // ratio"},
                  {"name": "Hit", "formula": "[Profit Ratio] >= [Target]"},
                  {"name": "State Total", "formula": "{FIXED [State] : SUM([Sales])}"},
                  {"name": "Running", "formula": "RUNNING_SUM(SUM([Sales]))"}],
        "sheets": [
            {"name": "KPI", "mark": "text", "label": "SUM(Sales)", "formats": {"SUM(Sales)": "$#,##0"}},
            {"name": "Trend", "mark": "line", "cols": ["TMONTH(Order Date)"], "rows": ["SUM(Sales)"], "color": "Category",
             "axes": {"SUM(Sales)": {"title": "Sales", "min": 0}}},
            {"name": "Bars", "mark": "bar", "rows": ["Region", "State"], "cols": ["SUM(Sales)"], "label": "SUM(Sales)",
             "filters": [{"field": "State", "top": 2, "by": "SUM(Sales)"}, {"field": "Category", "values": ["C"], "exclude": True},
                         {"field": "SUM(Sales)", "min": 10}], "sort": [{"field": "State", "by": "SUM(Sales)"}]},
            {"name": "Map", "mark": "filled_map", "detail": ["State"], "color": "Profit Ratio", "tooltip": ["State"]},
            {"name": "Race", "mark": "circle", "cols": ["SUM(Sales)"], "rows": ["Profit Ratio"], "detail": ["State"], "color": "Hit",
             "pages": "MONTH(Order Date)", "axes": {"SUM(Sales)": {"min": 0, "max": 50000}}},
            {"name": "Pie", "mark": "pie", "color": "Region", "angle": "SUM(Sales)"},
            {"name": "Run", "mark": "area", "cols": ["EXACT(Order Date)"], "rows": ["Running"]},
        ],
        "dashboards": [{"name": "Home", "title": "Self test", "layout": {"col": [
            {"row": ["KPI", {"text": "hello"}, {"param": "Target", "size": 200}], "size": 100},
            {"row": [{"sheet": "Trend", "weight": 2}, {"col": [{"legend": "color", "sheet": "Trend", "size": 100},
                     {"filter": "Region", "sheet": "Trend", "mode": "checkdropdown", "size": 80}, {"blank": True}], "size": 180}]},
            {"row": ["Bars", "Map", "Pie"]}]},
            "actions": [{"type": "filter", "source": "Bars", "targets": ["Trend"]}, {"type": "highlight", "source": "Pie", "on": "hover"}]},
            {"name": "Anim", "size": "auto", "layout": {"row": ["Race", {"col": [{"pages": "Race", "size": 140}, {"legend": "color", "sheet": "Race"}], "size": 180}, "Run"]}}],
    }


def check_xml(root: ET.Element):
    calcs = {c.get("caption"): c.find("calculation").get("formula") for c in root.iter("column")
             if c.find("calculation") is not None and c.get("caption")}
    for cap, f in calcs.items():                       # RULE: formulas never reference captions of calcs
        for other in calcs:
            if other != cap and f"[{other}]" in f:
                raise AssertionError(f"formula of {cap} references caption [{other}]")
    for ws in root.iter("worksheet"):
        name = ws.get("name")
        declared = {f"[{d.get('datasource')}].{ci.get('name')}" for d in ws.iter("datasource-dependencies") for ci in d.iter("column-instance")}
        refs = []
        t = ws.find("table")
        for tag in ("rows", "cols"):
            refs += re.findall(r"\[federated\.[a-z0-9]+\]\.\[[^\]]*(?:\]\][^\]]*)*\]", t.findtext(tag) or "")
        refs += [e.get("column") for e in ws.iter() if e.tag in ("color", "size", "text", "lod", "tooltip", "wedge-size") and e.get("column")]
        refs += [e.get("column") for e in ws.iter("filter")]
        refs += [e.get("using") for e in ws.iter("sort") if e.get("using")]
        for r in refs:
            if "(generated)" in r:
                continue
            assert r in declared, f"{name}: {r} used but not declared in datasource-dependencies"
    usr = [ci for ci in root.iter("column-instance") if ci.get("derivation") == "User"]
    assert usr and all(ci.get("name").startswith("[usr:") for ci in usr), "aggregate calcs must be usr:"
    sheets = {ws.get("name"): ws for ws in root.iter("worksheet")}
    for z in root.iter("zone"):
        kind = z.get("type-v2")
        if kind == "color":
            enc = [e.get("column") for e in sheets[z.get("name")].iter("color")]
            assert z.get("param") in enc, f"legend param {z.get('param')} not on sheet color"
        if kind == "filter":
            fl = [e.get("column") for e in sheets[z.get("name")].iter("filter")]
            assert z.get("param") in fl, f"filter card {z.get('param')} has no sheet filter"
    assert root.find("mapsources") is not None, "maps need workbook-level <mapsources>"
    assert [w.get("name") for w in root.iter("window") if w.get("maximized") == "true"] == ["Home"]
    assert all(w.get("hidden") == "true" for w in root.iter("window") if w.get("class") == "worksheet")


def main():
    # parsers
    assert _kind("1,234") == "integer" and _kind("1,234.5") == "real" and _kind("2025-03-01") == "date" and _kind("x") == "string"
    assert is_aggregate_formula("SUM([a])/2") and not is_aggregate_formula("{FIXED [s] : SUM([a])}") and not is_aggregate_formula('"SUM(x)"')
    assert tableau_format("$#,##0") == '*"$"#,##0' and tableau_format("0.0%") == "p0.0%" and tableau_format("n#,##0") == "n#,##0"
    with tempfile.TemporaryDirectory() as d:
        p = make_csv(Path(d))
        wb = Workbook(spec(p))
        ds = wb.default_ds
        assert wb.pill("SUM(Sales)", ds).name == "[sum:Sales:qk]"
        assert wb.pill("Profit Ratio", ds).name.startswith("[usr:")
        assert wb.pill("YEAR(Order Date)", ds).name == "[yr:Order Date:ok]"
        assert wb.pill("Region", ds).name == "[none:Region:nk]"
        assert wb.pill("TMONTH(Order Date)", ds).name == "[tmn:Order Date:qk]"
        hit = ds.field("Hit")
        assert "[Parameters].[Parameter 2]" in hit.formula and hit.is_agg          # inherits aggregation from Profit Ratio
        for bad, msg in (("SUM(Profit Ratio)", "already aggregates"), ("YEAR(Region)", "needs a date"), ("Salez", "did you mean")):
            try:
                wb.pill(bad, ds)
                raise AssertionError(f"{bad} should fail")
            except SpecError as e:
                assert msg in str(e), e
        try:
            s = spec(p); s["calcs"] = [{"name": "A", "formula": "[B]+1"}, {"name": "B", "formula": "[A]+1"}]
            Workbook(s)
            raise AssertionError("cycle not detected")
        except SpecError as e:
            assert "circular" in str(e)
        # shelves
        sh = Compiler._shelf([wb.pill("Region", ds), wb.pill("State", ds), wb.pill("SUM(Sales)", ds)])
        assert sh.startswith("(") and " / " in sh and " * " in sh, sh
        # full compile + structural checks
        xml = Compiler(wb).compile()
        root = ET.fromstring(xml)
        check_xml(root)
    print("ok - all self-checks passed")


if __name__ == "__main__":
    main()
