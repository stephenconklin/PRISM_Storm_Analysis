"""
Run-time support shared by the PRISM Storm Analysis scripts (standard library only).

- Exit codes, and die() to stop with a message and one of them.
- run_main(main): every script's entry point. A run that fails, is interrupted (Ctrl-C) or is cancelled
  still gets a run_manifest.json entry with its status; network errors get a clear message and exit 4;
  Ctrl-C says what was kept and how to continue.
- attach(out, run, args): takes the folder's lock (<out>/run/.lock), starts the run's log file in
  <out>/run/logs/, and registers the manifest entry for run_main to complete. A folder still in the old
  layout (layout.py) stops with the command that migrates it.
- write_atomic(), atomic_open(), atomic_path(): write to <name>.part and rename it into place, so an
  interrupted run never leaves a half-written file under the real name.
- confirm(), interactive(), countdown(), eta(): command-line interaction. Prompts appear only when stdin
  and stdout are a terminal; otherwise (pipelines, cron, tests) the answer is yes.

Exit codes:
    0 ok · 1 QC failed (outputs written) · 2 bad arguments or missing inputs · 3 incomplete (rate-limited
    or partly done: rerun to continue) · 4 network failure · 5 other error · 130 interrupted or cancelled
"""
import contextlib
import datetime as dt
import json
import logging
import os
import shlex
import socket
import sys
import time
import traceback
from pathlib import Path

import layout
from layout import span  # noqa: F401  (re-exported: '<YYYYMMDD>_<YYYYMMDD>' for a window)

EXIT_OK, EXIT_QC, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_NETWORK, EXIT_ERROR, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 5, 130
STATUS = {EXIT_OK: "ok", EXIT_QC: "qc_fail", EXIT_USAGE: "input_error", EXIT_INCOMPLETE: "incomplete",
          EXIT_NETWORK: "network_error", EXIT_ERROR: "error", EXIT_INTERRUPTED: "interrupted"}
LOCK_ENV = "PRISM_STORM_LOCK"   # set by pipeline.py: the lock it holds, which its steps share
LOG_ENV = "PRISM_STORM_LOG"     # set by pipeline.py: the log file a step writes to
PIPELINE_ENV = "PRISM_STORM_PIPELINE"  # set by pipeline.py: its own command, for the resume hint
log = logging.getLogger("prism_storm")

_state = {"out": None, "run": None, "lock": None, "log": None, "streams": None, "handlers": []}


class Stop(SystemExit):
    """SystemExit with an exit code and the message already printed."""

    def __init__(self, message, code):
        super().__init__(code)
        self.message = message


def die(message, code=EXIT_USAGE):
    """Print message to stderr and stop with code."""
    print(message, file=sys.stderr, flush=True)
    raise Stop(message, code)


def status_name(code):
    return STATUS.get(code, "error")


# ---------- atomic writes ----------

def part_path(path):
    path = Path(path)
    return path.with_name(path.name + ".part")


@contextlib.contextmanager
def atomic_path(path):
    """Yield a temporary path next to path; on success it replaces path, on any error it is removed.
    For writers that take a path (rasterio, xarray); give them an explicit driver or format."""
    tmp = part_path(path)
    tmp.unlink(missing_ok=True)
    try:
        yield tmp
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def atomic_open(path, mode="w", **kwargs):
    """open() that writes to <path>.part and renames it over path when the block finishes."""
    with atomic_path(path) as tmp:
        with open(tmp, mode, **kwargs) as f:
            yield f


def write_atomic(path, data, encoding="utf-8"):
    """Write text or bytes to path atomically."""
    with atomic_path(path) as tmp:
        if isinstance(data, bytes):
            Path(tmp).write_bytes(data)
        else:
            Path(tmp).write_text(data, encoding=encoding)


def write_json(path, obj):
    write_atomic(path, json.dumps(obj, indent=2, default=str))


def windowed(path, start, end):
    """<stem>_<YYYYMMDD>_<YYYYMMDD><suffix>: a per-window output's name."""
    path = Path(path)
    return path.with_name(f"{path.stem}_{span(start, end)}{path.suffix}")


def window_json(path, start, end):
    """A summary JSON for the window start..end: path, or its window-named form, when its "window" matches;
    None otherwise."""
    for p in (Path(path), windowed(path, start, end)):
        if p.exists():
            try:
                js = json.loads(p.read_text())
            except ValueError:
                continue
            w = js.get("window") or {}
            if str(w.get("start")) == str(start) and str(w.get("end")) == str(end):
                return js
    return None


# ---------- folder lock ----------

def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_path(out):
    return layout.Layout(out, settings={}).lock_file


def lock_holder(out, path=None):
    """The lock file's contents ({pid, host, command, started}) if another live run holds out, else None."""
    path = Path(path) if path else lock_path(out)
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if info.get("host") != socket.gethostname():
        return info  # can't check a process on another machine: assume it is running
    return info if _alive(int(info.get("pid", 0))) else None


class FolderLock:
    """<out>/run/.lock: one run writes to a folder at a time. A lock left by a run that is gone is taken over.
    A step started by pipeline.py shares the pipeline's lock (LOCK_ENV)."""

    def __init__(self, out):
        self.out = Path(out)
        self.path = lock_path(out)
        self.owned = False

    def acquire(self):
        held = os.environ.get(LOCK_ENV)
        if held and Path(held).resolve() == self.path.resolve() and lock_holder(self.out):
            return self  # inside a pipeline that holds this folder
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = lock_holder(self.out)
                if info:
                    die(f"{self.out} is in use by another run (pid {info.get('pid')} on {info.get('host')}, "
                        f"started {info.get('started')}): {info.get('command')}\nWait for it to finish; if that run "
                        f"is gone, delete {self.path}.")
                self.path.unlink(missing_ok=True)  # left behind by a run that stopped without cleaning up
                continue
            with os.fdopen(fd, "w") as f:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(), "command": shlex.join(sys.argv),
                           "started": dt.datetime.now().astimezone().isoformat(timespec="seconds")}, f)
            self.owned = True
            return self
        die(f"Could not take the lock {self.path}.")

    def release(self):
        if self.owned:
            self.path.unlink(missing_ok=True)
            self.owned = False


# ---------- logging ----------

class Tee:
    """A text stream that writes to the terminal (unless quiet) and to the log file, with a time stamp at the
    start of each line in the file."""

    def __init__(self, stream, file, echo=True):
        self.stream, self.file, self.echo = stream, file, echo
        self._line_start = True

    def write(self, s):
        if self.echo:
            self.stream.write(s)
        for piece in s.splitlines(keepends=True):
            if self._line_start:
                self.file.write(dt.datetime.now().strftime("%H:%M:%S "))
            self.file.write(piece)
            self._line_start = piece.endswith("\n")
        return len(s)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def isatty(self):
        return self.stream.isatty()

    def fileno(self):
        return self.stream.fileno()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def start_log(out, script, verbose=False, quiet=False, path=None):
    """Copy this run's output to <out>/run/logs/<time>-<script>.log (or the file pipeline.py chose) and send
    the 'prism_storm' logger's records there (DEBUG and up) and to the terminal (INFO and up; DEBUG with
    verbose; WARNING and up with quiet). Returns the log path."""
    if _state["log"]:
        return _state["log"]
    path = Path(path or os.environ.get(LOG_ENV) or layout.Layout(out, settings={}).logs_dir /
                f"{dt.datetime.now():%Y%m%d-%H%M%S}-{Path(script).stem}.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a", encoding="utf-8", buffering=1)
    f.write(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {shlex.join(sys.argv)}\n")
    _state["streams"] = (sys.stdout, sys.stderr)
    sys.stdout = Tee(sys.stdout, f, echo=not quiet)
    sys.stderr = Tee(sys.stderr, f)
    fh = logging.StreamHandler(f)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(_state["streams"][1])
    ch.setFormatter(logging.Formatter("%(message)s"))
    ch.setLevel(logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for h in (fh, ch):
        log.addHandler(h)
    _state["handlers"] = [fh, ch]
    _state["log"] = path
    _state["log_file"] = f
    return path


def stop_log():
    if _state["streams"]:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout, sys.stderr = _state["streams"]
        _state["streams"] = None
    for h in _state["handlers"]:
        log.removeHandler(h)
    _state["handlers"] = []
    if _state.get("log_file"):
        _state["log_file"].close()
        _state["log_file"] = None
    _state["log"] = None


# ---------- attach / run_main ----------

def add_common_args(ap):
    """--yes, --verbose and --quiet, shared by every script."""
    ap.add_argument("-y", "--yes", action="store_true",
                    help="don't ask before re-downloading or other actions that use a download allowance")
    ap.add_argument("-v", "--verbose", action="store_true", help="also show debug detail (HTTP requests) here")
    ap.add_argument("-q", "--quiet", action="store_true", help="show only warnings and errors here (the log has all)")


def attach(out, run=None, args=None, lock=True, log_path=None):
    """Lock the output folder, start the run's log, and register its manifest entry. Does nothing for a
    folder that doesn't exist yet (the script will stop on its missing inputs). Stops on a folder in the
    old layout."""
    out = Path(out)
    if not out.is_dir():
        return None
    if layout.legacy(out):
        die(layout.migrate_hint(out))
    _state["out"] = out
    if lock and not _state["lock"]:
        _state["lock"] = FolderLock(out).acquire()
    path = start_log(out, sys.argv[0], getattr(args, "verbose", False), getattr(args, "quiet", False), log_path)
    if run is not None:
        _state["run"] = run
        try:
            run["log"] = str(path.resolve().relative_to(out.resolve()))
        except ValueError:
            run["log"] = str(path)
        settings = out / "storm.toml"
        if settings.exists():
            import hashlib
            run["settings_file"] = {"path": settings.name,
                                    "sha256": hashlib.sha256(settings.read_bytes()).hexdigest()}
    return path


def resume_hint():
    command = os.environ.get(PIPELINE_ENV) or shlex.join(["python"] + sys.argv)
    return ("Finished work is kept (downloaded zips, completed gauges, outputs already written). "
            f"To continue, rerun:\n  {command}")


def is_network_error(e):
    mods = {c.__module__.split(".")[0] for c in type(e).__mro__}
    return bool(mods & {"requests", "urllib3"}) or isinstance(e, (ConnectionError, TimeoutError, socket.gaierror))


def _finish(code, message):
    run, out = _state["run"], _state["out"]
    if run is not None and not run.get("_appended") and out is not None and out.is_dir():
        run["status"] = status_name(code)
        if message:
            run["error"] = message[:2000]
        try:
            import manifest
            manifest.append_run(out, run)
        except Exception as e:  # never hide the run's own error behind a manifest problem
            print(f"(could not record the run in run_manifest.json: {e})", file=sys.stderr)
    if _state["lock"]:
        _state["lock"].release()
        _state["lock"] = None
    stop_log()
    _state["run"] = _state["out"] = None


def run_main(main):
    """Run a script's main() and exit with one of the codes above."""
    code, message = EXIT_OK, None
    try:
        main()
    except Stop as e:
        code, message = e.code, e.message
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            code = e.code or 0
        else:
            message = str(e.code)
            print(message, file=sys.stderr)
            code = EXIT_ERROR
    except KeyboardInterrupt:
        code, message = EXIT_INTERRUPTED, "interrupted (Ctrl-C)"
        print(f"\nInterrupted. {resume_hint()}", file=sys.stderr)
    except Exception as e:
        if is_network_error(e):
            code, message = EXIT_NETWORK, f"network error: {e.__class__.__name__}: {e}"
            print(f"{message}\nCheck the connection or the service (python scripts/check_endpoints.py). "
                  f"{resume_hint()}", file=sys.stderr)
        else:
            code, message = EXIT_ERROR, f"{e.__class__.__name__}: {e}"
            traceback.print_exc()
    finally:
        _finish(code, message)
    sys.exit(code)


# ---------- interaction and progress ----------

def interactive():
    """True when a person is at the terminal (stdin and stdout are terminals)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def confirm(question, assume_yes=False, default=False):
    """Ask a yes/no question when someone is at the terminal; otherwise (or with assume_yes) answer yes."""
    if assume_yes or not interactive():
        return True
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input(f"{question} [{hint}] ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes", "n", "no"):
            return ans.startswith("y")


def confirm_or_stop(question, assume_yes=False):
    if not confirm(question, assume_yes):
        die("Cancelled; nothing was changed.", EXIT_INTERRUPTED)


def countdown(seconds, label):
    """Wait seconds, showing a live countdown on a terminal (one line otherwise)."""
    console = _state["streams"][1] if _state["streams"] else sys.stderr
    log.info(f"{label}: waiting {int(seconds) // 60} min {int(seconds) % 60} s")
    if not (hasattr(console, "isatty") and console.isatty()):
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        console.write(f"\r    {label}: {int(left) // 60}:{int(left) % 60:02d} left ")
        console.flush()
        time.sleep(min(1.0, left))
    console.write("\r" + " " * (len(label) + 24) + "\r")
    console.flush()


def eta(started, done, total):
    """' (about 3 min left)' from the average time per item so far; '' until there is something to go on."""
    if done < 1 or done >= total:
        return ""
    left = (time.monotonic() - started) / done * (total - done)
    if left < 60:
        return f" (about {left:.0f} s left)"
    return f" (about {left / 60:.0f} min left)"
