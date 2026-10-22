"""Self-check: python3 test_tableau_mcp.py   (no Tableau, no network, stdlib only)

Covers the parsers (pills, formulas, formats, shelves) and compiles a spec touching every feature,
then checks the cross-references Tableau's loader requires. Live load-testing is open_in_tableau.
The Windows layer (EXPERIMENTAL) is checked by simulation: sys.platform / IS_WIN, subprocess, ctypes,
environment variables and Program Files trees are faked, so these checks run on macOS.
"""
import ast
import contextlib
import csv
import ctypes
import importlib
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import types
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from tabmcp import data as D  # noqa: E402
from tabmcp import tools as T  # noqa: E402
from tabmcp import validate as V  # noqa: E402
from tabmcp.compile import Compiler  # noqa: E402
from tabmcp.data import _kind  # noqa: E402
from tabmcp.model import SpecError, Workbook, is_aggregate_formula, tableau_format  # noqa: E402


def make_csv(d: Path) -> Path:
    random.seed(1)
    p = d / "sales.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
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


# ---------------------------------------------------------------- Windows layer, simulated on any OS
_MISSING = object()


@contextlib.contextmanager
def patched(obj, **attrs):
    """Temporarily set attributes (creating missing ones) and restore them afterwards."""
    old = {k: getattr(obj, k, _MISSING) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is _MISSING:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


@contextlib.contextmanager
def patched_items(mapping, values: dict):
    """Temporarily set (value) or remove (None) keys of a dict such as os.environ or sys.modules."""
    old = {k: mapping.get(k, _MISSING) for k in values}
    for k, v in values.items():
        if v is None:
            mapping.pop(k, None)
        else:
            mapping[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is _MISSING:
                mapping.pop(k, None)
            else:
                mapping[k] = v


def _forbid(*a, **k):
    raise AssertionError(f"must not be called here: {a} {k}")


def _done(cmd, out=""):
    return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"MZ")
    return p


TASKS = ('"tabpublic.exe","4242","Console","1","312,456 K"\r\n'
         '"tab,public.exe","77","Console","1","1,024 K"\r\n')
NO_TASKS = "INFO: No tasks are running which match the specified criteria.\r\n"


def windows_checks(tmp: Path, csv_path: Path):
    # tasklist parsing: quoted fields with commas, the INFO line (any language), empty output
    assert V.parse_tasklist(TASKS) == [("tabpublic.exe", 4242), ("tab,public.exe", 77)]
    assert V.parse_tasklist(NO_TASKS) == [] and V.parse_tasklist("") == []
    runs = []
    win_app = r"C:\Program Files\Tableau\Tableau Public 2026.2\bin\tabpublic.exe"
    with patched(V, IS_WIN=True, APP_PATH=win_app), \
            patched(subprocess, run=lambda cmd, **kw: runs.append((cmd, kw)) or _done(cmd, TASKS)):
        inst = V.tableau_instances()
        assert V.windows_of(4242) == []
        try:
            V._jxa("x")
            raise AssertionError("osascript must be refused on Windows")
        except RuntimeError:
            pass
    assert inst == [{"pid": 4242, "finished": True, "active": None}, {"pid": 77, "finished": True, "active": None}]
    assert runs[0][0] == ["tasklist", "/FI", "IMAGENAME eq tabpublic.exe", "/FO", "CSV", "/NH"], runs
    assert runs[0][1]["creationflags"] == 0x08000000 and len(runs) == 1      # CREATE_NO_WINDOW; no osascript ran

    # app discovery on a fake Program Files tree, newest version first, x86 included, Desktop ignored
    pf, x86 = tmp / "PF", tmp / "PF86"
    _touch(pf / "Tableau" / "Tableau Public 2025.4" / "bin" / "tabpublic.exe")
    newest = _touch(pf / "Tableau" / "Tableau Public 2026.2" / "bin" / "tabpublic.exe")
    _touch(pf / "Tableau" / "Tableau Public 2027.1" / "bin" / "readme.txt")          # no exe: skipped
    _touch(pf / "Tableau" / "Tableau 2027.1" / "bin" / "tableau.exe")                # Tableau Desktop: skipped
    _touch(x86 / "Tableau" / "Tableau Public 2024.1" / "bin" / "tabpublic.exe")
    env = {"ProgramFiles": str(pf), "ProgramFiles(x86)": str(x86)}
    assert V.find_windows_app(env) == str(newest), V.find_windows_app(env)
    ten = _touch(x86 / "Tableau" / "Tableau Public 2026.10" / "bin" / "tabpublic.exe")
    assert V.find_windows_app(env) == str(ten)                                      # numeric, not string, order
    alt = _touch(tmp / "PF2" / "Tableau" / "Tableau Public 2027.1" / "bin" / "tableau.exe")
    assert V.find_windows_app({"ProgramFiles": str(tmp / "PF2")}) == str(alt)      # renamed-exe fallback
    _touch(tmp / "PF3" / "Tableau" / "Tableau Public 2026.2" / "bin" / "tableaucrashreporter.exe")
    assert V.find_windows_app({"ProgramFiles": str(tmp / "PF3")}) is None          # never a helper exe
    assert V.find_windows_app({}) is None and V.find_windows_app({"ProgramFiles": str(tmp / "nope")}) is None
    # module import under win32: IS_WIN, APP_PATH and LOG_DIR take the Windows branch
    try:
        with patched(sys, platform="win32"), patched_items(os.environ, {"ProgramFiles": str(pf), "ProgramW6432": None,
                                                                         "ProgramFiles(x86)": None}):
            importlib.reload(V)
            assert V.IS_WIN and V.APP_PATH == str(newest), V.APP_PATH
            assert V.LOG_DIR == V._documents_dir() / "My Tableau Repository" / "Logs"
        with patched(sys, platform="win32"), patched_items(os.environ, {"ProgramFiles": str(tmp / "nope"),
                                                                         "ProgramW6432": None, "ProgramFiles(x86)": None}):
            importlib.reload(V)
            assert V.APP_PATH == str(Path(str(tmp / "nope")) / "Tableau" / "Tableau Public" / "bin" / "tabpublic.exe")
    finally:
        importlib.reload(V)
    assert not V.IS_WIN or sys.platform == "win32"

    # app_build on Windows: folder version + exe file version; fallback kept
    with patched(T, _exe_build=lambda exe: "20262.26.0912.1023"):
        assert T._win_app_build(PureWindowsPath(win_app)) == "2026.2 (20262.26.0912.1023)"
        assert T._win_app_build(PureWindowsPath(r"D:\Apps\Tableau Public\bin\tabpublic.exe")) == "2026.2 (20262.26.0912.1023)"
    with patched(T, IS_WIN=True, _exe_build=lambda exe: None):
        assert T.app_build() == T.FALLBACK_BUILD
    assert T._exe_build(tmp / "missing.exe") is None          # unreadable: no crash (not win_app: it may exist on Windows)

    # launch: tabpublic.exe <file>, detached; os.startfile when no exe; macOS 'open' command unchanged
    exe, target = str(newest), r"C:\Users\Me\OneDrive\out\Sales__tv1a2b3c4d.twbx"
    popens = []
    with patched(V, IS_WIN=True, APP_PATH=exe), patched(subprocess, run=_forbid,
                                                       Popen=lambda args, **kw: popens.append((args, kw))):
        V.launch(target)
        V.launch(target, "open-new")
        try:
            V.launch(target, "bogus")
            raise AssertionError("unknown mode accepted")
        except ValueError:
            pass
    args, kw = popens[0]
    assert args == [exe, target] and popens[1][0] == args, popens
    assert kw["creationflags"] == 0x00000008 | 0x00000200 and kw["close_fds"] is True     # DETACHED | NEW_GROUP
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL              # never the MCP stdio pipe
    started = []
    with patched(V, IS_WIN=True, APP_PATH=str(tmp / "missing.exe")), patched(subprocess, Popen=_forbid, run=_forbid), \
            patched(os, startfile=started.append):
        V.launch(target)
    assert started == [target]
    mac = []
    with patched(V, IS_WIN=False, APP_PATH=str(tmp)), patched(subprocess, run=lambda cmd, **kw: mac.append(cmd)):
        V.launch("/w/a.twbx")
        V.launch("/w/a.twbx", "open-new")
    assert mac == [["open", "-g", "-a", str(tmp), "/w/a.twbx"], ["open", "-n", "-g", "-a", str(tmp), "--args", "/w/a.twbx"]]

    # quit_pid on Windows: graceful taskkill (no /F), polls tasklist, and os.kill never runs
    def kill(*a):
        raise AssertionError("os.kill on Windows terminates the process")
    polls = [TASKS, NO_TASKS]
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _done(cmd, "SUCCESS: Sent termination signal to the process with PID 4242.\r\n"
                     if cmd[0] == "taskkill" else (polls.pop(0) if polls else NO_TASKS))
    with patched(V, IS_WIN=True), patched(os, kill=kill), patched(subprocess, run=fake_run), \
            patched(time, sleep=lambda s: None):
        assert V.quit_pid(4242) is True                        # running, then gone after WM_CLOSE
        poll = ["tasklist", "/FI", "PID eq 4242", "/FO", "CSV", "/NH"]
        assert calls == [["taskkill", "/PID", "4242"], poll, poll], calls
        assert V.quit_pid(9999) is True                        # not listed: already gone
        with patched(V, _tasklist=lambda flt: V.parse_tasklist(TASKS)):
            assert V.quit_pid(4242, timeout=0) is False        # still running: gives up, never forces
    assert all("/F" not in c and "/f" not in c for c in calls), calls
    # ...and macOS quit_pid still goes through JXA
    jx = []
    with patched(V, IS_WIN=False, _jxa=lambda *a, **k: jx.append(a) or "gone"):
        assert V.quit_pid(1) is True
        V.tableau_instances()
    assert jx == [(V._JXA_TERMINATE, "1"), (V._JXA_INSTANCES, V.BUNDLE_ID)]
    # static guard: every os.kill(...) call sits in a function whose first statement is `if IS_WIN: ... return`
    for f in [*sorted((ROOT / "tabmcp").glob("*.py")), ROOT / "server.py", ROOT / "test_tableau_mcp.py"]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        is_kill = lambda n: (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "kill"
                             and isinstance(n.func.value, ast.Name) and n.func.value.id == "os")
        total = sum(map(is_kill, ast.walk(tree)))
        guarded = 0
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and any(map(is_kill, ast.walk(fn))):
                body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
                g = body[0]
                assert (isinstance(g, ast.If) and isinstance(g.test, ast.Name) and g.test.id == "IS_WIN"
                        and isinstance(g.body[-1], ast.Return)), f"{f.name}:{fn.name}: os.kill without an IS_WIN guard"
                guarded += sum(map(is_kill, ast.walk(fn)))
        assert total == guarded, f"{f.name}: os.kill outside a guarded function"

    # Documents folder: the shell's answer (OneDrive redirect), else ~/Documents
    with patched(V, IS_WIN=True):
        if not hasattr(ctypes, "windll"):                                          # not on a real Windows
            assert V._documents_dir() == Path.home() / "Documents"
        def shget(hwnd, csidl, token, flags, buf):
            assert csidl == 5 and flags == 0
            buf.value = r"D:\OneDrive - Contoso\Documents"
            return 0
        with patched(ctypes, windll=types.SimpleNamespace(shell32=types.SimpleNamespace(SHGetFolderPathW=shget))):
            assert str(V._documents_dir()) == r"D:\OneDrive - Contoso\Documents"
            assert V._default_log_dir() == Path(r"D:\OneDrive - Contoso\Documents") / "My Tableau Repository" / "Logs"
        fail = types.SimpleNamespace(SHGetFolderPathW=lambda *a: -2147467259)     # E_FAIL
        with patched(ctypes, windll=types.SimpleNamespace(shell32=fail)):
            assert V._documents_dir() == Path.home() / "Documents"
    with patched(V, IS_WIN=False):
        assert V._default_log_dir() == Path.home() / "Documents" / "My Tableau Repository" / "Logs"

    # log tail on Windows opens with FILE_SHARE_READ|WRITE|DELETE so Tableau can still rename the slot
    log = tmp / "log_1.txt"
    log.write_text('{"k":"msg","pid":7,"v":"hi"}\r\n', encoding="utf-8")
    opened = []

    def create_file(path, access, share, sa, disposition, flags, template):
        opened.append((access, share, disposition))
        return os.open(path, os.O_RDONLY)
    msvcrt = types.ModuleType("msvcrt")
    msvcrt.open_osfhandle = lambda h, flags: h
    with patched(V, IS_WIN=True), patched_items(sys.modules, {"msvcrt": msvcrt}), \
            patched(ctypes, WinDLL=lambda name, use_last_error=False: types.SimpleNamespace(CreateFileW=create_file)):
        with V._open_log(log) as fh:
            assert fh.read().startswith(b'{"k"')
        tail = V.LogDirTail(tmp, snap={})
        assert [rec["v"] for _, rec in tail.poll()] == ["hi"]                      # CRLF lines parse
        tail.close()
    assert opened and all(o == (0x80000000, 0x7, 3) for o in opened), opened
    with V._open_log(log) as fh:                                                   # macOS: plain open()
        assert fh.read(1) == b"{"

    # argv[1] on Windows: raw or escaped backslashes both give the real path; the copy's unique name matches
    with patched(V, IS_WIN=True):
        assert V.argv_path(r"C:\Users\Me\wb.twbx") == r"C:\Users\Me\wb.twbx"
        assert V.argv_path(r"C:\\Users\\Me\\wb.twbx") == r"C:\Users\Me\wb.twbx"
        cls = V.Classifier(V.Report())
        cls.feed({"k": "msg", "pid": 5, "v": r'  argv[1]="C:\Users\Me\out\Sales__tv1a2b3c4d.twbx"'})
        assert cls.r.argv1 == r"C:\Users\Me\out\Sales__tv1a2b3c4d.twbx"
        copy = tmp / "Sales__tv1a2b3c4d.twbx"
        assert V.is_target(cls.r.argv1, copy, True) and not V.is_target(cls.r.argv1, copy, False)
        assert not V.is_target(r"C:\Users\Me\out\Other.twbx", copy, True)
    with patched(V, IS_WIN=False):
        assert V.argv_path(r'/Users/me/a \"b\".twbx') == '/Users/me/a "b".twbx'
        assert not V.is_target(cls.r.argv1, copy, True)

    # zip members always '/', whatever the input; unsafe names still refused
    hy, cs = _touch(tmp / "payload" / "s.hyper"), _touch(tmp / "payload" / "sales.csv")
    a = tmp / "a.twbx"
    D.package_twbx(a, "a.twb", b"<workbook/>", [(PureWindowsPath(r"Data\s\s.hyper"), hy), (D.member("Data", "s", cs.name), cs)])
    with patched(sys, platform="win32"):
        b = tmp / "b.twbx"
        D.package_twbx(b, "b.twb", b"<workbook/>", [(r"Data\s\s.hyper", hy), (str(PureWindowsPath("Data", "s", "sales.csv")), cs)])
    for z, twb in ((a, "a.twb"), (b, "b.twb")):
        with zipfile.ZipFile(z) as zf:
            assert zf.namelist() == [twb, "Data/s/s.hyper", "Data/s/sales.csv"], zf.namelist()
    posix_only = [] if sys.platform == "win32" else [r"Data\s\x.csv"]          # a legal filename char off Windows
    for bad in ("/abs.csv", "Data/../x.csv", "C:/x.csv", PureWindowsPath(r"C:\x.csv"), PureWindowsPath(r"..\x.csv"), *posix_only):
        try:
            D.package_twbx(tmp / "bad.twbx", "bad.twb", b"<w/>", [(bad, cs)])
            raise AssertionError(f"unsafe member accepted: {bad!r}")
        except ValueError:
            pass
    with patched(sys, platform="win32"):
        for bad in (r"..\x.csv", r"\abs.csv", r"C:\x.csv"):
            try:
                D.package_twbx(tmp / "bad.twbx", "bad.twb", b"<w/>", [(bad, cs)])
                raise AssertionError(f"unsafe member accepted on Windows: {bad!r}")
            except ValueError:
                pass
    # XML paths: a Windows CSV path (drive, spaces, backslashes) still gives Data/<id>/... with '/'
    wb = Workbook(spec(csv_path))
    wb.default_ds.csv = PureWindowsPath(r"C:\Users\Me\OneDrive\Data Files\sales.csv")
    root = ET.fromstring(Compiler(wb).compile())
    text = [c for c in root.iter("connection") if c.get("class") == "textscan"]
    hyper = [c for c in root.iter("connection") if c.get("class") == "hyper"]
    assert text and all(c.get("directory") == "Data/s" and c.get("filename") == "sales.csv" for c in text)
    assert hyper and all(c.get("dbname") == "Data/s/s.hyper" for c in hyper)
    assert not [v for el in root.iter() for v in el.attrib.values() if "C:\\" in v or "Data\\" in v]
    # install.ps1 stays Windows PowerShell 5.1 syntax (no && / || pipeline chains)
    ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "$ErrorActionPreference = 'Stop'" in ps1 and not re.search(r"&&|\|\|", ps1)


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
        windows_checks(Path(d), p)
    print("ok - all self-checks passed")


if __name__ == "__main__":
    main()
