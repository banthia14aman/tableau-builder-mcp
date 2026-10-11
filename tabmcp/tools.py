"""Tool implementations (plain functions; server.py exposes them over MCP)."""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

from .compile import Compiler
from .data import build_hyper, package_twbx, profile_csv, read_header, read_twb_bytes, safe_base, wide_hint, write_twb_bytes
from .model import SpecError, Workbook

from .validate import APP_PATH
APP = Path(APP_PATH)


def app_build() -> str:
    try:
        plist = APP / "Contents" / "Info.plist"
        get = lambda k: subprocess.run(["/usr/libexec/PlistBuddy", "-c", f"Print :{k}", str(plist)],
                                       capture_output=True, text=True, timeout=5).stdout.strip()
        return f"{get('CFBundleShortVersionString')} ({get('CFBundleVersion')})"
    except Exception:  # noqa: BLE001
        return "2026.2.3 (20262.26.0912.1023)"


# ---------------------------------------------------------------- profile
def profile_data(path: str, max_rows: int | None = None) -> dict:
    cols, rows = profile_csv(path, max_rows)
    out = {"path": str(path), "rows": rows, "columns": []}
    for c in cols:
        d = {"name": c.name, "datatype": c.datatype, "role": c.role, "type": c.type, "nulls": c.nulls,
             "distinct": c.distinct, "samples": c.samples[:3]}
        if c.min is not None:
            d["min"], d["max"] = str(c.min), str(c.max)
        if c.mean is not None:
            d["mean"] = round(c.mean, 4)
        if c.geo:
            d["geo_role"] = c.geo
        if c.top:
            d["top_values"] = c.top[:8]
        out["columns"].append(d)
    hint = wide_hint(read_header(Path(path)))
    if hint:
        out["hint"] = hint
    return out


# ---------------------------------------------------------------- build
def validate_spec(spec: dict) -> dict:
    wb = Workbook(spec)
    c = Compiler(wb, app_build())
    xml = c.compile()
    ET.fromstring(xml)
    return {"ok": True, "sheets": list(c.sheets), "dashboards": [d["name"] for d in c.dashboards],
            "datasources": {k: {"rows": v.rows, "fields": sorted(v.fields)} for k, v in wb.datasources.items()},
            "parameters": sorted(wb.params), "warnings": c.warnings}


def build_workbook(spec: dict) -> dict:
    wb = Workbook(spec)
    c = Compiler(wb, app_build())
    xml = c.compile()
    ET.fromstring(xml)                                       # well-formed or raise
    w = spec["workbook"]
    out = Path(w.get("output_dir") or Path(next(iter(wb.datasources.values())).csv).parent / "tableau").expanduser()
    if not out.is_absolute() and w.get("base_dir"):
        out = Path(w["base_dir"]).expanduser() / out
    out.mkdir(parents=True, exist_ok=True)
    base = safe_base(w.get("file_name") or wb.name)
    payloads, extracts = [], {}
    for ds in wb.datasources.values():
        d = out / "Data" / ds.id
        d.mkdir(parents=True, exist_ok=True)
        hy = d / f"{ds.id}.hyper"
        info = build_hyper(ds.csv, ds.columns, hy)
        csv_copy = d / ds.csv.name
        if csv_copy.resolve() != ds.csv.resolve():
            shutil.copyfile(ds.csv, csv_copy)
        payloads += [(f"Data/{ds.id}/{hy.name}", hy), (f"Data/{ds.id}/{ds.csv.name}", csv_copy)]
        extracts[ds.id] = info
    twb = out / f"{base}.twb"
    twb.write_bytes(xml)
    twbx = out / f"{base}.twbx"
    entries = package_twbx(twbx, twb.name, xml, payloads)
    warn = list(c.warnings) + [f"data source '{k}': {sum(v['coercion_failures'].values())} cells could not be parsed "
                               f"as their column type and became NULL: {v['coercion_failures']}"
                               for k, v in extracts.items() if v["coercion_failures"]]
    return {"ok": True, "twbx": str(twbx), "twb": str(twb), "sheets": list(c.sheets),
            "dashboards": [d["name"] for d in c.dashboards], "entries": entries,
            "rows": {k: v["rows"] for k, v in extracts.items()}, "warnings": warn,
            "next": "Call open_in_tableau(twbx) to load it in Tableau Public and check the log for errors."}


# ---------------------------------------------------------------- open + validate
def open_in_tableau(path: str, keep_open: bool = True) -> dict:
    from .validate import validate
    r = validate(path, quit_after=not keep_open)
    d = asdict(r)
    ok = bool(r.loaded and not r.load_failed and not r.invalid_calcs and not any(x["open"] for x in r.dialogs)
              and set(r.expected_sheets) <= set(r.computed_sheets))
    keep = ["loaded", "load_failed", "load_errors", "invalid_calcs", "dialogs", "active_sheet", "expected_sheets",
            "computed_sheets", "copy_path", "timings"]
    res = {"ok": ok, **{k: d[k] for k in keep}}
    missing = sorted(set(r.expected_sheets) - set(r.computed_sheets))
    if missing:
        res["sheets_not_computed"] = missing
    if not ok:
        res["hint"] = ("Load problems: a dialog means the XML was rejected (schema); invalid_calcs means a formula "
                       "references something unknown; sheets_not_computed are broken views. Fix the spec and rebuild.")
    return res


# ---------------------------------------------------------------- inspect / patch / diff
def _load(path: str) -> tuple[ET.Element, bytes, str | None]:
    raw, inner = read_twb_bytes(path)
    if b"<!DOCTYPE" in raw[:2000] or b"<!ENTITY" in raw:
        raise ValueError("refusing a workbook with DOCTYPE/ENTITY declarations")
    return ET.fromstring(raw), raw, inner


def inspect_workbook(path: str) -> dict:
    root, _, _ = _load(path)
    out = {"version": root.get("version"), "source_build": root.get("source-build"), "datasources": [], "worksheets": [], "dashboards": []}
    for ds in root.find("datasources") or []:
        cols = []
        for c in ds.findall("column"):
            calc = c.find("calculation")
            cols.append({k: v for k, v in {"name": c.get("name"), "caption": c.get("caption"), "datatype": c.get("datatype"),
                                           "role": c.get("role"), "type": c.get("type"),
                                           "formula": calc.get("formula") if calc is not None else None}.items() if v})
        out["datasources"].append({"name": ds.get("name"), "caption": ds.get("caption"), "columns": cols})
    for ws in (root.find("worksheets") or []):
        t = ws.find("table")
        pane = t.find("panes/pane") if t is not None else None
        out["worksheets"].append({
            "name": ws.get("name"),
            "mark": pane.find("mark").get("class") if pane is not None and pane.find("mark") is not None else None,
            "rows": (t.findtext("rows") or "") if t is not None else "", "cols": (t.findtext("cols") or "") if t is not None else "",
            "encodings": [(e.tag, e.get("column")) for e in (pane.find("encodings") if pane is not None and pane.find("encodings") is not None else [])],
            "filters": [f.get("column") for f in t.findall("view/filter")] if t is not None else []})
    for d in (root.find("dashboards") or []):
        zones = [{k: z.get(k) for k in ("name", "type-v2", "param", "mode") if z.get(k)} for z in d.iter("zone")]
        out["dashboards"].append({"name": d.get("name"), "zones": [z for z in zones if z]})
    hidden = [w.get("name") for w in root.iter("window") if w.get("hidden") == "true"]
    if hidden:
        out["hidden_sheets"] = hidden
    return out


def patch_workbook(path: str, ops: list[dict], output: str | None = None) -> dict:
    """ops: {"op": "set_attr"|"remove"|"append_xml"|"insert_before_xml"|"replace_xml"|"set_text", "xpath": ..., ...}
    xpath uses ElementTree syntax relative to <workbook>, e.g. ".//worksheet[@name='Sales']/table/panes/pane/mark"."""
    root, _, inner = _load(path)
    parent = {c: p for p in root.iter() for c in p}
    log = []
    for o in ops:
        hits = root.findall(o["xpath"]) if o["xpath"] not in (".", "") else [root]
        if not hits:
            raise ValueError(f"xpath matched nothing: {o['xpath']}")
        for el in hits:
            k = o["op"]
            if k == "set_attr":
                for a, v in o["attrs"].items():
                    if v is None:
                        el.attrib.pop(a, None)
                    else:
                        el.set(a, str(v))
            elif k == "set_text":
                el.text = o["text"]
            elif k == "remove":
                parent[el].remove(el)
            elif k in ("append_xml", "insert_before_xml", "replace_xml"):
                new = ET.fromstring(o["xml"])
                if k == "append_xml":
                    el.append(new)
                else:
                    p = parent[el]
                    i = list(p).index(el)
                    p.insert(i, new)
                    if k == "replace_xml":
                        p.remove(el)
            else:
                raise ValueError(f"unknown op {k}")
        log.append(f"{o['op']} x{len(hits)}: {o['xpath']}")
    ET.indent(root, space="  ")
    data = b"<?xml version='1.0' encoding='utf-8' ?>\n" + ET.tostring(root, encoding="utf-8")
    dst = Path(output or path)
    if dst != Path(path):
        shutil.copyfile(path, dst)
    write_twb_bytes(dst, data, inner)
    return {"ok": True, "path": str(dst), "applied": log, "next": "open_in_tableau to verify it still loads"}


_VOLATILE = re.compile(r"\s(?:uuid|source-build|update-time)='[^']*'|<thumbnail .*?</thumbnail>", re.S)


def diff_workbooks(a: str, b: str, context: int = 2, max_lines: int = 400) -> dict:
    """Normalised XML diff: learn the XML of any feature by changing it once in Tableau's UI and saving."""
    def norm(p):
        root, _, _ = _load(p)
        for el in root.iter():
            el.attrib = dict(sorted(el.attrib.items()))
        ET.indent(root, space=" ")
        return _VOLATILE.sub("", ET.tostring(root, encoding="unicode")).splitlines()
    lines = list(difflib.unified_diff(norm(a), norm(b), fromfile=Path(a).name, tofile=Path(b).name, n=context, lineterm=""))
    return {"changed_lines": sum(1 for l in lines if l[:1] in "+-") - 2 if lines else 0,
            "diff": "\n".join(lines[:max_lines]) + ("\n... (truncated)" if len(lines) > max_lines else "")}
