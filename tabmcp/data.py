"""CSV profiling, type inference, Hyper extract creation and .twbx packaging.

Ported (and fixed) from tableau-public-authoring v0.1 (MIT, Andy Yaro):
ids/bracket/build_hyper/package_twbx.  Fixes: types inferred over ALL rows (no
silent NULLs from row 501), coercion failures are counted and reported,
approx-count is the distinct count, year-like integers default to dimensions,
thousands-separated numbers are recognised.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath

# ---------------------------------------------------------------- ids / names
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def stable_id(*parts: str, prefix: str = "", length: int = 28) -> str:
    """Deterministic Tableau-style id (same spec -> byte-identical .twb)."""
    n = int.from_bytes(hashlib.sha256("\x1f".join(parts).encode()).digest(), "big")
    out = []
    while n and len(out) < length:
        n, r = divmod(n, 36)
        out.append(_ALPHABET[r])
    return prefix + "".join(out).ljust(length, "0")[:length]


def calc_id(*parts: str) -> str:
    n = int.from_bytes(hashlib.sha256("\x1f".join(parts).encode()).digest()[:8], "big")
    return f"Calculation_{n % 10**16:016d}"


def bracket(name: str) -> str:
    """Tableau identifier: [name] with ']' doubled."""
    return f"[{name.replace(']', ']]')}]"


def safe_base(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-") or "workbook"


# ---------------------------------------------------------------- inference
_INT = re.compile(r"^[+-]?\d+$")
_THOUS = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
_REAL = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DATE_FMTS = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y")
_DT_FMTS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M")
_BOOL = {"true", "false", "yes", "no"}

# remote-type codes Tableau writes for CSV columns (corpus: string 129, int 20, real 5, bool 11, date 133, datetime 135)
REMOTE_TYPE = {"string": 129, "integer": 20, "real": 5, "boolean": 11, "date": 133, "datetime": 135}

# column-name heuristics for geographic roles (semantic-role values seen in Tableau-saved XML)
GEO_ROLES = {
    "country": "[Country].[Name]", "nation": "[Country].[Name]",
    "state": "[State].[Name]", "province": "[State].[Name]", "region_state": "[State].[Name]",
    "city": "[City].[Name]", "county": "[County].[Name]",
    "zip": "[ZipCode].[Name]", "postal code": "[ZipCode].[Name]", "postcode": "[ZipCode].[Name]", "zipcode": "[ZipCode].[Name]",
    "latitude": "[Geographical].[Latitude]", "lat": "[Geographical].[Latitude]",
    "longitude": "[Geographical].[Longitude]", "long": "[Geographical].[Longitude]",
    "lon": "[Geographical].[Longitude]", "lng": "[Geographical].[Longitude]",
}


def _parse_date(v: str, fmts) -> dt.datetime | None:
    for f in fmts:
        try:
            return dt.datetime.strptime(v, f)
        except ValueError:
            pass
    return None


def _kind(v: str) -> str:
    """Narrowest type a single non-empty cell fits."""
    if _INT.match(v):
        return "integer"
    if _THOUS.match(v):
        return "real" if "." in v else "integer"
    if _REAL.match(v):
        return "real"
    if v.lower() in _BOOL:
        return "boolean"
    if _parse_date(v, _DATE_FMTS):
        return "date"
    if _parse_date(v, _DT_FMTS):
        return "datetime"
    return "string"


_WIDEN = {  # (current, new) -> merged
    ("integer", "real"): "real", ("real", "integer"): "real",
    ("date", "datetime"): "datetime", ("datetime", "date"): "datetime",
}


def _merge(a: str | None, b: str) -> str:
    if a is None or a == b:
        return b
    return _WIDEN.get((a, b), "string")


@dataclass
class Column:
    name: str
    ordinal: int
    datatype: str = "string"
    nulls: int = 0
    distinct: int = 0
    distinct_capped: bool = False
    min: object = None
    max: object = None
    mean: float | None = None
    top: list = field(default_factory=list)       # [(value, count)] for low-cardinality columns
    samples: list = field(default_factory=list)

    @property
    def geo(self) -> str | None:
        return GEO_ROLES.get(self.name.strip().lower())

    @property
    def yearlike(self) -> bool:
        return (self.datatype == "integer" and isinstance(self.min, int) and isinstance(self.max, int)
                and (1000 <= self.min and self.max <= 2200) and ("year" in self.name.lower() or self.distinct <= 400))

    @property
    def role(self) -> str:
        if self.datatype in ("integer", "real"):
            if self.geo in ("[Geographical].[Latitude]", "[Geographical].[Longitude]"):
                return "measure"
            if self.yearlike or re.search(r"(^|[ _])(id|code|zip)$", self.name.lower()):
                return "dimension"
            return "measure"
        return "dimension"

    @property
    def type(self) -> str:
        if self.role == "measure":
            return "quantitative"
        if self.datatype in ("date", "datetime"):
            return "ordinal"
        if self.yearlike:
            return "quantitative"   # continuous dimension: draws a time axis; use ':d' for discrete headers
        return "nominal" if self.datatype in ("string", "boolean") else "ordinal"

    @property
    def default_agg(self) -> str:
        if self.geo in ("[Geographical].[Latitude]", "[Geographical].[Longitude]"):
            return "Avg"
        if self.datatype in ("integer", "real"):
            return "Sum"
        if self.datatype in ("date", "datetime"):
            return "Year"
        return "Count"


DISTINCT_CAP = 200_000


def read_header(path: Path) -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        hdr = next(csv.reader(fh), [])
    names, seen = [], {}
    for i, h in enumerate(hdr):
        h = h.strip() or f"F{i + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 0
        names.append(h)
    return names


def profile_csv(path: str | Path, max_rows: int | None = None) -> tuple[list[Column], int]:
    """Scan every row: datatype (widening), nulls, distinct, min/max/mean, top values."""
    path = Path(path)
    names = read_header(path)
    cols = [Column(n, i) for i, n in enumerate(names)]
    kinds: list[str | None] = [None] * len(cols)
    sets: list[dict] = [dict() for _ in cols]
    rows = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        r = csv.reader(fh)
        next(r, None)
        for raw in r:
            if not any(c.strip() for c in raw):
                continue
            rows += 1
            if max_rows and rows > max_rows:
                break
            for i, c in enumerate(cols):
                v = raw[i].strip() if i < len(raw) else ""
                if v == "":
                    c.nulls += 1
                    continue
                if kinds[i] != "string":
                    kinds[i] = _merge(kinds[i], _kind(v))
                s = sets[i]
                if not c.distinct_capped:
                    s[v] = s.get(v, 0) + 1
                    if len(s) > DISTINCT_CAP:
                        c.distinct_capped = True
                if len(c.samples) < 5 and v not in c.samples:
                    c.samples.append(v)
    for i, c in enumerate(cols):
        c.datatype = kinds[i] or "string"
        s = sets[i]
        c.distinct = len(s)
        vals = [coerce(v, c.datatype)[0] for v in s]
        vals = [v for v in vals if v is not None]
        if vals and c.datatype in ("integer", "real", "date", "datetime"):
            c.min, c.max = min(vals), max(vals)
            if c.datatype in ("integer", "real"):
                tot = sum(coerce(v, c.datatype)[0] * n for v, n in s.items() if coerce(v, c.datatype)[0] is not None)
                cnt = sum(n for v, n in s.items() if coerce(v, c.datatype)[0] is not None)
                c.mean = tot / cnt if cnt else None
        elif vals and c.datatype == "string":
            c.min, c.max = min(vals), max(vals)
        if c.distinct <= 50:
            c.top = sorted(s.items(), key=lambda kv: -kv[1])[:20]
    return cols, rows


def coerce(v: str, datatype: str):
    """CSV cell -> (python value, ok). '' -> (None, True)."""
    v = (v or "").strip()
    if v == "":
        return None, True
    try:
        if datatype == "integer":
            return int(v.replace(",", "")), True
        if datatype == "real":
            return float(v.replace(",", "") if _THOUS.match(v) else v), True
        if datatype == "boolean":
            return v.lower() in ("true", "yes", "1"), True
        if datatype == "date":
            d = _parse_date(v, _DATE_FMTS)
            return (d.date(), True) if d else (None, False)
        if datatype == "datetime":
            d = _parse_date(v, _DT_FMTS) or _parse_date(v, _DATE_FMTS)
            return (d, True) if d else (None, False)
    except (ValueError, TypeError):
        return None, False
    return v, True


def wide_hint(names: list[str]) -> str | None:
    """Detect wide tables whose headers are years/dates (should be pivoted long)."""
    yearish = [n for n in names if re.fullmatch(r"(1[5-9]|2[0-2])\d\d", n)]
    if len(yearish) >= 5:
        return (f"{len(yearish)} columns look like years ({yearish[0]}..{yearish[-1]}): this is a WIDE table. "
                "Pivot to long format (one row per entity+year) before building time-series views.")
    return None


# ---------------------------------------------------------------- hyper
EXTRACT_SCHEMA = EXTRACT_TABLE = "Extract"


def _sql_type(datatype: str):
    from tableauhyperapi import SqlType
    return {"string": SqlType.text, "integer": SqlType.big_int, "real": SqlType.double,
            "boolean": SqlType.bool, "date": SqlType.date, "datetime": SqlType.timestamp}.get(datatype, SqlType.text)()


def build_hyper(csv_path: Path, cols: list[Column], out_path: Path) -> dict:
    """Write a single-table [Extract].[Extract] Hyper file. Returns rows, distinct counts, coercion failures."""
    from tableauhyperapi import Connection, CreateMode, HyperProcess, Inserter, TableDefinition, TableName, Telemetry
    log_dir = Path(tempfile.gettempdir()) / "tableau-builder" / "hyper-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    table = TableName(EXTRACT_SCHEMA, EXTRACT_TABLE)
    tdef = TableDefinition(table_name=table, columns=[TableDefinition.Column(c.name, _sql_type(c.datatype)) for c in cols])
    failures = {c.name: 0 for c in cols}
    rows = 0
    distinct = {}
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
                      parameters={"log_dir": str(log_dir), "log_file_size_limit": "10M", "log_file_max_count": "2"}) as hp:
        with Connection(endpoint=hp.endpoint, database=str(out_path), create_mode=CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema(EXTRACT_SCHEMA)
            conn.catalog.create_table(tdef)
            with open(csv_path, encoding="utf-8-sig", newline="") as fh, Inserter(conn, tdef) as ins:
                r = csv.reader(fh)
                next(r, None)
                for raw in r:
                    if not any(c.strip() for c in raw):
                        continue
                    rows += 1
                    row = []
                    for c in cols:
                        val, ok = coerce(raw[c.ordinal] if c.ordinal < len(raw) else "", c.datatype)
                        if not ok:
                            failures[c.name] += 1
                        row.append(val)
                    ins.add_row(row)
                if rows:
                    ins.execute()
            for c in cols:
                q = f'SELECT COUNT(DISTINCT "{c.name.replace(chr(34), chr(34) * 2)}") FROM "Extract"."Extract"'
                distinct[c.name] = conn.execute_scalar_query(q)
    return {"rows": rows, "distinct": distinct, "coercion_failures": {k: v for k, v in failures.items() if v}}


# ---------------------------------------------------------------- packaging
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def member(*parts: str) -> str:
    """A path inside the .twbx, as written into the workbook XML (connection directory, hyper dbname) and
    used as the zip member name: always '/'-separated, never os.sep, so a workbook built on Windows is the
    same as one built on macOS."""
    return PurePosixPath(*parts).as_posix()


def _safe_arc(name: str | PurePath) -> str:
    """Zip member name. The zip format requires '/' separators (APPNOTE 4.4.17) and Tableau looks members up
    as Data/<id>/<file>. A PurePath (PureWindowsPath included) is converted with as_posix(); on Windows a
    backslash in a string is a separator too. Elsewhere a backslash is a legal filename character, so it is
    rejected rather than guessed at. Absolute paths, drive letters and '..' are rejected everywhere."""
    if isinstance(name, PurePath):
        name = name.as_posix()
    if sys.platform == "win32":
        name = name.replace("\\", "/")
    if name.startswith("/") or ".." in PurePosixPath(name).parts or "\\" in name or re.match(r"[A-Za-z]:", name):
        raise ValueError(f"unsafe archive path: {name}")
    return name


def package_twbx(twbx_path: Path, twb_name: str, twb_bytes: bytes, payloads: list[tuple[str | PurePath, Path]]) -> list[str]:
    """Zip: one root .twb + data files at their relative archive paths (Data/<id>/...)."""
    entries = []
    tmp = twbx_path.with_suffix(".twbx.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in [(twb_name, twb_bytes)] + [(a, p.read_bytes()) for a, p in payloads]:
            arc = _safe_arc(arc)
            info = zipfile.ZipInfo(arc, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
            entries.append(arc)
    tmp.replace(twbx_path)
    return entries


def read_twb_bytes(path: str | Path) -> tuple[bytes, str | None]:
    """Return (.twb bytes, inner name) for a .twb or .twbx path."""
    path = Path(path)
    if path.suffix.lower() == ".twbx":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".twb") and "/" not in n.strip("/")]
            if len(names) != 1:
                raise ValueError(f"expected exactly one root .twb in {path}, found {names}")
            return zf.read(names[0]), names[0]
    return path.read_bytes(), None


def write_twb_bytes(path: str | Path, twb: bytes, inner: str | None) -> None:
    """Write back a modified .twb into a .twb or .twbx (other archive entries preserved)."""
    path = Path(path)
    if inner is None:
        path.write_bytes(twb)
        return
    tmp = path.with_suffix(".twbx.tmp")
    with zipfile.ZipFile(path) as zi, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        for it in zi.infolist():
            zo.writestr(it, twb if it.filename == inner else zi.read(it.filename))
    tmp.replace(path)
