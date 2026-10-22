"""Headless load validation for Tableau Public (macOS; Windows EXPERIMENTAL) via its JSON-lines logs.

Windows support (IS_WIN) is written from documented Windows behaviour and unit-tested on macOS by
simulation only; it has never run on a Windows machine. It assumes Tableau Public for Windows writes the
same JSON-lines logs to <Documents>/My Tableau Repository/Logs, lives at
%ProgramFiles%/Tableau/Tableau Public <version>/bin/tabpublic.exe (the layout public process databases
show for 7.0 to 2022.2) and starts one process per workbook that logs argv[1]. See the _win helpers.

Load-tested end to end against Tableau Public 2026.2.3: good workbooks report loaded=True with every
dashboard sheet computed (~3 s); schema-rejected workbooks surface the load-error dialog.
Log slots can be truncated and reused by a new process (same inode) - LogDirTail rewinds when that happens.
Target: Tableau Public 2026.2.3 (20262.26.0912.1023), bundle id com.tableausoftware.tableaupublic.
Logs: ~/Documents/My Tableau Repository/Logs/log.txt, log_<N>.txt (+ *_bk.txt previous runs).

VERIFIED from the logs of 2026-10-09 (11 `open -a` calls on World_Progress_Gapminder.twbx):
  * With an instance already running, `open -a <app> <file>` makes the running instance log
    msg "Processing FileOpen event. MainWindow is ready. File:<path>". About 1.1 s later a NEW
    process starts and logs msg '  argv[1]="<path>"'. Each process writes one file: log.txt or
    log_<N>.txt (first free slot). Taking a slot renames that slot's old file to *_bk.txt
    (records "open-log" and "previous-log"). No rotation happened while a process was running
    (log.txt grew to 28 MB in a single process).
  * Load finished: k "end-workspace.open-workbook" with a.rk == "ok" and ctx.wb == the file
    stem (10/10 processes, 1.4-1.8 s after the process started).
  * Each worksheet on the active (maximized) dashboard logs k "begin-compute-model-task.execute"
    with v.ws=<sheet> before load finishes. Sheets whose pills use an invalid calc never log it.
  * Invalid calc: warn msg "InvalidField: [<ds>].[<instance>]: The calculation '<caption>' is
    invalid." It is logged while the workbook is parsed, before load finishes. No dialog and
    no excp record go with it.
  * Error dialog: k begin-commands-controller.invoke-command with v.name ==
    "tabdoc:show-detailed-error-dialog". Its sev is "info", not error. The end record with the
    same a.id means the dialog was dismissed. error-code-id (decimal) equals e.excp-error-code
    (hex) on the excp record logged before it.
  * "Finished rendering sheet: <name>" is logged for broken sheets too, so it does not prove
    the sheet is correct.
  * ts is local time with no zone (startup-info.start-time is UTC). Records are not strictly
    ordered by ts. Use file offsets, never ts, to window the log.

UNVERIFIED (never observed): any load-failure record; loading in the same process (cold
start); the `open -n` mode; terminate() on Tableau; the window-count dialog heuristic.
Only replay(), scan(), read_expectations(), LogDirTail (on synthetic files) and the JXA
window/instance probes (on another app) were run during research. validate()/quit_pid()
were never run, because the research brief forbade opening or closing Tableau.
"""

from __future__ import annotations

import csv
import io
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

IS_WIN = sys.platform == "win32"


# ------------------------------------------------------------------ Windows discovery (EXPERIMENTAL)
def _documents_dir() -> Path:
    """The user's real Documents folder on Windows. OneDrive's folder backup and roaming-profile folder
    redirection move it (e.g. to C:/Users/me/OneDrive/Documents), so ask the shell for CSIDL_PERSONAL (5)
    instead of assuming ~/Documents. Falls back to Path.home()/"Documents" if the call is unavailable or fails."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)                                  # MAX_PATH, as the API requires
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # 0 = S_OK
            return Path(buf.value)
    except Exception:  # noqa: BLE001  (no windll off Windows; any shell failure)
        pass
    return Path.home() / "Documents"


def _default_log_dir() -> Path:
    if not IS_WIN:
        return Path.home() / "Documents" / "My Tableau Repository" / "Logs"
    return _documents_dir() / "My Tableau Repository" / "Logs"


def _version_key(name: str) -> tuple[int, ...]:
    m = re.search(r"(\d+(?:\.\d+)*)\s*$", name)
    return tuple(int(x) for x in m[1].split(".")) if m else ()


WIN_EXE_NAMES = ("tabpublic.exe", "tableaupublic.exe", "tableau.exe")   # in order of preference


def find_windows_app(env: dict | None = None) -> str | None:
    """Newest Tableau Public for Windows: <Program Files>/Tableau/Tableau Public <version>/bin/tabpublic.exe,
    searching %ProgramFiles%, %ProgramW6432% and %ProgramFiles(x86)%. tabpublic.exe is the name public process
    databases list for every version from 7.0 to 2022.2 (no Tableau document names it). In case a later version
    renamed it, tableaupublic.exe or tableau.exe in the same bin folder is accepted too: UNVERIFIED. Nothing
    broader (no tableau*.exe glob): bin/ also holds helper executables (crash reporter and the like), and
    launching one of them with our workbook would be wrong. Without a known name, launch() uses the .twbx
    file association (os.startfile) instead."""
    env = os.environ if env is None else env
    roots: list[str] = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        v = env.get(var)
        if v and v not in roots:
            roots.append(v)
    found = []
    for root in roots:
        for d in Path(root, "Tableau").glob("Tableau Public*"):
            b = d / "bin"
            exe = next((p for p in (b / n for n in WIN_EXE_NAMES) if p.is_file()), None)
            if exe is not None:
                found.append((_version_key(d.name), str(exe)))
    found.sort(key=lambda t: t[0], reverse=True)                     # newest version first; stable for ties
    return found[0][1] if found else None


def _default_app_path() -> str:
    if not IS_WIN:
        return next((str(p) for p in sorted(Path("/Applications").glob("Tableau Public*.app"), reverse=True)), "/Applications/Tableau Public.app")
    return find_windows_app() or str(Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
                                     / "Tableau" / "Tableau Public" / "bin" / "tabpublic.exe")


LOG_DIR = _default_log_dir()
APP_PATH = _default_app_path()
BUNDLE_ID = "com.tableausoftware.tableaupublic"
SLOT_RE = re.compile(r"^log(?:_(\d+))?\.txt$")  # live slots only
ANY_LOG_RE = re.compile(r"^log(?:_\d+)?(?:_bk)?\.txt$")  # live + previous-run files

# ------------------------------------------------------------------ signal patterns
RE_ARGV1 = re.compile(r'^\s*argv\[1\]="(?P<path>.*)"$')
RE_FILEOPEN = re.compile(r"^Processing FileOpen event\. MainWindow is ready\. File:(?P<path>.+)$")
RE_RENDER = re.compile(r"^Finished rendering sheet: (?P<sheet>.+)$")
RE_INVALID = re.compile(
    r"^InvalidField: \[(?P<ds>[^\]]+)\]\.\[(?P<inst>[^\]]+)\]: "
    r"The calculation '(?P<caption>.*)' is invalid\.$", re.S)
RE_THUMB_FAIL = re.compile(r"^ViewThumbnailJob failed in file (?P<file>\S+), line (?P<line>\d+)")
RE_CMD_ARG = re.compile(r'(?P<key>[a-z-]+)="(?P<val>(?:[^"\\]|\\.)*)"')
DIALOG_CMD = "tabdoc:show-detailed-error-dialog"

# warn/error records present in every clean open on this machine (telemetry offline, Qt
# locale files, WebEngine) or produced only by interactive editing. Matched on k + " " + text.
NOISE = re.compile(
    r"EventsConfigurator|endeavour-edpa|xdev-edpa|QTranslator::load|Qt WebEngine locales"
    r"|AudioContext was not allowed|QueryJob: Conflicting QueryCategory|m_dialect is null"
    r"|RegisterDataSources::Execute|^UserFilter for workbook info|missing font famil"
    r"|GPU Identifier|Window position QRect|BrowserAuthenticationHelper|GetBestDialect"
    r"|Normalized extracts cannot be created|ProtocolPool: Returning deleted protocol"
    r"|Setting a QCompleter|ExtendedCacheKey::ComputeSerializedHash"
    r"|Table representation column count is out of sync|CreateExtractAction::Do increment column"
)
# excp records Tableau recovers from by itself. Each is followed by "Skipping the logical query
# cache ..." and the query then succeeds: qp-batch-summary batch-errors:false and marks render.
# The TEMP_NORM_ case also appears with the FINAL, correct workbook (log_8.txt:3383).
BENIGN_EXCP = re.compile(
    r"No such column \[TEMP_NORM_|No such column \[__tableau_internal_object_id__\]"
    r"|The protocol is disconnected!")
BENIGN_FOLLOWUP = re.compile(
    r"^LogicalQuery::CacheDataInMemory::GetTupleSource: unknown error"
    r"|^Skipping the logical query cache because retrieving the data failed")


def vtext(rec: dict) -> str:
    v = rec.get("v")
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


_LONE_BACKSLASH = re.compile(r'(?<!\\)\\(?![\\"])')


def argv_path(raw: str) -> str:
    """The path inside the argv[1]="..." log message. macOS: Tableau escapes it C-style, so unescape.
    Windows (UNVERIFIED): a path is full of backslashes and it is not known whether Tableau escapes them.
    A backslash that is neither doubled nor before a quote means the path was logged as is (C:\\Users\\x
    unescaped has single ones), so it is kept; unescaping it would delete every separator."""
    if IS_WIN and _LONE_BACKSLASH.search(raw):
        return raw
    return unescape(raw)


def same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


# ------------------------------------------------------------------ workbook expectations
@dataclass
class Expect:
    stem: str
    active_sheet: str | None = None       # <window maximized='true'> name
    active_class: str | None = None       # dashboard | worksheet | storyboard
    expected_sheets: set[str] = field(default_factory=set)  # worksheets that must compute
    worksheets: set[str] = field(default_factory=set)


def read_expectations(path: str | Path) -> Expect:
    """Which sheets must compute when this file opens. Tableau opens the window marked
    maximized='true' (exactly one per corpus file; our file's first render matched it)."""
    p = Path(path)
    if p.suffix.lower() == ".twbx":
        with zipfile.ZipFile(p) as z:
            root_twbs = [n for n in z.namelist() if n.lower().endswith(".twb") and "/" not in n]
            root = ET.fromstring(z.read(root_twbs[0]))
    else:
        root = ET.parse(p).getroot()
    exp = Expect(stem=p.stem)
    exp.worksheets = {w.get("name") for w in root.iter("worksheet") if w.get("name")}
    for w in root.iter("window"):
        if w.get("maximized") == "true":
            exp.active_sheet, exp.active_class = w.get("name"), w.get("class")
            break
    if exp.active_class == "dashboard":
        for d in root.iter("dashboard"):
            if d.get("name") == exp.active_sheet:
                exp.expected_sheets = {z.get("name") for z in d.iter("zone")
                                       if z.get("name") in exp.worksheets}
    elif exp.active_class == "worksheet":
        exp.expected_sheets = {exp.active_sheet}
    return exp


# ------------------------------------------------------------------ report + classifier
@dataclass
class Report:
    target: str | None = None
    stem: str | None = None
    log_path: str | None = None
    pid: int | None = None
    argv1: str | None = None
    via: str | None = None                # child | same-process | replay
    workbook_version: str | None = None
    loaded: bool = False
    load_elapsed: float | None = None     # a.elapsed of open-workbook (s)
    load_failed: str | None = None
    load_errors: list[str] = field(default_factory=list)   # Tableau's detailed schema/load error lines
    active_sheet: str | None = None
    expected_sheets: list[str] = field(default_factory=list)
    computed_sheets: list[str] = field(default_factory=list)
    rendered: list[str] = field(default_factory=list)
    invalid_calcs: list[dict] = field(default_factory=list)
    dialogs: list[dict] = field(default_factory=list)
    ds_errors: list[str] = field(default_factory=list)
    query_errors: list[str] = field(default_factory=list)
    excp_errors: list[str] = field(default_factory=list)
    activity_exceptions: list[str] = field(default_factory=list)
    thumbnail_failures: int = 0
    unexpected: list[str] = field(default_factory=list)
    benign: int = 0
    noise: int = 0
    fileopen_paths: list[str] = field(default_factory=list)
    workbook_closed: bool = False         # tabui:close-workbook -> blank BookN afterwards
    closed: bool = False                  # close-log seen (process exited)
    windows: list[dict] | None = None     # CGWindow probe (titles need Screen Recording)
    copy_path: str | None = None          # unique copy that was opened (deleted once closed)
    timings: dict = field(default_factory=dict)

    @property
    def missing_sheets(self) -> list[str]:
        return sorted(set(self.expected_sheets) - set(self.computed_sheets))

    @property
    def status(self) -> str:
        if self.load_failed:
            return "load_failed"
        if self.dialogs:
            return "error_dialog"
        if not self.loaded:
            return "not_loaded"
        if self.invalid_calcs:
            return "invalid_calcs"
        if self.missing_sheets:
            return "sheets_not_computed"
        if self.ds_errors or self.query_errors or self.excp_errors or self.activity_exceptions:
            return "data_errors"
        if self.unexpected or self.thumbnail_failures:
            return "warnings"
        return "ok"

    @property
    def passed(self) -> bool:
        return self.status in ("ok", "warnings")

    def summary(self) -> dict:
        d = asdict(self)
        d.update(status=self.status, passed=self.passed, missing_sheets=self.missing_sheets)
        return d


class Classifier:
    """Feed one process's records in file order. Fills a Report."""

    def __init__(self, report: Report):
        self.r = report
        self._open_dialogs: dict[str, dict] = {}
        self._excp_by_code: dict[str, str] = {}
        self._last_benign = False
        self._n = 0
        self._wb_open = True  # False after close-workbook (then a blank BookN is shown)

    def _wb_match(self, rec: dict) -> bool:
        wb = (rec.get("ctx") or {}).get("wb")
        return self.r.stem is None or wb is None or wb == self.r.stem

    def feed(self, rec: dict) -> None:
        r, k, sev = self.r, rec.get("k", ""), rec.get("sev", "")
        a, v, text = rec.get("a") or {}, rec.get("v"), vtext(rec)
        self._n += 1
        if r.pid is None and isinstance(rec.get("pid"), int):
            r.pid = rec["pid"]
        if k == "detailed-error-msg" and isinstance(v, dict):
            for d in v.get("details") or []:
                r.load_errors += [ln for ln in str(d).splitlines() if ln.strip()][:40]

        if k == "msg" and isinstance(v, str):
            if self._n <= 40 and r.argv1 is None and (m := RE_ARGV1.match(v)):
                r.argv1 = argv_path(m["path"])
            elif m := RE_FILEOPEN.match(v):
                r.fileopen_paths.append(m["path"])
            elif m := RE_RENDER.match(v):
                if self._wb_open and m["sheet"] not in r.rendered:
                    r.rendered.append(m["sheet"])
                return
            elif m := RE_INVALID.match(v):
                d = m.groupdict()
                if d not in r.invalid_calcs:
                    r.invalid_calcs.append(d)
                return
            elif RE_THUMB_FAIL.match(v):
                r.thumbnail_failures += 1
                return

        if k == "load-workbook" and isinstance(v, dict):
            r.workbook_version = v.get("Workbook version")
        elif k == "end-workspace.open-workbook" and self._wb_match(rec):
            if a.get("rk") == "ok":
                r.loaded, r.load_elapsed = True, a.get("elapsed")
            else:  # UNVERIFIED: never observed; rk vocabulary is ok|exception|early-termination
                r.load_failed = f"open-workbook rk={a.get('rk')} rv={json.dumps(a.get('rv'))[:300]}"
        elif k == "begin-legacy-owning-workbook-lifecycle.close-workbook":
            self._wb_open = False
            r.workbook_closed = r.loaded
        elif k == "end-legacy-owning-workbook-lifecycle.new-workbook" and not r.loaded \
                and r.argv1 and not r.load_failed:
            # A file-launched process that shows a blank BookN instead. UNVERIFIED failure path.
            r.load_failed = "blank workbook created instead of loading argv[1]"
        elif k == "close-log":
            r.closed = True
            if not r.loaded and not r.load_failed and (r.argv1 or r.via == "same-process"):
                r.load_failed = "process exited before end-workspace.open-workbook"
        elif k == "begin-compute-model-task.execute" and isinstance(v, dict) and v.get("ws"):
            if self._wb_open and v["ws"] not in r.computed_sheets:
                r.computed_sheets.append(v["ws"])
        elif k in ("begin-commands-controller.invoke-command",
                   "end-commands-controller.invoke-command") \
                and isinstance(v, dict) and v.get("name") == DIALOG_CMD:
            if k.startswith("begin-"):
                args = {m["key"]: unescape(m["val"]) for m in RE_CMD_ARG.finditer(v.get("args", ""))}
                code = args.get("error-code-id")
                hexcode = f"0x{int(code):08X}" if code and code.isdigit() and code != "0" else None
                d = {"message": args.get("error-short-message"), "title": args.get("error-dialog-title"),
                     "code": hexcode, "detail": self._excp_by_code.get(hexcode or ""),
                     "open": True, "ts": rec.get("ts")}
                r.dialogs.append(d)
                self._open_dialogs[a.get("id", str(len(r.dialogs)))] = d
            elif (d := self._open_dialogs.pop(a.get("id", ""), None)) is not None:
                d["open"] = False
                d["shown_s"] = a.get("elapsed")
        elif k == "qp-process-query-error":
            r.query_errors.append(text[:300])
        elif k == "qp-batch-summary" and isinstance(v, dict) \
                and (v.get("batch-errors") or v.get("query-errors")):
            for j in v.get("jobs") or []:
                if j.get("exception"):
                    r.query_errors.append(f"{j.get('owner-worksheet') or j.get('owner-dashboard')}: "
                                          f"{j['exception'][:240]}")
        elif k == "compute-task" and isinstance(v, dict) and "tableau-error-code" in v:
            r.query_errors.append(text[:300])
        elif k.startswith("end-ds.") and a.get("rk") not in (None, "ok"):
            cap = v.get("caption") if isinstance(v, dict) else None
            r.ds_errors.append(f"{k} rk={a.get('rk')} ds={cap!r}")

        if k.startswith("end-") and a.get("rk") == "exception":
            r.activity_exceptions.append(f"{k}: {json.dumps(a.get('rv'))[:300]}")

        if k == "excp" and isinstance(v, dict):
            code = (rec.get("e") or {}).get("excp-error-code")
            if code:
                self._excp_by_code[code.upper().replace("0X", "0x")] = (v.get("excp-msg") or "").strip()

        if sev in ("warn", "error", "fatal"):
            if NOISE.search(k + " " + text):
                r.noise += 1
                return
            if k == "excp" and BENIGN_EXCP.search(text):
                r.benign += 1
                self._last_benign = True
                return
            if k == "msg" and self._last_benign and BENIGN_FOLLOWUP.search(text):
                r.benign += 1
                return
            self._last_benign = False
            if k == "excp":
                r.excp_errors.append(text[:300])
            elif not (k == "msg" and (RE_INVALID.match(text) or RE_THUMB_FAIL.match(text))):
                r.unexpected.append(f"{sev} {k} {text[:300]}")


# ------------------------------------------------------------------ log directory tailer
_GENERIC_READ, _SHARE_READ_WRITE_DELETE, _OPEN_EXISTING, _ATTR_NORMAL = 0x80000000, 0x7, 3, 0x80


def _open_log(p: Path):
    """Open a log for binary reading. Windows: Python's open() does not pass FILE_SHARE_DELETE, so while we
    held a file Tableau could not rename log.txt to log_bk.txt when a new process takes the slot. Open it with
    CreateFileW and FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE instead, so renames work as on
    macOS and the handle follows the file (UNVERIFIED on Windows; assumes Tableau shares its log for reading)."""
    if not IS_WIN:
        return open(p, "rb")
    import ctypes
    import msvcrt
    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.restype = ctypes.c_void_p
    create.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    h = create(str(p), _GENERIC_READ, _SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, _ATTR_NORMAL, None)
    if h is None or h == ctypes.c_void_p(-1).value:                       # INVALID_HANDLE_VALUE
        raise OSError(ctypes.get_last_error(), "CreateFileW failed", str(p))
    return os.fdopen(msvcrt.open_osfhandle(h, os.O_RDONLY | getattr(os, "O_BINARY", 0)), "rb")


def snapshot(log_dir: Path = LOG_DIR) -> dict[int, int]:
    """inode -> size for every live slot (log.txt, log_<N>.txt)."""
    out = {}
    for name in os.listdir(log_dir):
        if SLOT_RE.match(name):
            try:
                st = (log_dir / name).stat()
            except FileNotFoundError:
                continue
            out[st.st_ino] = st.st_size
    return out


class LogDirTail:
    """Tails every live slot from a snapshot (inode -> size).

    A pre-existing file is read from its snapshot size. A new inode (a new process took the
    slot) is read from 0. Each file stays open, keyed by inode. When the slot is reused, the
    old file is renamed to *_bk.txt and we keep reading it through the same fd.
    """

    def __init__(self, log_dir: Path = LOG_DIR, snap: dict[int, int] | None = None):
        self.dir = Path(log_dir)
        self.snap = snapshot(self.dir) if snap is None else snap
        self.handles: dict[int, list] = {}  # ino -> [fh, name, partial-line bytes]
        self._discover()

    def _discover(self) -> None:
        for name in os.listdir(self.dir):
            if not SLOT_RE.match(name):
                continue
            p = self.dir / name
            try:
                ino = p.stat().st_ino
                if ino in self.handles:
                    continue
                fh = _open_log(p)
            except OSError:
                continue
            st = os.fstat(fh.fileno())
            if st.st_ino in self.handles:  # raced with a rename; already tracked
                fh.close()
                continue
            start = self.snap.get(st.st_ino, 0)
            fh.seek(start if start <= st.st_size else 0)
            self.handles[st.st_ino] = [fh, name, b""]

    def poll(self):
        """Yield (file name at discovery, record) for every complete new line."""
        self._discover()
        for h in self.handles.values():
            try:  # slot reused by truncation (same inode, smaller size): restart from 0
                if os.fstat(h[0].fileno()).st_size < h[0].tell():
                    h[0].seek(0); h[2] = b""
            except OSError:
                continue
            data = h[0].read()
            if not data:
                continue
            *lines, h[2] = (h[2] + data).split(b"\n")
            for line in lines:
                if line.strip():
                    try:
                        yield h[1], json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue

    def close(self) -> None:
        for h in self.handles.values():
            h[0].close()


# ------------------------------------------------------------------ macOS helpers (no TCC)
_JXA_WINDOWS = r"""
ObjC.import('CoreGraphics');
function run(argv) {
  var pid = parseInt(argv[0]), out = [];
  var arr = ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, 0));
  for (var i = 0; i < arr.count; i++) {
    var d = arr.objectAtIndex(i);
    if (ObjC.unwrap(d.objectForKey('kCGWindowOwnerPID')) != pid) continue;
    var n = d.objectForKey('kCGWindowName');
    out.push({layer: ObjC.unwrap(d.objectForKey('kCGWindowLayer')),
              bounds: ObjC.deepUnwrap(d.objectForKey('kCGWindowBounds')),
              title: n.isNil() ? null : ObjC.unwrap(n)});
  }
  return JSON.stringify(out);
}"""
_JXA_INSTANCES = r"""
ObjC.import('AppKit');
function run(argv) {
  var a = $.NSRunningApplication.runningApplicationsWithBundleIdentifier(argv[0]), out = [];
  for (var i = 0; i < a.count; i++) { var x = a.objectAtIndex(i);
    out.push({pid: x.processIdentifier, finished: x.finishedLaunching, active: x.active}); }
  return JSON.stringify(out);
}"""
_JXA_TERMINATE = r"""
ObjC.import('AppKit');
function run(argv) {
  var x = $.NSRunningApplication.runningApplicationWithProcessIdentifier(parseInt(argv[0]));
  if (x.isNil()) return 'gone';
  return x.terminate ? 'sent' : 'refused';
}"""


def _jxa(script: str, *args: str, timeout: float = 10.0):
    if IS_WIN:
        raise RuntimeError("osascript (JXA) is macOS-only")
    p = subprocess.run(["osascript", "-l", "JavaScript", "-", *args], input=script,
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    out = p.stdout.strip()
    return json.loads(out) if out[:1] in "[{" else out


def windows_of(pid: int) -> list[dict]:
    """On-screen windows of one process. Works without Screen Recording, but then every
    title is null (checked on Safari: layer and bounds are present, kCGWindowName is not).
    Windows: returns [] (no probe). Listing another process's windows needs EnumWindows through
    ctypes, and the result only feeds the informational Report.windows, which nothing decides on."""
    if IS_WIN:
        return []
    return _jxa(_JXA_WINDOWS, str(pid))


# ------------------------------------------------------------------ Windows helpers (EXPERIMENTAL)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)      # no console flash from a GUI-less server
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def parse_tasklist(out: str) -> list[tuple[str, int]]:
    """(image name, pid) rows from `tasklist /FO CSV /NH` output. Any line whose second field is not a
    number is skipped: that covers "INFO: No tasks are running which match the specified criteria." and its
    translations on non-English Windows. The csv module handles quoted fields with commas ("12,345 K")."""
    rows = []
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[1].strip().isdigit():
            rows.append((row[0].strip(), int(row[1])))
    return rows


def _tasklist(flt: str) -> list[tuple[str, int]]:
    p = subprocess.run(["tasklist", "/FI", flt, "/FO", "CSV", "/NH"], capture_output=True, text=True,
                       errors="replace", timeout=15, creationflags=_NO_WINDOW)
    return parse_tasklist(p.stdout)


def _win_image() -> str:
    return ntpath.basename(APP_PATH) if APP_PATH.lower().endswith(".exe") else "tabpublic.exe"


def _win_pid_alive(pid: int) -> bool:
    """Never os.kill(pid, 0) on Windows: there it calls TerminateProcess and kills the process."""
    return any(p == pid for _, p in _tasklist(f"PID eq {pid}"))


def _win_quit_pid(pid: int, timeout: float) -> bool:
    """`taskkill /PID <pid>` WITHOUT /F posts WM_CLOSE to the process's windows: the same polite close as
    clicking X (UNVERIFIED for Tableau: an unmodified workbook should close, a modified one should prompt).
    Then poll tasklist until the pid is gone. Nothing is force-killed."""
    subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, text=True, errors="replace",
                   timeout=15, creationflags=_NO_WINDOW)
    end = time.monotonic() + timeout
    while True:
        if not _win_pid_alive(pid):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.5)


def _win_launch(target: str, mode: str) -> None:
    """Start tabpublic.exe with the workbook as argv[1], detached from the server (no console, own process
    group, no inherited handles: stdout is the MCP stdio channel). Without a found exe, fall back to the file
    association (os.startfile, ShellExecute "open"). Assumed (UNVERIFIED): every launch is a new Tableau
    process that logs argv[1], so "open" and "open-new" behave the same. There is no background (-g) equivalent."""
    if mode not in ("open", "open-new"):
        raise ValueError(mode)
    if os.path.isfile(APP_PATH):
        subprocess.Popen([APP_PATH, target], creationflags=_DETACHED | _NEW_GROUP, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=os.path.dirname(APP_PATH))
    else:
        os.startfile(target)  # type: ignore[attr-defined]  # Windows-only


def tableau_instances() -> list[dict]:
    """Running Tableau Public processes (one per workbook). Needs no permission.
    Windows: from tasklist, with finished=True (the process exists) and active=None (not known)."""
    if IS_WIN:
        return [{"pid": pid, "finished": True, "active": None}
                for _, pid in _tasklist(f"IMAGENAME eq {_win_image()}")]
    return _jxa(_JXA_INSTANCES, BUNDLE_ID)


def quit_pid(pid: int, timeout: float = 20.0) -> bool:
    """Ask ONE Tableau process to quit (NSRunningApplication.terminate, through JXA, so no
    pyobjc is needed). UNVERIFIED for Tableau. Logs show that an unmodified workbook closes
    with no prompt: handle-close takes 0.02-0.06 s, then the process exits about 3 s later.
    A modified workbook brings up the Tableau Public sign-in/save flow (log.txt 18:51:11),
    so never interact with the window before quitting. Nothing is force-killed.
    Windows: taskkill without /F, see _win_quit_pid (os.kill must never run there)."""
    if IS_WIN:
        return _win_quit_pid(pid, timeout)
    if _jxa(_JXA_TERMINATE, str(pid)) == "gone":
        return True
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.25)
    return False


def launch(target: str, mode: str = "open", background: bool = True) -> None:
    if IS_WIN:
        return _win_launch(target, mode)
    app = ["-a", APP_PATH] if os.path.isdir(APP_PATH) else ["-b", BUNDLE_ID]
    bg = ["-g"] if background else []
    if mode == "open":        # observed: the running instance spawns a child with argv[1]
        cmd = ["open", *bg, *app, target]
    elif mode == "open-new":  # UNVERIFIED: a dedicated instance launched with argv[1]
        cmd = ["open", "-n", *bg, *app, "--args", target]
    else:
        raise ValueError(mode)
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def is_target(argv1: str, path: Path, unique: bool) -> bool:
    """Did the process with this argv[1] open `path`? Windows (UNVERIFIED): Tableau may log argv[1] in another
    form (escaped, \\\\?\\ prefix), so when `path` is our uniquely named copy, a name match at the end is enough."""
    if same_file(argv1, str(path)):
        return True
    return IS_WIN and unique and argv1.lower().endswith(path.name.lower())


def unique_copy(path: Path) -> Path:
    """Copy the file, in the same directory so relative data paths still resolve, under a
    stem nothing else has open. ctx.wb (= the stem) then identifies our load without doubt."""
    q = path.with_name(f"{path.stem}__tv{uuid.uuid4().hex[:8]}{path.suffix}")
    shutil.copy2(path, q)
    return q


# ------------------------------------------------------------------ validate
def validate(target: str | Path, *, copy: bool = True, quit_after: bool = True,
             spawn_timeout: float = 45.0, load_timeout: float = 120.0,
             stall_quiet: float = 20.0, settle_quiet: float = 1.5, settle_max: float = 10.0,
             dialog_grace: float = 0.5, mode: str = "open", log_dir: Path = LOG_DIR) -> Report:
    """Open `target` in Tableau Public and classify the load from the logs (load-tested on Tableau Public 2026.2.3)."""
    src = Path(target).resolve()
    exp = read_expectations(src)
    path = unique_copy(src) if copy else src
    exp.stem = path.stem
    tail = LogDirTail(log_dir)                    # 1. record inode+size of every live slot
    t0 = time.monotonic()
    launch(str(path), mode)                       # 2. open -a (Windows: tabpublic.exe <file>)
    states: dict[int, Classifier] = {}
    tpid: int | None = None
    last = dialog_at = loaded_at = None
    try:
        while True:
            got = False
            for name, rec in tail.poll():         # 3. tail every slot, route by pid
                got = True
                pid = rec.get("pid")
                cls = states.get(pid)
                if cls is None:
                    cls = states[pid] = Classifier(Report(target=str(src), stem=exp.stem,
                                                          log_path=str(log_dir / name)))
                cls.feed(rec)
                if tpid is None:
                    r = cls.r
                    if r.argv1 and is_target(r.argv1, path, copy):
                        tpid, r.via = pid, "child"
                    elif rec.get("k") in ("load-workbook", "end-workspace.open-workbook") \
                            and (rec.get("ctx") or {}).get("wb") == exp.stem \
                            and any(same_file(p, str(path)) for p in r.fileopen_paths):
                        tpid, r.via = pid, "same-process"  # UNVERIFIED (cold start)
                    if tpid is not None:
                        r.timings["spawn_s"] = round(time.monotonic() - t0, 3)
                        r.copy_path = str(path) if copy else None
            now = time.monotonic()
            if got:
                last = now
            if tpid is None:
                if now - t0 > spawn_timeout:
                    rep = Report(target=str(src), stem=exp.stem,
                                 load_failed=f"no Tableau process picked up the file in {spawn_timeout}s")
                    return rep
                time.sleep(0.2)
                continue
            r = states[tpid].r
            r.active_sheet, r.expected_sheets = exp.active_sheet, sorted(exp.expected_sheets)
            if r.load_failed or r.closed:
                break
            if any(d["open"] for d in r.dialogs):
                dialog_at = dialog_at or now
                if now - dialog_at >= dialog_grace:
                    break                         # modal: nothing progresses until dismissed
            if r.loaded:
                if loaded_at is None:
                    loaded_at = now
                    r.timings["load_s"] = round(now - t0, 3)
                # Good loads compute every dashboard sheet before load-finished. A sheet still
                # missing once the active sheet has painted and the log is quiet will not come.
                sheets_ok = exp.expected_sheets <= set(r.computed_sheets)
                painted = exp.active_sheet is None or exp.active_sheet in r.rendered
                quiet = settle_quiet if sheets_ok else 2 * settle_quiet
                if (painted and now - last >= quiet) or now - loaded_at >= settle_max:
                    break
            elif now - t0 > load_timeout:
                r.load_failed = f"timeout after {load_timeout}s without end-workspace.open-workbook"
                break
            elif now - last >= stall_quiet:
                r.load_failed = f"stalled: no log output for {stall_quiet}s before load finished"
                break
            time.sleep(0.2)
        try:
            r.windows = windows_of(tpid)          # >1 layer-0 window may be a modal (UNVERIFIED)
        except Exception as e:  # noqa: BLE001
            r.windows = [{"error": str(e)[:200]}]
        if quit_after and not r.closed and not any(d["open"] for d in r.dialogs):
            if quit_pid(tpid):
                end = time.monotonic() + 5
                while not r.closed and time.monotonic() < end:
                    for _, rec in tail.poll():
                        if rec.get("pid") == tpid:
                            states[tpid].feed(rec)
                    time.sleep(0.2)
        return r
    finally:
        tail.close()
        if copy and path != src:
            try:
                if tpid is None or states[tpid].r.closed:
                    path.unlink()          # else left in place: the process may still hold it
            except OSError:
                pass


# ------------------------------------------------------------------ offline analysis
def replay(path: str | Path, stem: str | None = None, expect: Expect | None = None) -> Report:
    """Classify one complete process log (read-only)."""
    rep = Report(log_path=str(path), via="replay", stem=stem or (expect.stem if expect else None))
    if expect:
        rep.active_sheet, rep.expected_sheets = expect.active_sheet, sorted(expect.expected_sheets)
    cls = Classifier(rep)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                cls.feed(json.loads(line))
            except ValueError:
                continue
    if rep.stem is None and rep.argv1:
        rep.stem = Path(rep.argv1).stem
    return rep


def scan(match: str, log_dir: Path = LOG_DIR, expect: Expect | None = None) -> list[Report]:
    """Find every process log (live and *_bk) whose argv[1] contains `match`. The result answers
    "what happened the last times this file was opened" without launching anything."""
    out = []
    for p in sorted(Path(log_dir).iterdir()):
        if not ANY_LOG_RE.match(p.name) or p.stat().st_size == 0:
            continue
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            head = "".join(fh.readline() for _ in range(40))
        if "argv[1]=" in head and match in head:
            out.append(replay(p, expect=expect))
    return sorted(out, key=lambda r: r.log_path or "")


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "validate":  # launches Tableau
        print(json.dumps(validate(sys.argv[2]).summary(), indent=2, default=list))
        sys.exit(0)
    exp = None
    args = sys.argv[1:]
    if args and args[0] == "--expect":
        exp, args = read_expectations(args[1]), args[2:]
    reps = scan(args[1], expect=exp) if args and args[0] == "scan" else [replay(p, expect=exp) for p in args]
    for rep in reps:
        print(f"{Path(rep.log_path).name:13s} pid={rep.pid} status={rep.status:19s} "
              f"v={rep.workbook_version} load={rep.load_elapsed}s render0={(rep.rendered or [None])[0]!r} "
              f"invalid={[c['caption'] for c in rep.invalid_calcs]} missing={rep.missing_sheets} "
              f"dialogs={[d['message'] for d in rep.dialogs]} thumbfail={rep.thumbnail_failures} "
              f"qerr={len(rep.query_errors)} excp={len(rep.excp_errors)} unexpected={len(rep.unexpected)} "
              f"benign={rep.benign} noise={rep.noise}")
        for u in (rep.unexpected + rep.excp_errors)[:3]:
            print("     !", u[:180])
