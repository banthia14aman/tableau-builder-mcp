"""Spec model: data sources, fields, calcs, parameters, and the pill resolver.

Everything the compilers need to turn spec text into Tableau identifiers lives here:
  * formula rewriting (captions -> internal names, parameter names -> [Parameters].[Parameter N])
  * aggregate / table-calc / LOD detection (aggregating calcs are derivation="User", prefix usr)
  * calc datatype/role inference
  * resolve_pill("SUM(Sales):d") -> Pill (column-instance name, qualified ref, derivation, ...)
  * dependency closure for <datasource-dependencies>
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .data import Column, bracket, calc_id, profile_csv, stable_id


class SpecError(ValueError):
    """User-facing spec error (message is actionable)."""


# ------------------------------------------------------------------ derivations
# AGG keyword -> (derivation attr, instance prefix, discrete-by-default)
DERIV = {
    "SUM": ("Sum", "sum", False), "AVG": ("Avg", "avg", False), "MIN": ("Min", "min", False),
    "MAX": ("Max", "max", False), "COUNT": ("Count", "cnt", False), "COUNTD": ("CountD", "ctd", False),
    "MEDIAN": ("Median", "med", False), "ATTR": ("Attribute", "attr", True),
    "STDEV": ("Stdev", "stdev", False), "STDEVP": ("Stdevp", "stdevp", False),
    "VAR": ("Var", "var", False), "VARP": ("Varp", "varp", False),
    # date parts (discrete) and truncations (continuous)
    "YEAR": ("Year", "yr", True), "QUARTER": ("Quarter", "qr", True), "MONTH": ("Month", "mn", True),
    "WEEK": ("Week", "wk", True), "DAY": ("Day", "dy", True), "WEEKDAY": ("Weekday", "wd", True),
    "HOUR": ("Hour", "hr", True), "MINUTE": ("Minute", "mi", True),
    "TYEAR": ("Year-Trunc", "tyr", False), "TQUARTER": ("Quarter-Trunc", "tqr", False),
    "TMONTH": ("Month-Trunc", "tmn", False), "TWEEK": ("Week-Trunc", "twk", False),
    "TDAY": ("Day-Trunc", "tdy", False), "EXACT": ("None", "none", False),
    "NONE": ("None", "none", None), "USER": ("User", "usr", None),
}
AGG_NAMES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "ATTR", "STDEV", "STDEVP", "VAR", "VARP"}
DATE_PARTS = {"YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "WEEKDAY", "HOUR", "MINUTE",
              "TYEAR", "TQUARTER", "TMONTH", "TWEEK", "TDAY", "EXACT"}

_AGG_FN = re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|COUNTD|MEDIAN|ATTR|STDEV|STDEVP|VAR|VARP|PERCENTILE|CORR|COVAR|COVARP|"
                     r"RUNNING_\w+|WINDOW_\w+|RANK\w*|INDEX|FIRST|LAST|SIZE|LOOKUP|PREVIOUS_VALUE|TOTAL|SCRIPT_\w+)\s*\(", re.I)
_TABLE_FN = re.compile(r"\b(RUNNING_\w+|WINDOW_\w+|RANK\w*|INDEX|FIRST|LAST|SIZE|LOOKUP|PREVIOUS_VALUE|TOTAL)\s*\(", re.I)
_SPECIAL = {"Measure Names": ":Measure Names", "Measure Values": "Multiple Values", "Number of Records": "Number of Records"}


# ------------------------------------------------------------------ formula scanning
def _scan(formula: str):
    """Yield (kind, text) tokens: 'str', 'comment', 'ref', 'code'. Refs keep their brackets."""
    i, n, buf = 0, len(formula), []
    while i < n:
        ch = formula[i]
        if ch in "\"'":
            if buf:
                yield "code", "".join(buf); buf = []
            j = i + 1
            while j < n:
                if formula[j] == ch:
                    if j + 1 < n and formula[j + 1] == ch:   # doubled quote escape
                        j += 2; continue
                    break
                j += 1
            yield "str", formula[i:j + 1]; i = j + 1
        elif formula.startswith("//", i):
            if buf:
                yield "code", "".join(buf); buf = []
            j = formula.find("\n", i)
            j = n if j < 0 else j
            yield "comment", formula[i:j]; i = j
        elif ch == "[":
            if buf:
                yield "code", "".join(buf); buf = []
            j = i + 1
            while j < n:
                if formula[j] == "]":
                    if j + 1 < n and formula[j + 1] == "]":
                        j += 2; continue
                    break
                j += 1
            yield "ref", formula[i:j + 1]; i = j + 1
        else:
            buf.append(ch); i += 1
    if buf:
        yield "code", "".join(buf)


def _unbracket(ref: str) -> str:
    return ref[1:-1].replace("]]", "]")


def code_only(formula: str) -> str:
    """Formula with strings/comments removed and refs replaced by X (for function detection)."""
    return "".join(t if k == "code" else (" X " if k == "ref" else " ") for k, t in _scan(formula))


def _strip_lod(code: str) -> str:
    """Remove {FIXED/INCLUDE/EXCLUDE ... : ...} blocks (row-level at the outer level)."""
    out, depth = [], 0
    for ch in code:
        if ch == "{":
            depth += 1
            out.append(" X ") if depth == 1 else None
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def is_aggregate_formula(formula: str) -> bool:
    return bool(_AGG_FN.search(_strip_lod(code_only(formula))))


def is_table_calc_formula(formula: str) -> bool:
    return bool(_TABLE_FN.search(_strip_lod(code_only(formula))))


def infer_calc_datatype(formula: str) -> str:
    c = _strip_lod(code_only(formula)).strip()
    raw = "".join(t if k != "comment" else " " for k, t in _scan(formula))
    up = c.upper()
    if re.match(r"^\s*(IF|CASE|IIF)\b", up) or re.match(r"^\s*IIF\s*\(", up):
        # look at branch results
        branches = re.findall(r"\b(?:THEN|ELSE)\s+(\"[^\"]*\"|'[^']*'|TRUE|FALSE|[-+\d.]+)", raw, re.I)
        if branches:
            b = branches[0]
            if b[0] in "\"'":
                return "string"
            if b.upper() in ("TRUE", "FALSE"):
                return "boolean"
            return "real" if "." in b else "integer" if re.fullmatch(r"[-+]?\d+", b) else "real"
        return "real"
    if re.match(r"^\s*(\"[^\"]*\"|'[^']*')\s*$", raw.strip()):
        return "string"
    if re.match(r"^\s*(STR|LEFT|RIGHT|MID|UPPER|LOWER|TRIM|LTRIM|RTRIM|REPLACE|SPLIT|PROPER|DATENAME)\s*\(", up) \
            or re.search(r"\+\s*(\"|')", raw) or re.search(r"(\"|')\s*\+", raw):
        return "string"
    if re.match(r"^\s*(DATETRUNC|DATEADD|DATE|MAKEDATE|TODAY|DATEPARSE)\s*\(", up):
        return "date"
    if re.match(r"^\s*(NOW|MAKEDATETIME|DATETIME)\s*\(", up):
        return "datetime"
    if re.match(r"^\s*(DATEPART|DATEDIFF|COUNT|COUNTD|INDEX|RANK\w*|SIZE|LEN|FIND|YEAR|MONTH|DAY|QUARTER|WEEK|INT)\s*\(", up):
        return "integer"
    if re.search(r"(<>|!=|<=|>=|(?<![<>!])=|<|>)|\b(AND|OR|NOT|ISNULL|CONTAINS|STARTSWITH|ENDSWITH|IN)\b", up) \
            and not re.search(r"[-+*/]", re.sub(r"(<>|!=|<=|>=)", "", c)):
        return "boolean"
    return "real"


# ------------------------------------------------------------------ model objects
@dataclass
class Field:
    caption: str            # what the spec / user calls it
    internal: str           # name inside [] in XML: physical column name or Calculation_123
    ds: "DataSource"
    datatype: str
    role: str               # dimension | measure
    type: str               # nominal | ordinal | quantitative
    default_agg: str = "Sum"
    formula: str | None = None        # rewritten (XML-ready) formula for calcs
    is_agg: bool = False
    is_table_calc: bool = False
    deps: set = field(default_factory=set)      # internal names of fields referenced
    params: set = field(default_factory=set)    # internal names of parameters referenced
    format: str | None = None
    geo: str | None = None
    colors: dict | None = None
    aliases: dict | None = None
    hidden: bool = False
    column: Column | None = None

    @property
    def is_calc(self) -> bool:
        return self.formula is not None

    @property
    def ref(self) -> str:                 # [Internal]
        return bracket(self.internal)


@dataclass
class Param:
    caption: str
    internal: str           # "Parameter 1"
    datatype: str           # integer real string date datetime boolean
    value: object
    domain: str             # range | list | any
    range: dict | None = None
    members: list | None = None     # [(value, alias)]
    format: str | None = None

    @property
    def qualified(self) -> str:
        return f"[Parameters].{bracket(self.internal)}"


@dataclass
class DataSource:
    id: str
    name: str               # federated.xxx
    conn: str               # textscan.xxx
    csv: Path
    columns: list
    rows: int
    fields: dict            # caption -> Field (physical + calcs)
    by_internal: dict       # internal -> Field

    def field(self, caption: str) -> Field:
        f = self.fields.get(caption) or self.by_internal.get(caption)
        if f is None:
            cands = difflib.get_close_matches(caption, list(self.fields), n=3, cutoff=0.5)
            raise SpecError(f"unknown field '{caption}' in data source '{self.id}'"
                            + (f"; did you mean {cands}?" if cands else f"; fields are {sorted(self.fields)}"))
        return f


# ------------------------------------------------------------------ pills
@dataclass
class Pill:
    text: str
    field: Field | None
    agg: str                # key of DERIV
    derivation: str
    prefix: str
    discrete: bool
    special: str | None = None   # Measure Names / Measure Values

    @property
    def itype(self) -> str:
        """instance @type: nominal | ordinal | quantitative."""
        if not self.discrete:
            return "quantitative"
        f = self.field
        if self.agg in DATE_PARTS or self.agg in AGG_NAMES - {"ATTR"} or self.agg == "USER" and f and f.datatype not in ("string", "boolean"):
            return "ordinal"
        if f is not None and self.agg in ("NONE", "ATTR") and (f.type == "ordinal" or f.datatype in ("integer", "real", "date", "datetime")):
            return "ordinal"
        return "nominal"

    @property
    def suffix(self) -> str:
        return {"nominal": "nk", "ordinal": "ok", "quantitative": "qk"}[self.itype]

    @property
    def name(self) -> str:      # [sum:Sales:qk]
        if self.special:
            return bracket(_SPECIAL[self.special])
        return bracket(f"{self.prefix}:{self.field.internal}:{self.suffix}")

    @property
    def qualified(self) -> str:
        if self.special == "Measure Names" or self.special == "Measure Values":
            return f"{bracket(self.field.ds.name) if self.field else ''}.{self.name}".lstrip(".")
        return f"{bracket(self.field.ds.name)}.{self.name}"

    @property
    def is_measure(self) -> bool:
        """Aggregated / continuous value pill (makes an axis when continuous)."""
        return self.agg in AGG_NAMES - {"ATTR"} or self.agg == "USER" or (
            self.agg == "NONE" and self.field is not None and self.field.role == "measure")


_PILL = re.compile(r"^\s*(?:([A-Za-z]+)\s*\((.*)\)|(.*?))\s*(?::([dc]))?\s*$", re.S)


class Workbook:
    """Normalised spec + registries; shared by all compilers."""

    def __init__(self, spec: dict):
        self.spec = spec
        wb = spec.get("workbook") or {}
        if not wb.get("name"):
            raise SpecError("workbook.name is required")
        self.name = wb["name"]
        self.datasources: dict[str, DataSource] = {}
        self.params: dict[str, Param] = {}
        self._load_params(spec.get("params") or [])
        for d in spec.get("data") or []:
            self._load_ds(d)
        if not self.datasources:
            raise SpecError("spec.data must list at least one CSV data source")
        self.default_ds = next(iter(self.datasources.values()))
        self._load_calcs(spec.get("calcs") or [])

    # -------------------------------------------------------------- loading
    def _load_params(self, plist):
        for i, p in enumerate(plist, 1):
            name = p.get("name")
            if not name:
                raise SpecError("each param needs a name")
            dtype = p.get("type", "real")
            if dtype not in ("integer", "real", "string", "date", "datetime", "boolean"):
                raise SpecError(f"param '{name}': type must be integer/real/string/date/datetime/boolean")
            if "range" in p:
                domain, rng, members = "range", p["range"], None
            elif "list" in p:
                domain, rng = "list", None
                members = [(m["value"], m.get("alias")) if isinstance(m, dict) else (m, None) for m in p["list"]]
            else:
                domain, rng, members = "any", None, None
            if "value" not in p:
                raise SpecError(f"param '{name}' needs a default 'value'")
            self.params[name] = Param(name, f"Parameter {i}", dtype, p["value"], domain, rng, members, p.get("format"))

    def _load_ds(self, d):
        did = d.get("id")
        if not did or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", did):
            raise SpecError("each data source needs an id (letters/digits/underscore, starting with a letter)")
        csv = Path(d.get("csv", "")).expanduser()
        if not csv.is_absolute():          # relative paths resolve against workbook.base_dir (else the server's cwd)
            csv = Path((self.spec.get("workbook") or {}).get("base_dir") or ".").expanduser() / csv
        if not csv.is_file():
            raise SpecError(f"data source '{did}': CSV not found: {csv}")
        cols, rows = profile_csv(csv)
        ds = DataSource(did, stable_id(self.name, did, "datasource", prefix="federated."),
                        stable_id(self.name, did, "connection", prefix="textscan."), csv, cols, rows, {}, {})
        over = d.get("fields") or {}
        unknown = set(over) - {c.name for c in cols}
        if unknown:
            raise SpecError(f"data source '{did}': field overrides for unknown columns {sorted(unknown)}; columns are {[c.name for c in cols]}")
        for c in cols:
            o = over.get(c.name, {})
            if "datatype" in o:
                c.datatype = o["datatype"]
            role = o.get("role", c.role)
            typ = o.get("type") or ("quantitative" if role == "measure" else c.type if role == c.role else
                                     ("nominal" if c.datatype in ("string", "boolean") else "ordinal"))
            geo = o.get("geo")
            geo = (GEO_ALIASES.get(geo.lower(), geo) if geo else c.geo)
            f = Field(caption=o.get("caption", c.name), internal=c.name, ds=ds, datatype=c.datatype, role=role, type=typ,
                      default_agg=o.get("aggregation", c.default_agg), format=o.get("format"), geo=geo,
                      colors=o.get("colors"), aliases=o.get("aliases"), hidden=o.get("hidden", False), column=c)
            ds.fields[f.caption] = f
            if f.caption != c.name:
                ds.fields.setdefault(c.name, f)     # raw column name also resolves
            ds.by_internal[c.name] = f
        self.datasources[did] = ds

    def _load_calcs(self, calcs):
        pending = []
        for c in calcs:
            if not c.get("name") or not c.get("formula"):
                raise SpecError("each calc needs 'name' and 'formula'")
            ds = self.datasources.get(c.get("ds") or c.get("data_source") or self.default_ds.id)
            if ds is None:
                raise SpecError(f"calc '{c['name']}': unknown data source {c.get('ds')}")
            if c["name"] in ds.fields:
                raise SpecError(f"calc '{c['name']}' clashes with an existing field name in '{ds.id}'")
            f = Field(caption=c["name"], internal=c.get("internal") or calc_id(self.name, ds.id, c["name"]), ds=ds,
                      datatype=c.get("datatype", ""), role=c.get("role", ""), type=c.get("type", ""),
                      default_agg="Sum", formula=c["formula"], format=c.get("format"), colors=c.get("colors"),
                      aliases=c.get("aliases"), hidden=c.get("hidden", False))
            ds.fields[f.caption] = f
            ds.by_internal[f.internal] = f
            pending.append((f, c))
        # rewrite formulas now that every caption is registered
        for f, c in pending:
            f.formula, f.deps, f.params = self.rewrite_formula(c["formula"], f.ds, where=f"calc '{f.caption}'")
        # aggregate-ness propagates through references (fixpoint)
        for f, _ in pending:
            f.is_agg = is_aggregate_formula(f.formula)
            f.is_table_calc = is_table_calc_formula(f.formula)
        changed = True
        while changed:
            changed = False
            for f, _ in pending:
                if not f.is_agg and any(f.ds.by_internal[d].is_agg for d in f.deps if d in f.ds.by_internal):
                    f.is_agg = changed = True
                if not f.is_table_calc and any(f.ds.by_internal[d].is_table_calc for d in f.deps if d in f.ds.by_internal):
                    f.is_table_calc = changed = True
        for f, c in pending:
            if not f.datatype:
                f.datatype = infer_calc_datatype(c["formula"])
            if not f.role:
                f.role = "measure" if (f.is_agg or f.datatype in ("integer", "real")) else "dimension"
            if not f.type:
                f.type = ("quantitative" if f.role == "measure" and f.datatype in ("integer", "real")
                          else "nominal" if f.datatype in ("string", "boolean") else
                          "ordinal" if f.datatype in ("date", "datetime") else "quantitative")
        # cycle check
        for f, _ in pending:
            self.calc_closure(f)

    # -------------------------------------------------------------- formulas
    def rewrite_formula(self, formula: str, ds: DataSource, where: str = "formula"):
        out, deps, params = [], set(), set()
        toks = list(_scan(formula))
        k = 0
        while k < len(toks):
            kind, t = toks[k]
            if kind != "ref":
                out.append(t); k += 1; continue
            # qualified [A].[B]
            if k + 2 < len(toks) and toks[k + 1] == ("code", ".") and toks[k + 2][0] == "ref":
                a, b = _unbracket(t), _unbracket(toks[k + 2][1])
                if a == "Parameters":
                    p = self.params.get(b) or next((p for p in self.params.values() if p.internal == b), None)
                    if not p:
                        raise SpecError(f"{where}: unknown parameter [{b}]; parameters are {sorted(self.params)}")
                    out.append(p.qualified); params.add(p.internal)
                else:
                    out.append(t + "." + toks[k + 2][1])
                k += 3; continue
            name = _unbracket(t)
            f = ds.fields.get(name) or ds.by_internal.get(name)
            if f is not None:
                out.append(f.ref); deps.add(f.internal)
            elif name in self.params:
                p = self.params[name]
                out.append(p.qualified); params.add(p.internal)
            else:
                cands = difflib.get_close_matches(name, list(ds.fields) + list(self.params), n=3, cutoff=0.5)
                raise SpecError(f"{where}: unknown field or parameter [{name}]" + (f"; did you mean {cands}?" if cands else ""))
            k += 1
        return "".join(out), deps, params

    def calc_closure(self, f: Field, seen=None) -> tuple[set, set]:
        """All internal field names (and params) a field depends on, transitively."""
        seen = seen if seen is not None else []
        if f.internal in seen:
            cap = {g.internal: g.caption for g in f.ds.by_internal.values()}
            raise SpecError(f"circular calc reference: {' -> '.join(cap.get(x, x) for x in seen + [f.internal])}")
        fields, params = set(), set(f.params)
        for d in f.deps:
            g = f.ds.by_internal.get(d)
            if g is None:
                continue
            fields.add(d)
            if g.is_calc:
                a, b = self.calc_closure(g, seen + [f.internal])
                fields |= a; params |= b
        return fields, params

    # -------------------------------------------------------------- pills
    def ds_for(self, sheet: dict) -> DataSource:
        did = sheet.get("ds") or sheet.get("data_source")
        if did is None:
            return self.default_ds
        if did not in self.datasources:
            raise SpecError(f"sheet '{sheet.get('name')}': unknown data source '{did}'")
        return self.datasources[did]

    def pill(self, text: str, ds: DataSource) -> Pill:
        if not isinstance(text, str) or not text.strip():
            raise SpecError(f"bad field reference {text!r}")
        m = _PILL.match(text)
        agg, inner, bare, dc = m.group(1), m.group(2), m.group(3), m.group(4)
        if agg and agg.upper() not in DERIV:     # e.g. a field literally named "Foo(bar)"
            bare, agg, inner = text.split(":")[0].strip() if dc else text.strip(), None, None
        name = (inner if agg else bare).strip()
        if name.startswith("[") and name.endswith("]"):
            name = _unbracket(name)
        if name in _SPECIAL and name != "Number of Records":
            return Pill(text, None, "NONE", "None", "none", name == "Measure Names", special=name)
        f = ds.field(name)
        a = agg.upper() if agg else None
        if a is None:
            if f.is_calc and f.is_agg:
                a = "USER"
            elif f.role == "measure":
                a = {"Sum": "SUM", "Avg": "AVG", "Count": "COUNT", "CountD": "COUNTD", "Min": "MIN", "Max": "MAX",
                     "Median": "MEDIAN", "Year": "YEAR", "None": "NONE"}.get(f.default_agg, "SUM")
            elif f.datatype in ("date", "datetime"):
                a = "YEAR"
            else:
                a = "NONE"
        if f.is_calc and f.is_agg and a != "USER":
            if a in AGG_NAMES:
                raise SpecError(f"'{text}': '{f.caption}' already aggregates inside its formula; use it bare: '{f.caption}'")
            a = "USER"
        if a in DATE_PARTS and f.datatype not in ("date", "datetime"):
            raise SpecError(f"'{text}': {a} needs a date/datetime field but '{f.caption}' is {f.datatype}")
        deriv, prefix, disc_default = DERIV[a]
        if disc_default is None:     # NONE / USER: discrete unless continuous by nature
            if a == "USER":
                disc_default = f.datatype in ("string", "boolean")
            else:
                disc_default = not (f.type == "quantitative")
        discrete = disc_default if dc is None else dc == "d"
        return Pill(text, f, a, deriv, prefix, discrete)

    def pills(self, items, ds: DataSource) -> list[Pill]:
        if items is None:
            return []
        if isinstance(items, str):
            items = [items]
        return [self.pill(t, ds) for t in items]

    def param(self, name: str) -> Param:
        p = self.params.get(name)
        if p is None:
            raise SpecError(f"unknown parameter '{name}'; parameters are {sorted(self.params)}")
        return p


GEO_ALIASES = {
    "country": "[Country].[Name]", "state": "[State].[Name]", "province": "[State].[Name]",
    "city": "[City].[Name]", "county": "[County].[Name]", "zip": "[ZipCode].[Name]", "postcode": "[ZipCode].[Name]",
    "latitude": "[Geographical].[Latitude]", "longitude": "[Geographical].[Longitude]",
    "iso2": "[Country].[ISO3166_2]", "iso3": "[Country].[ISO3166_3]",
}


# ------------------------------------------------------------------ number formats
def tableau_format(fmt: str | None) -> str | None:
    """Spec format -> Tableau format string with type prefix.

    Already-prefixed values (n.., c.., p.., *.., C1033..) pass through. Otherwise:
    trailing % -> 'p' (percentage), everything else -> '*' (custom, Excel-style codes)."""
    if not fmt:
        return None
    if "$" in fmt and '"$"' not in fmt:          # unquoted $ is localised (shows the OS currency): force a literal
        fmt = fmt.replace("$", '"$"')
    if re.match(r'^(\*|[ncp][#0"$]|C\d{3,5})', fmt):
        return fmt
    if fmt.rstrip().endswith("%") and re.match(r"^[#0,.]+%$", fmt.strip()):
        return "p" + fmt.strip()
    return "*" + fmt
