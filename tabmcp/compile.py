"""Spec -> Tableau workbook XML (legacy content model, proven to load in Tableau Public 2026.2).

Every structure here mirrors XML that Tableau itself writes (see research/*.md) or that was
load-tested in Tableau Public 2026.2.3. Hard-won rules are marked RULE.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .data import REMOTE_TYPE, bracket, member
from .model import AGG_NAMES, Field, Param, Pill, SpecError, Workbook, tableau_format

E = ET.SubElement

MARKS = {"auto": "Automatic", "automatic": "Automatic", "bar": "Bar", "line": "Line", "area": "Area",
         "circle": "Circle", "shape": "Shape", "square": "Square", "text": "Text", "pie": "Pie",
         "gantt": "GanttBar", "polygon": "Polygon", "map": "Automatic", "filled_map": "Multipolygon"}
FILTER_MODES = {"dropdown", "checkdropdown", "radiolist", "typeinlist", "checklist", "slider", "pattern"}
PARAM_MODES = {"slider", "type_in", "compact", "list"}


def _real(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    r = repr(float(v))
    return r if ("." in r or "e" in r) else r + ".0"


def quoted(v) -> str:
    """Tableau quoted literal for member/bucket values."""
    if v is None:
        return "%null%"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _real(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("%", "\\%")
    return f'"{s}"'


def _literal(v, datatype: str) -> str:
    if datatype == "string":
        return '"' + str(v).replace('"', '""') + '"'
    if datatype in ("date", "datetime"):
        return f"#{v}#"
    if datatype == "boolean":
        return "true" if v else "false"
    if datatype == "integer":
        return str(int(v))
    return _real(v)


class Compiler:
    def __init__(self, wb: Workbook, app_build: str = "2026.2.3 (20262.26.0912.1023)"):
        self.wb = wb
        self.spec = wb.spec
        self.build = app_build
        self.warnings: list[str] = []
        self.sheets = {s["name"]: s for s in self.spec.get("sheets") or []}
        self.dashboards = self.spec.get("dashboards") or []
        names = list(self.sheets) + [d["name"] for d in self.dashboards]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise SpecError(f"sheet/dashboard names must be unique across both: {sorted(dup)}")
        self.sheet_ctx: dict[str, dict] = {}     # name -> resolved pills etc. (for legends/filters/actions)

    # ================================================================ public
    def compile(self) -> bytes:
        root = ET.Element("workbook", {"original-version": "18.1", "source-build": self.build,
                                       "source-platform": "mac", "version": "18.1"})
        if (self.spec.get("workbook") or {}).get("animate", True):
            man = E(root, "document-format-change-manifest")
            E(man, "AnimationOnByDefault"); E(man, "MarkAnimation")
        prefs = E(root, "preferences")
        E(prefs, "preference", name="ui.encoding.shelf.height", value="24")
        E(prefs, "preference", name="ui.shelf.height", value="26")
        dss = E(root, "datasources")
        if self.wb.params:
            self._params_ds(dss)
        for ds in self.wb.datasources.values():
            self._datasource(dss, ds)
        # resolve sheets first (dashboards, legends and actions refer to them)
        for name, sh in self.sheets.items():
            self.sheet_ctx[name] = self._resolve_sheet(sh)
        self._inject_dashboard_filters()
        if any(c["map"] for c in self.sheet_ctx.values()):
            E(E(root, "mapsources"), "mapsource", name="Tableau")
        acts = [a for d in self.dashboards for a in self._actions(d)]
        if acts:
            ae = E(root, "actions")
            for a in acts:
                ae.append(a)
        wss = E(root, "worksheets")
        for name in self.sheets:
            self._worksheet(wss, name)
        if self.dashboards:
            dbs = E(root, "dashboards")
            for d in self.dashboards:
                self._dashboard(dbs, d)
        self._windows(root)
        ET.indent(root, space="  ")
        return b"<?xml version='1.0' encoding='utf-8' ?>\n" + ET.tostring(root, encoding="utf-8")

    # ================================================================ datasources
    def _params_ds(self, dss):
        pds = E(dss, "datasource", hasconnection="false", inline="true", name="Parameters", version="18.1")
        E(pds, "aliases", enabled="yes")
        for p in self.wb.params.values():
            self._param_column(pds, p, members=True)

    def _param_column(self, parent, p: Param, members: bool):
        lit = _literal(p.value, p.datatype)
        a = {"caption": p.caption, "datatype": p.datatype}
        if p.format:
            a["default-format"] = tableau_format(p.format)
        a.update({"name": bracket(p.internal), "param-domain-type": p.domain, "role": "measure",
                  "type": "nominal" if p.datatype in ("string", "boolean") else "quantitative", "value": lit})
        col = E(parent, "column", a)
        E(col, "calculation", {"class": "tableau", "formula": lit})
        if p.domain == "range" and p.range:
            r = {}
            for k_spec, k_xml in (("step", "granularity"), ("max", "max"), ("min", "min")):
                if p.range.get(k_spec) is not None:
                    v = p.range[k_spec]
                    r[k_xml] = str(int(v)) if p.datatype == "integer" else _real(v)
            E(col, "range", r)
        elif p.domain == "list" and members:
            ms = E(col, "members")
            for v, alias in p.members:
                m = {"value": _literal(v, p.datatype) if p.datatype != "string" else quoted(v)}
                if alias:
                    m["alias"] = alias
                E(ms, "member", m)
        return col

    def _column_decl(self, parent, f: Field):
        a = {}
        if f.caption != f.internal:
            a["caption"] = f.caption
        a["datatype"] = f.datatype
        if f.format:
            a["default-format"] = tableau_format(f.format)
        a["name"] = f.ref
        a["role"] = f.role
        if f.geo:
            a["semantic-role"] = f.geo
        a["type"] = f.type
        if f.geo in ("[Geographical].[Latitude]", "[Geographical].[Longitude]"):
            a["aggregation"] = "Avg"
        if f.hidden:
            a["hidden"] = "true"
        col = E(parent, "column", a)
        if f.is_calc:
            E(col, "calculation", {"class": "tableau", "formula": f.formula})
        if f.aliases:
            al = E(col, "aliases")
            for k, v in f.aliases.items():
                E(al, "alias", key=quoted(k), value=str(v))
        return col

    def _datasource(self, dss, ds):
        d = E(dss, "datasource", caption=ds.id, inline="true", name=ds.name, version="18.1")
        con = E(d, "connection", {"class": "federated"})
        nc = E(E(con, "named-connections"), "named-connection", caption=ds.id, name=ds.conn)
        E(nc, "connection", {"class": "textscan", "directory": member("Data", ds.id), "filename": ds.csv.name,
                              "password": "", "server": "", "workgroup-auth-mode": "as-is"})
        rel = E(con, "relation", connection=ds.conn, name=ds.csv.name, table=f"[{ds.csv.stem}#csv]", type="table")
        cols = E(rel, "columns", {"character-set": "UTF-8", "header": "yes", "locale": "en_US", "separator": ","})
        for c in ds.columns:
            E(cols, "column", datatype=c.datatype, name=c.name, ordinal=str(c.ordinal))
        mr = E(con, "metadata-records")
        for c in ds.columns:
            self._mrec(mr, c, f"[{ds.csv.name}]")
        E(d, "aliases", enabled="yes")
        physical = [ds.by_internal[c.name] for c in ds.columns]
        for f in physical:
            self._column_decl(d, f)
        calcs = [f for f in ds.by_internal.values() if f.is_calc]
        for f in calcs:
            self._column_decl(d, f)
        colored = [f for f in ds.by_internal.values() if f.colors]
        for f in colored:
            E(d, "column-instance", column=f.ref, derivation="None", name=bracket(f"none:{f.internal}:nk"),
              pivot="key", type="nominal")
        ex = E(d, "extract", count="-1", enabled="true", units="records")
        hc = E(ex, "connection", {"access_mode": "readonly", "author-locale": "en_US", "class": "hyper",
                                  "dbname": member("Data", ds.id, f"{ds.id}.hyper"), "default-settings": "hyper",
                                  "schema": "Extract", "sslmode": "", "tablename": "Extract", "update-time": ""})
        E(hc, "relation", name="Extract", table="[Extract].[Extract]", type="table")
        mr2 = E(hc, "metadata-records")
        for c in ds.columns:
            self._mrec(mr2, c, "[Extract]", family=ds.csv.name, approx=getattr(ds, "distinct", {}).get(c.name, c.distinct))
        E(d, "layout", {"dim-ordering": "alphabetic", "dim-percentage": "0.5", "measure-ordering": "alphabetic",
                        "measure-percentage": "0.4", "show-structure": "true"})
        if colored:
            sr = E(E(d, "style"), "style-rule", element="mark")
            for f in colored:
                enc = E(sr, "encoding", attr="color", field=bracket(f"none:{f.internal}:nk"), type="palette")
                for k, hexv in f.colors.items():
                    E(E(enc, "map", to=hexv), "bucket").text = quoted(k)
        country = next((d.get("country") for d in self.spec.get("data") or [] if d.get("id") == ds.id), None)
        if country:
            E(E(d, "semantic-values"), "semantic-value", key="[Country].[Name]", value=quoted(country))
        used = sorted({p for f in calcs for p in self.wb.calc_closure(f)[1]})
        if used:
            dep = E(d, "datasource-dependencies", datasource="Parameters")
            for p in self.wb.params.values():
                if p.internal in used:
                    self._param_column(dep, p, members=False)

    def _mrec(self, parent, c, parent_name, family=None, approx=None):
        m = E(parent, "metadata-record", {"class": "column"})
        agg = "Sum" if c.datatype in ("integer", "real") else ("Year" if c.datatype in ("date", "datetime") else "Count")
        for tag, val in (("remote-name", c.name), ("remote-type", str(REMOTE_TYPE[c.datatype])),
                         ("local-name", bracket(c.name)), ("parent-name", parent_name),
                         ("remote-alias", c.name), ("ordinal", str(c.ordinal))):
            E(m, tag).text = val
        if family:
            E(m, "family").text = family
        E(m, "local-type").text = c.datatype
        E(m, "aggregation").text = agg
        if approx is not None:
            E(m, "approx-count").text = str(approx)
        E(m, "contains-null").text = "true"

    # ================================================================ sheets
    def _resolve_sheet(self, sh: dict) -> dict:
        name = sh.get("name")
        ds = self.wb.ds_for(sh)
        P = lambda x: self.wb.pills(x, ds)
        one = lambda k: (self.wb.pill(sh[k], ds) if sh.get(k) else None)
        ctx = {"ds": ds, "rows": P(sh.get("rows")), "cols": P(sh.get("cols")),
               "color": one("color"), "size": one("size"), "label": one("label"), "shape": one("shape"),
               "angle": one("angle"), "path": one("path"),
               "detail": P(sh.get("detail")), "tooltip": [], "pages": one("pages"),
               "filters": [], "sorts": [], "map": None}
        for t in sh.get("tooltip") or []:
            p = self.wb.pill(t, ds)
            if not p.is_measure and p.agg == "NONE" and p.special is None:     # RULE: tooltip dims are ATTR
                p = self.wb.pill(f"ATTR({p.field.caption})", ds)
            ctx["tooltip"].append(p)
        mark = (sh.get("mark") or "auto").lower()
        if mark not in MARKS:
            raise SpecError(f"sheet '{name}': mark must be one of {sorted(MARKS)}")
        ctx["mark"] = MARKS[mark]
        if mark in ("map", "filled_map"):
            geo = [p for p in ctx["detail"] + [ctx["color"]] if p is not None and p.field is not None and p.field.geo]
            if not geo:
                raise SpecError(f"sheet '{name}': a map needs a geographic field (data.fields.<col>.geo) on detail or color")
            ctx["map"] = mark
        if ctx["pages"] is not None and not ctx["pages"].discrete:
            ctx["pages"] = self.wb.pill(ctx["pages"].text.split(":")[0] + ":d", ds)
        for i, fl in enumerate(sh.get("filters") or []):
            ctx["filters"].append(self._resolve_filter(name, ds, fl))
        for so in sh.get("sort") or []:
            dim = self.wb.pill(so["field"], ds)
            by = self.wb.pill(so["by"], ds) if so.get("by") else None
            d = (so.get("dir") or "desc").upper()
            if d not in ("ASC", "DESC"):
                raise SpecError(f"sheet '{name}': sort dir must be asc or desc")
            ctx["sorts"].append((dim, by, d))
        if not ctx["rows"] and not ctx["cols"] and ctx["mark"] not in ("Text", "Pie") and not ctx["map"]:
            raise SpecError(f"sheet '{name}': needs rows and/or cols (or mark 'text' for a KPI/number tile)")
        return ctx

    def _resolve_filter(self, sheet, ds, fl: dict) -> dict:
        if "field" not in fl:
            raise SpecError(f"sheet '{sheet}': each filter needs 'field'")
        p = self.wb.pill(fl["field"], ds)
        if "values" in fl:
            return {"kind": "values", "pill": p, "values": list(fl["values"]), "exclude": bool(fl.get("exclude"))}
        if "top" in fl:
            by = self.wb.pill(fl["by"], ds)
            return {"kind": "top", "pill": p, "n": int(fl["top"]), "by": by, "bottom": bool(fl.get("bottom"))}
        if "min" in fl or "max" in fl:
            if p.discrete and p.field.datatype not in ("integer", "real", "date", "datetime"):
                raise SpecError(f"sheet '{sheet}': range filter on '{fl['field']}' needs a number/date field")
            if p.discrete:
                p = self.wb.pill(fl["field"].split(":")[0] + ":c", ds)
            return {"kind": "range", "pill": p, "min": fl.get("min"), "max": fl.get("max")}
        return {"kind": "all", "pill": p}        # interactive card with every value selected

    def _inject_dashboard_filters(self):
        """A dashboard filter card needs a matching <filter> on its sheet: add an 'all values' one if absent."""
        for d in self.dashboards:
            for z in _walk(d.get("layout")):
                if isinstance(z, dict) and "filter" in z:
                    sheet = z.get("sheet")
                    if sheet not in self.sheet_ctx:
                        raise SpecError(f"dashboard '{d['name']}': filter card needs 'sheet' (one of {list(self.sheets)})")
                    ctx = self.sheet_ctx[sheet]
                    p = self.wb.pill(z["filter"], ctx["ds"])
                    if not any(f["pill"].name == p.name for f in ctx["filters"]):
                        ctx["filters"].append({"kind": "all", "pill": p})

    def _all_pills(self, ctx) -> list[Pill]:
        ps = ctx["rows"] + ctx["cols"] + ctx["detail"] + ctx["tooltip"]
        ps += [ctx[k] for k in ("color", "size", "label", "shape", "angle", "path", "pages") if ctx[k] is not None]
        ps += [f["pill"] for f in ctx["filters"]] + [f["by"] for f in ctx["filters"] if f.get("by")]
        ps += [s[0] for s in ctx["sorts"]] + [s[1] for s in ctx["sorts"] if s[1] is not None]
        return [p for p in ps if p.special is None]

    def _deps(self, view, ds, pills: list[Pill], extra_fields=()):
        """<datasource-dependencies>: every field + nested calc closure, then one column-instance per pill."""
        fields, params = {}, set()
        for f in [p.field for p in pills] + list(extra_fields):
            fields[f.internal] = f
            if f.is_calc:
                a, b = self.wb.calc_closure(f)
                params |= b
                for i in a:
                    fields[i] = ds.by_internal[i]
        if params:
            pd = E(view, "datasource-dependencies", datasource="Parameters")
            for p in self.wb.params.values():
                if p.internal in params:
                    self._param_column(pd, p, members=False)
        dep = E(view, "datasource-dependencies", datasource=ds.name)
        for f in sorted(fields.values(), key=lambda f: f.internal):
            self._column_decl(dep, f)
        seen = set()
        for p in pills:
            if p.name in seen:
                continue
            seen.add(p.name)
            E(dep, "column-instance", column=p.field.ref, derivation=p.derivation, name=p.name, pivot="key", type=p.itype)
        return params

    @staticmethod
    def _shelf(pills: list[Pill]) -> str:
        """RULE: right-nested, parenthesised; '/' discrete-discrete, '+' continuous-continuous, '*' mixed."""
        if not pills:
            return ""
        expr = pills[-1].qualified
        for i in range(len(pills) - 2, -1, -1):
            a, b = pills[i], pills[i + 1]
            op = "/" if (a.discrete and b.discrete) else "+" if (not a.discrete and not b.discrete) else "*"
            expr = f"({a.qualified} {op} {expr})"
        return expr

    def _filter_xml(self, view, f):
        p = f["pill"]
        if f["kind"] == "range":
            fe = E(view, "filter", {"class": "quantitative", "column": p.qualified, "included-values": "in-range"})
            for k in ("min", "max"):
                if f.get(k) is not None:
                    v = f[k]
                    E(fe, k).text = f"#{v}#" if p.field.datatype in ("date", "datetime") else _real(v)
            return
        fe = E(view, "filter", {"class": "categorical", "column": p.qualified})
        lvl = p.name
        if f["kind"] == "all":
            E(fe, "groupfilter", function="level-members", level=lvl)
        elif f["kind"] == "top":
            by = f["by"]
            fn = by.field.formula if (by.field.is_calc and by.agg == "USER") else f"{by.agg}({by.field.ref})"
            g = E(fe, "groupfilter", {"count": str(f["n"]), "end": "bottom" if f["bottom"] else "top",
                                      "function": "end", "units": "records"})
            g2 = E(g, "groupfilter", {"direction": "ASC" if f["bottom"] else "DESC", "expression": fn, "function": "order"})
            E(g2, "groupfilter", function="level-members", level=lvl)
        else:
            vals = f["values"]
            mem = lambda parent, v: E(parent, "groupfilter", function="member", level=lvl, member=quoted(v))
            if not f["exclude"]:
                if len(vals) == 1:
                    mem(fe, vals[0])
                else:
                    u = E(fe, "groupfilter", function="union")
                    for v in vals:
                        mem(u, v)
            else:
                ex = E(fe, "groupfilter", function="except")
                E(ex, "groupfilter", function="level-members", level=lvl)
                if len(vals) == 1:
                    mem(ex, vals[0])
                else:
                    u = E(ex, "groupfilter", function="union")
                    for v in vals:
                        mem(u, v)

    def _worksheet(self, wss, name):
        sh, ctx = self.sheets[name], self.sheet_ctx[name]
        ds = ctx["ds"]
        ws = E(wss, "worksheet", name=name)
        if sh.get("title") is not False:
            ft = E(E(E(ws, "layout-options"), "title"), "formatted-text")
            E(ft, "run", bold="true", fontsize=str(sh.get("title_size", 13))).text = sh.get("title") or name
            if sh.get("subtitle"):
                E(ft, "run").text = "Æ\n"
                E(ft, "run", fontcolor="#6b6b6b", fontsize="10").text = sh["subtitle"]
        table = E(ws, "table")
        view = E(table, "view")
        dsl = E(view, "datasources")
        E(dsl, "datasource", caption=ds.id, name=ds.name)
        if ctx["map"]:
            E(E(view, "mapsources"), "mapsource", name="Tableau")
        pills = self._all_pills(ctx)
        params = self._deps(view, ds, pills)
        if params:
            E(dsl, "datasource", name="Parameters")
            # RULE: Parameters deps block must come first -> move it before the ds block
            deps = [c for c in view if c.tag == "datasource-dependencies"]
            if len(deps) == 2 and deps[0].get("datasource") != "Parameters":
                view.remove(deps[1]); view.insert(1, deps[1])
        for f in ctx["filters"]:
            self._filter_xml(view, f)
        for dim, by, d in ctx["sorts"]:          # RULE: legacy model wants <sort class=...>, not computed-sort
            if by is None:
                E(view, "sort", {"class": "alphabetic", "column": dim.qualified, "direction": d})
            else:
                E(view, "sort", {"class": "computed", "column": dim.qualified, "direction": d, "using": by.qualified})
        if ctx["filters"]:
            sl = E(view, "slices")
            for f in ctx["filters"]:
                E(sl, "column").text = f["pill"].qualified
        E(view, "aggregation", value="true")
        self._table_style(table, sh, ctx)
        pane = E(E(table, "panes"), "pane", {"selection-relaxation-option": "selection-relaxation-allow"})
        E(E(pane, "view"), "breakdown", value="auto")
        E(pane, "mark", {"class": ctx["mark"]})
        enc = E(pane, "encodings")
        for tag, key in (("color", "color"), ("size", "size"), ("shape", "shape"), ("wedge-size", "angle"), ("text", "label")):
            if ctx[key] is not None:
                E(enc, tag, column=ctx[key].qualified)
        for p in ctx["detail"]:
            E(enc, "lod", column=p.qualified)
        for p in ctx["tooltip"]:
            E(enc, "tooltip", column=p.qualified)
        if ctx["path"] is not None:
            E(enc, "path", column=ctx["path"].qualified)
        if ctx["map"] == "filled_map":
            E(enc, "geometry", column=f"{bracket(ds.name)}.[Geometry (generated)]")
        st = E(E(pane, "style"), "style-rule", element="mark")
        show = sh.get("labels", ctx["label"] is not None or ctx["mark"] == "Text")
        E(st, "format", attr="mark-labels-show", value="true" if show else "false")
        E(st, "format", attr="mark-labels-cull", value="true")
        if sh.get("mark_size") is not None:
            E(st, "format", attr="size", value=_real(sh["mark_size"]))
        if sh.get("mark_color"):
            E(st, "format", attr="mark-color", value=sh["mark_color"])
        if sh.get("opacity") is not None:
            E(st, "format", attr="mark-transparency", value=str(round(float(sh["opacity"]) * 255)))
        rows, cols = self._shelf(ctx["rows"]), self._shelf(ctx["cols"])
        if ctx["map"]:
            rows = rows or f"{bracket(ds.name)}.[Latitude (generated)]"
            cols = cols or f"{bracket(ds.name)}.[Longitude (generated)]"
        if rows:
            E(table, "rows").text = rows
        else:
            E(table, "rows")
        if cols:
            E(table, "cols").text = cols
        else:
            E(table, "cols")
        if ctx["pages"] is not None:
            E(E(table, "pages"), "column").text = ctx["pages"].qualified
            E(table, "page-trail-options", enabled="true" if sh.get("trails", True) else "false", lines="true")

    def _table_style(self, table, sh, ctx):
        rules = []
        axes = sh.get("axes") or {}
        if axes:
            ax = ET.Element("style-rule", element="axis")
            for key, o in axes.items():
                p = self.wb.pill(key, ctx["ds"])
                scope = "rows" if any(q.name == p.name for q in ctx["rows"]) else "cols"
                if o.get("title") is not None:
                    E(ax, "format", {"attr": "title", "class": "0", "field": p.qualified, "scope": scope, "value": o["title"]})
                if o.get("hide"):
                    E(ax, "format", {"attr": "display", "class": "0", "field": p.qualified, "scope": scope, "value": "false"})
                if any(o.get(k) is not None for k in ("min", "max")) or o.get("scale") == "log":
                    a = {"attr": "space", "class": "0", "field": p.qualified, "field-type": "quantitative"}
                    if o.get("max") is not None:
                        a["max"] = _real(o["max"])
                    if o.get("min") is not None:
                        a["min"] = _real(o["min"])
                    if o.get("min") is not None or o.get("max") is not None:
                        a["range-type"] = "fixed"
                    a["scale"] = "log" if o.get("scale") == "log" else "linear"
                    a["scope"] = scope
                    a["type"] = "space"
                    E(ax, "encoding", a)
            rules.append(ax)
        cell = ET.Element("style-rule", element="cell")
        for key, fmt in (sh.get("formats") or {}).items():
            p = self.wb.pill(key, ctx["ds"])
            E(cell, "format", attr="text-format", field=p.qualified, value=tableau_format(fmt))
        if ctx["mark"] == "Text" and not ctx["rows"] and not ctx["cols"]:      # KPI tile look
            for attr, val in (("font-size", str(sh.get("font_size", 26))), ("font-weight", "bold"), ("color", "#1f1f1f"),
                              ("text-align", "center"), ("vertical-align", "center")):
                E(cell, "format", attr=attr, value=val)
        elif sh.get("font_size"):
            E(cell, "format", attr="font-size", value=str(sh["font_size"]))
        if len(cell):
            rules.append(cell)
        if sh.get("palette") and ctx["color"] is not None:
            mk = ET.Element("style-rule", element="mark")
            E(mk, "encoding", attr="color", field=ctx["color"].qualified, palette=sh["palette"], type="interpolated")
            rules.append(mk)
        hdr = ET.Element("style-rule", element="header")
        for key, w in (sh.get("header_width") or {}).items():
            E(hdr, "format", attr="width", field=self.wb.pill(key, ctx["ds"]).qualified, value=str(int(w)))
        if len(hdr):
            rules.append(hdr)
        hide = [s for s in ("rows", "cols") if s in (sh.get("hide_field_labels") or [])]
        if hide:
            wsr = ET.Element("style-rule", element="worksheet")
            for s in hide:
                E(wsr, "format", attr="display-field-labels", scope=s, value="false")
            rules.append(wsr)
        style = E(table, "style")
        for r in rules:
            style.append(r)

    # ================================================================ dashboards
    def _dashboard(self, dbs, d):
        name = d["name"]
        dash = E(dbs, "dashboard", name=name)
        if d.get("title"):
            E(E(E(E(dash, "layout-options"), "title"), "formatted-text"), "run", fontsize="16").text = d["title"]
        E(dash, "style")
        size = d.get("size", [1280, 760])
        if size == "auto":
            E(dash, "size", {"sizing-mode": "automatic"})
            W, H = 1280, 760
        else:
            W, H = int(size[0]), int(size[1])
            E(dash, "size", maxheight=str(H), maxwidth=str(W), minheight=str(H), minwidth=str(W), **{"sizing-mode": "fixed"})
        # dashboard-level dependencies for parameter controls and filter cards
        leaves = [z for z in _walk(d.get("layout")) if not _is_container(z)]
        params = [self.wb.param(z["param"]) for z in leaves if isinstance(z, dict) and "param" in z]
        if params:
            E(E(dash, "datasources"), "datasource", name="Parameters")
            dep = E(dash, "datasource-dependencies", datasource="Parameters")
            for p in {p.internal: p for p in params}.values():
                self._param_column(dep, p, members=True)
        zones = E(dash, "zones")
        self._ids = iter(range(3, 10_000))
        basic = E(zones, "zone", h="100000", id="1", **{"type-v2": "layout-basic"}, w="100000", x="0", y="0")
        top = E(basic, "zone", h="100000", id="2", param="vert", **{"type-v2": "layout-flow"}, w="100000", x="0", y="0")
        y0 = 0
        if d.get("title"):
            th = round(d.get("title_height", 48) / H * 100000)
            self._zone(top, {"type-v2": "title"}, 0, 0, 100000, th)
            y0 = th
        layout = d.get("layout")
        if layout is None:
            raise SpecError(f"dashboard '{name}': needs 'layout'")
        self._place(top, layout, 0, y0, 100000, 100000 - y0, W, H, name)

    def _zone(self, parent, attrs, x, y, w, h, margin=6):
        a = {"h": str(h), "id": str(next(self._ids))}
        a.update(attrs)
        a.update({"w": str(w), "x": str(x), "y": str(y)})
        z = E(parent, "zone", {k: a[k] for k in sorted(a, key=lambda k: (k not in ("h", "id"), k))})
        zs = E(z, "zone-style")
        E(zs, "format", attr="border-style", value="none")
        E(zs, "format", attr="margin", value=str(margin))
        return z

    def _place(self, parent, node, x, y, w, h, W, H, dname):
        if isinstance(node, dict) and ("row" in node or "col" in node):
            horiz = "row" in node
            kids = node["row"] if horiz else node["col"]
            extra = {}
            fx = node.get("_fixed")
            if fx:
                extra = {"fixed-size": str(fx), "is-fixed": "true"}
            flow = self._zone(parent, {**extra, "param": "horz" if horiz else "vert", "type-v2": "layout-flow"}, x, y, w, h, margin=0)
            flow.remove(flow.find("zone-style"))
            total_px = (w / 100000 * W) if horiz else (h / 100000 * H)
            fixed = [(k.get("size") if isinstance(k, dict) else None) for k in kids]
            weights = [0 if f else (k.get("weight", 1) if isinstance(k, dict) else 1) for k, f in zip(kids, fixed)]
            free = max(total_px - sum(f for f in fixed if f), 1)
            wsum = sum(weights) or 1
            px = [f if f else free * wt / wsum for f, wt in zip(fixed, weights)]
            span = w if horiz else h
            pos = x if horiz else y
            used = 0
            for i, (k, p) in enumerate(zip(kids, px)):
                s = span - used if i == len(kids) - 1 else round(p / total_px * span)
                kk = {"sheet": k} if isinstance(k, str) else k
                # RULE: Tableau re-flows unfixed tiles by content (an axis-less pie collapses), so pin every
                # child but the last to its computed pixel size; the last one absorbs the remainder.
                if fixed[i] or i < len(kids) - 1:
                    kk = dict(kk); kk["_fixed"] = int(round(fixed[i] or p))
                if horiz:
                    self._place(flow, kk, pos + used, y, s, h, W, H, dname)
                else:
                    self._place(flow, kk, x, pos + used, w, s, W, H, dname)
                used += s
            return
        z = {"sheet": node} if isinstance(node, str) else node
        fx = z.get("_fixed")
        extra = {"fixed-size": str(fx), "is-fixed": "true"} if fx else {}
        if "text" in z:
            el = self._zone(parent, {**extra, "type-v2": "text"}, x, y, w, h)
            ft = ET.Element("formatted-text")
            r = E(ft, "run", fontsize=str(z.get("font_size", 14)))
            if z.get("bold"):
                r.set("bold", "true")
            if z.get("color"):
                r.set("fontcolor", z["color"])
            r.text = z["text"]
            el.insert(0, ft)
        elif z.get("blank"):
            self._zone(parent, {**extra, "type-v2": "empty"}, x, y, w, h)
        elif "param" in z:
            p = self.wb.param(z["param"])
            mode = z.get("mode", "slider")
            if mode not in PARAM_MODES:
                raise SpecError(f"param control mode must be one of {sorted(PARAM_MODES)}")
            self._zone(parent, {**extra, "mode": mode, "param": p.qualified, "type-v2": "paramctrl"}, x, y, w, h)
        elif "legend" in z:
            sheet = self._need_sheet(z, dname)
            kind = z["legend"] if z["legend"] in ("color", "size", "shape") else "color"
            p = self.sheet_ctx[sheet][kind]
            if p is None:
                raise SpecError(f"dashboard '{dname}': legend '{kind}' but sheet '{sheet}' has nothing on {kind}")
            a = {**extra, "name": sheet, "param": p.qualified, "type-v2": kind}
            if z.get("layout") in ("horz", "vert"):
                a["leg-item-layout"] = z["layout"]
            self._zone(parent, a, x, y, w, h)
        elif "filter" in z:
            sheet = self._need_sheet(z, dname)
            p = self.wb.pill(z["filter"], self.sheet_ctx[sheet]["ds"])
            a = {**extra, "name": sheet, "param": p.qualified, "type-v2": "filter"}
            if z.get("mode"):
                if z["mode"] not in FILTER_MODES:
                    raise SpecError(f"filter card mode must be one of {sorted(FILTER_MODES)}")
                a["mode"] = z["mode"]
            self._zone(parent, a, x, y, w, h)
        elif "pages" in z:
            sheet = z["pages"]
            if sheet not in self.sheet_ctx or self.sheet_ctx[sheet]["pages"] is None:
                raise SpecError(f"dashboard '{dname}': pages control needs a sheet with 'pages' set")
            self._zone(parent, {**extra, "name": sheet, "type-v2": "currpage"}, x, y, w, h)
        elif "sheet" in z:
            sheet = self._need_sheet(z, dname)
            a = {**extra, "name": sheet}
            if z.get("show_title") is False:
                a["show-title"] = "false"
            self._zone(parent, a, x, y, w, h)
        else:
            raise SpecError(f"dashboard '{dname}': unknown layout item {z}")

    def _need_sheet(self, z, dname):
        sheet = z.get("sheet") or (z.get("pages") if "pages" in z else None)
        if sheet not in self.sheets:
            raise SpecError(f"dashboard '{dname}': unknown sheet {sheet!r}; sheets are {list(self.sheets)}")
        return sheet

    # ================================================================ actions
    def _actions(self, d) -> list:
        out = []
        on_dash = set(_dash_sheet_names(d.get("layout")))
        for i, a in enumerate(d.get("actions") or [], 1):
            kind = a.get("type", "filter")
            if kind not in ("filter", "highlight"):
                raise SpecError("action type must be 'filter' or 'highlight'")
            srcs = a.get("source") or []
            srcs = [srcs] if isinstance(srcs, str) else srcs
            tgts = a.get("targets") or sorted(on_dash - set(srcs))
            for s in srcs + tgts:
                if s not in on_dash:
                    raise SpecError(f"action on '{d['name']}': sheet '{s}' is not on that dashboard")
            act = ET.Element("action", caption=a.get("caption", f"{kind.title()} {i}"),
                             name=f"[Action{len(out) + 1}_{abs(hash((d['name'], i))) % 10**8:08d}]")
            E(act, "activation", {"auto-clear": "true", "type": "on-hover" if a.get("on") == "hover" else "on-select"})
            src = E(act, "source", dashboard=d["name"], type="sheet")
            if len(srcs) == 1:
                src.set("worksheet", srcs[0])
            else:
                for s in sorted(on_dash - set(srcs)):
                    E(src, "exclude-sheet", name=s)
            cmd = E(act, "command", command="tsc:tsl-filter" if kind == "filter" else "tsc:brush")
            excl = sorted(on_dash - set(tgts))
            if excl:
                E(cmd, "param", name="exclude", value=",".join(excl))
            E(cmd, "param", name="special-fields", value="all")
            E(cmd, "param", name="target", value=d["name"])
            out.append(act)
        return out

    # ================================================================ windows
    def _windows(self, root):
        win = E(root, "windows", {"source-height": "30"})
        on_any = {s for d in self.dashboards for s in _dash_sheet_names(d.get("layout"))}
        hide = (self.spec.get("workbook") or {}).get("hide_sheets", True)
        maximized = self.dashboards[0]["name"] if self.dashboards else next(iter(self.sheets), None)
        for name, sh in self.sheets.items():
            a = {"class": "worksheet"}
            if hide and name in on_any:
                a["hidden"] = "true"              # RULE: sheets used on dashboards stay out of the tab bar
            if name == maximized:
                a["maximized"] = "true"
            a["name"] = name
            w = E(win, "window", a)
            cards = E(w, "cards")
            left = E(E(cards, "edge", name="left"), "strip", size="160")
            for t in ("pages", "filters", "marks"):
                E(left, "card", type=t)
            topc = E(cards, "edge", name="top")
            for t, sz in (("columns", "2147483647"), ("rows", "2147483647"), ("title", "31")):
                E(E(topc, "strip", size=sz), "card", type=t)
            ctx = self.sheet_ctx[name]
            right = []
            if ctx["pages"] is not None:
                right.append({"type": "currpage"})
            if ctx["color"] is not None:
                right.append({"param": ctx["color"].qualified, "type": "color"})
            if right:
                rs = E(E(cards, "edge", name="right"), "strip", size="160")
                for c in right:
                    E(rs, "card", c)
            fit = self._fit(sh, ctx)
            vp = E(w, "viewpoint")
            if fit:
                E(vp, "zoom", type=fit)
        for d in self.dashboards:
            a = {"class": "dashboard"}
            if d["name"] == maximized:
                a["maximized"] = "true"
            a["name"] = d["name"]
            w = E(win, "window", a)
            vps = E(w, "viewpoints")
            for s in dict.fromkeys(_dash_sheet_names(d.get("layout"))):
                vp = E(vps, "viewpoint", name=s)
                fit = self._fit(self.sheets[s], self.sheet_ctx[s])
                if fit:                            # RULE: without zoom, dashboard charts don't fill their zone
                    E(vp, "zoom", type=fit)
            E(w, "active", id="-1")

    @staticmethod
    def _fit(sh, ctx):
        f = sh.get("fit")
        if f is None:   # KPI/text tiles look best at standard fit, charts at entire view
            f = "standard" if (ctx["mark"] == "Text" and not ctx["rows"] and not ctx["cols"]) else "entire"
        return {"entire": "entire-view", "width": "fit-width", "height": "fit-height", "standard": None}[f]


def _is_container(z) -> bool:
    return isinstance(z, dict) and ("row" in z or "col" in z)


def _walk(node):
    if node is None:
        return
    yield node
    if _is_container(node):
        for k in node.get("row") or node.get("col") or []:
            yield from _walk(k)


def _dash_sheet_names(layout) -> list:
    out = []
    for z in _walk(layout):
        if isinstance(z, str):
            out.append(z)
        elif isinstance(z, dict) and "sheet" in z and not any(k in z for k in ("legend", "filter")):
            out.append(z["sheet"])
    return out
