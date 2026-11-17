"""
Umbra Gore & Effects' body models: a prebaked bake when it fits, a bake on this
machine when it does not.

Umbra Gore & Effects cuts every character model of the player's mod list into
parts (its "bake", made by Umbra Effects Setup, tools/zgo_setup in its folder).
Umbra publishes the bake it made for the pack's own mod list (packdef: "Prebaked
bakes"). Setup asks the installed Umbra Effects Setup:

    --check        is there a bake, and is it up to date?   (RESULT body=.. props=.. kid=.. pack=..)
                   body=unused (1.8.3): the bake is right, but the game doesn't load it
    --match DIR    would the prebaked bake whose report is in DIR fit?   (RESULT match=yes|no)
    --adopt-dates  once Setup has put the prebaked bake in place: record this machine's dates of
                   the game's model archives and of the mods' models in its report, as Umbra
                   Effects Setup does after its own download (1.8.3; RESULT adopted=n). Without
                   them it finds the bake out of date and gets it all again.
    --bake --no-download   bake for this mod list here (minutes; the fallback). Setup has
                   decided about the prebaked bake already: Umbra Effects Setup must not download
                   it again
    --pack         pack a bake's loose small parts into archives (bakes made before 1.8.2)
    --props        cut the breakable props here (seconds)

Umbra Effects Setup does the work of --bake, --pack and --props in a worker process of its
own. stop() stops both, as Umbra Effects Setup's own Ctrl+C and Cancel do.

The prebaked files go into the mod's folder, next to the layer's own files. They
are not part of the layer: install_mods_layer() carries them over when the layer
is updated (carry_generated), and Umbra Effects Setup says whether they still fit.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

# what Umbra Effects Setup makes in its folder, each listed (with its files) in a report there
REPORTS = ("zgo_bake_report.json", "zgo_fracture_report.json")
BODY_REPORT = REPORTS[0]
PROGRAM = "Umbra Effects Setup.exe" if os.name == "nt" else "zgo-setup-linux"
# --check's body= states where the bake's files are right ("unused": but the game doesn't load them)
FITS = ("ok", "order", "unused")
WINDOWS = os.name == "nt"
STOP_WAIT = 20          # seconds Umbra Effects Setup gets to stop by itself when asked (stop())
KILL_WAIT = 5           # then after terminate(), before kill()


class BakeError(Exception):
    pass


def program(zgo_dir):
    """The installed Umbra Effects Setup, or None."""
    p = os.path.join(zgo_dir, "tools", "zgo_setup", PROGRAM)
    return p if os.path.isfile(p) else None


def _rel_ok(rel):
    return isinstance(rel, str) and rel and not rel.startswith("/") and "\\" not in rel and ":" not in rel \
        and ".." not in rel.split("/")


def generated(folder):
    """{rel} of what Umbra Effects Setup made in folder: the reports and the files they list."""
    out = set()
    for rep in REPORTS:
        try:
            with open(os.path.join(folder, rep), encoding="utf-8") as f:
                files = json.load(f).get("files") or []
        except (OSError, ValueError, AttributeError):
            continue
        out.add(rep)
        out.update(r for r in files if _rel_ok(r))
    return out


def carry_generated(old_dir, new_dir, shipped=()):
    """A layer update puts a new folder in place of the old one: move the bake (made on
    this machine or prebaked) from the old folder into the new one, unless the new
    layer ships a file of that name itself. Returns how many files moved."""
    shipped = set(shipped)
    moved = 0
    for rel in sorted(generated(old_dir)):
        if rel in shipped:
            continue
        src = os.path.join(old_dir, *rel.split("/"))
        dst = os.path.join(new_dir, *rel.split("/"))
        if os.path.isfile(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            moved += 1
    return moved


def parse_result(lines):
    """The last 'RESULT k=v ...' line of Umbra Effects Setup's output -> {k: v} ({} without one)."""
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("RESULT "):
            return dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
    return {}


def run(prog, args, work, env=None, on_line=None, cancel=None, timeout=None):
    """Run Umbra Effects Setup without its window; returns (exit code, output lines).
    Its output goes to a log file (the Windows program has no console), read as it
    grows: on_line(line) for each new line. What it wrote to stderr (argparse's
    "unrecognized arguments", a crash) comes last. cancel() True, or the timeout,
    stops it (stop()) and raises KeyboardInterrupt, or BakeError."""
    os.makedirs(work, exist_ok=True)
    for n in range(20):                          # a fresh file: --log appends, and an old RESULT line must
        log = os.path.join(work, "umbra-effects-setup%s.log" % ("-%d" % n if n else ""))   # not count
        try:
            os.remove(log)
        except FileNotFoundError:
            break
        except OSError:                          # (Windows: still open in a program that hung)
            continue
        if not os.path.exists(log):
            break
    else:
        raise BakeError("could not start a log file in %s" % work)
    cmd = [prog] + list(args) + ["--log", log]
    kw = {}
    if WINDOWS:
        # no console window; a process group of its own, so that Ctrl+Break can reach it alone (stop())
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    mod = args[args.index("--mod") + 1] if "--mod" in args[:-1] else None
    err_path = log[:-len(".log")] + ".err"
    try:
        err = open(err_path, "wb")
    except OSError:
        err = None
    since = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=os.path.dirname(prog), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=err or subprocess.DEVNULL, **kw)
    except OSError as e:                         # damaged, or not runnable here
        if err:
            err.close()
        return None, ["Umbra Effects Setup could not start: %s" % e]
    lines, pos, t0 = [], 0, time.monotonic()

    def add(line):
        lines.append(line)
        if on_line:
            on_line(line)

    def read_new():
        nonlocal pos
        try:
            with open(log, "rb") as f:
                f.seek(pos)
                data = f.read()
        except OSError:
            return
        cut = data.rfind(b"\n")
        if cut < 0:
            return
        pos += cut + 1
        for raw in data[:cut].split(b"\n"):
            add(raw.decode("utf-8", "replace").rstrip("\r"))
    stopped = False
    try:
        while p.poll() is None:
            if cancel and cancel():
                stopped = True
                stop(p, mod, since, env)
                raise KeyboardInterrupt
            if timeout and time.monotonic() - t0 > timeout:
                stopped = True
                stop(p, mod, since, env)
                raise BakeError("Umbra Effects Setup did not finish in %d minutes" % (timeout // 60))
            read_new()
            time.sleep(0.5)
    except KeyboardInterrupt:
        if not stopped:         # Ctrl+C in Umbra Setup's terminal, which sent Umbra Effects Setup its own
            # (on POSIX; the cancel file does it on Windows)
            stopped = True
            stop(p, mod, since, env, interrupt=False)
        raise
    finally:
        if not stopped and p.poll() is None:
            stop(p, mod, since, env)
        if err:
            err.close()
    read_new()
    try:                                        # a last line without its newline
        with open(log, "rb") as f:
            f.seek(pos)
            rest = f.read().decode("utf-8", "replace").strip()
        if rest:
            add(rest)
    except OSError:
        pass
    if err:
        try:
            with open(err_path, "rb") as f:
                for line in f.read().decode("utf-8", "replace").splitlines():
                    if line.strip():
                        add(line.rstrip())
        except OSError:
            pass
    return p.returncode, lines


def unknown_option(code, lines):
    """Did Umbra Effects Setup refuse its command line (one from before an option it was given)?
    argparse exits 2 before the log starts; its message is on stderr, which the Windows program
    (no console) does not have."""
    return code == 2 and (not any(x.strip() for x in lines) or any("unrecognized arguments" in x for x in lines))


def stop(p, mod=None, since=0.0, env=None, interrupt=True):
    """Stop Umbra Effects Setup (Popen p), which runs a bake, a download or packing in a worker
    process of its own: killing the command line leaves the worker going for minutes.
      1. interrupt: Ctrl+C, which its command line answers by cancelling its worker
         (SIGINT; on Windows Ctrl+Break, see _interrupt);
      2. what that cancelling does, done here too: the cancel file of the worker's job
         (cancel_jobs). On Windows this is what stops it;
      3. after STOP_WAIT seconds, terminate() and kill() the command line, as before."""
    if interrupt and p.poll() is None:
        _interrupt(p)
    if mod:
        cancel_jobs(mod, since, env)
    try:
        p.wait(STOP_WAIT)
        return
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:                   # asked again: no more waiting
        p.kill()
        raise
    p.terminate()
    try:
        p.wait(KILL_WAIT)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.wait(KILL_WAIT)
        except subprocess.TimeoutExpired:
            pass


def _interrupt(p):
    """Ctrl+C for Umbra Effects Setup: SIGINT, which its command line turns into a KeyboardInterrupt
    and cancels its worker. Windows has no SIGINT for one program: Ctrl+C can't be sent to a
    process group, and Ctrl+Break (its own process group, see run()) only to a program on this
    program's console; for any other group the console sends it to every program on it, Umbra
    Setup too. Umbra Effects Setup.exe is a window program without a console (and CREATE_NO_WINDOW
    gives a console program a console of its own), and Python ends on Ctrl+Break rather than
    cancelling: so on Windows this is only sent when it can arrive, and cancel_jobs() does the
    stopping."""
    try:
        if not WINDOWS:
            p.send_signal(signal.SIGINT)
        elif p.pid in _console_pids():
            p.send_signal(getattr(signal, "CTRL_BREAK_EVENT", 1))
    except (OSError, ValueError, AttributeError):
        pass


def _console_pids():
    """The programs on this program's console (Windows; none without one)."""
    try:
        import ctypes
        n = 64
        for _ in range(3):
            buf = (ctypes.c_uint32 * n)()
            got = ctypes.windll.kernel32.GetConsoleProcessList(buf, n)
            if got <= n:
                return set(buf[:got])
            n = got + 16                        # (too small: it says how many, and fills nothing)
    except (AttributeError, OSError, ImportError):
        pass
    return set()


def cancel_jobs(mod, since=0.0, env=None):
    """Ask the workers Umbra Effects Setup started for the mod folder mod (since then) to stop,
    as its Cancel and Ctrl+C do (setup_core.Job.cancel): a file "cancel" in the job's folder,
    zgo_setup_* in its temp folder, next to the job.json that names the folder. Returns how many."""
    want = os.path.normcase(os.path.abspath(mod))
    env = os.environ if env is None else env
    bases = []
    for b in [env.get(k) for k in ("TMPDIR", "TEMP", "TMP")] + [tempfile.gettempdir()]:
        if b and b not in bases:
            bases.append(b)
    n = 0
    for base in bases:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if not name.startswith("zgo_setup_"):
                continue
            d = os.path.join(base, name)
            jp = os.path.join(d, "job.json")
            try:
                if os.path.getmtime(jp) < since - 5:
                    continue
                with open(jp, encoding="utf-8") as f:
                    job = json.load(f)
                if os.path.normcase(os.path.abspath(job["paths"]["mod"])) != want:
                    continue
                open(os.path.join(d, os.path.basename(job.get("cancel") or "cancel")), "w").close()
                n += 1
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    return n


# Umbra Effects Setup's progress lines, as its command line prints them: "  [task] <stage> done/total
# message", the stage in its words (setup_core.STAGE_TEXT), each stage counting from 0 on its own; the
# download says its bytes: "  [download] Downloading the ready-made bake: 1.2 GB of 4.7 GB, ...".
# Each stage's part of its task, weighed as Umbra Effects Setup's own window does (STAGE_WEIGHT,
# DOWNLOAD_SHARE).
PACK_TEXT = "Packing the small parts into archives"
INSTALL_TEXT = "Installing into the Umbra Effects folder"


def _shares(weights):
    out, at, total = {}, 0, float(sum(w for _, w in weights))
    for text, w in weights:
        out[text] = (at / total, (at + w) / total)
        at += w
    return out


STAGES = {
    "models": _shares([("Getting ready", 1), ("Reading the game's own models", 6), ("Reading your mod list", 3),
                       ("Baking models", 70), ("Writing the index", 4), (PACK_TEXT, 10), (INSTALL_TEXT, 6),
                       ("Done", 0)]),
    "download": {"Looking for a ready-made bake": (0.0, 0.05), "Downloading the ready-made bake": (0.05, 0.8),
                 "Unpacking the ready-made bake": (0.8, 0.88), INSTALL_TEXT: (0.88, 0.9), PACK_TEXT: (0.9, 1.0)},
    "pack": {PACK_TEXT: (0.0, 1.0)},            # --pack, or a --bake that only packs an old bake
    "props": {"Getting ready": (0.0, 0.05), "Reading your mod list": (0.05, 0.2), "Cutting props": (0.2, 0.9),
              INSTALL_TEXT: (0.9, 1.0), "Done": (1.0, 1.0)},
    "particles": {"Kid mode for particles": (0.0, 1.0)},
}
# a --bake's tasks in the whole run (setup_core.overall_fraction): the body models (baked here, the
# ready-made bake downloaded, or an old bake packed), then the props, then kid mode for particles
BAKE_TASKS = {"models": (0.0, 0.92), "download": (0.0, 0.92), "pack": (0.0, 0.92), "props": (0.92, 0.98),
              "particles": (0.98, 1.0)}
_COUNT = re.compile(r"(?:^|\s)(\d+)/(\d+)(?=\s|$)")
_SIZES = re.compile(r"([\d.]+) (bytes|KB|MB|GB|TB) of ([\d.]+) (bytes|KB|MB|GB|TB)")
_UNIT = {"bytes": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def progress_of(line, job="bake"):
    """How far a run of Umbra Effects Setup is (0..1), from one of its progress lines
    ('  [models] Baking models 120/4790 actors\\x'), or None. job "bake": a --bake (body models,
    props, kid mode for particles); anything else (--pack, --props): the line's task is the run."""
    s = line.strip()
    if not s.startswith("[") or "] " not in s:
        return None
    task, rest = s[1:].split("] ", 1)
    lo, hi = 0.0, 1.0                           # (a stage it doesn't know: the task as a whole)
    for text, (a, b) in STAGES.get(task, {}).items():
        if rest.startswith(text) and rest[len(text):len(text) + 1] in ("", " ", ":"):
            lo, hi, rest = a, b, rest[len(text):]
            if text == PACK_TEXT:               # (bake.pack: the archives, then deleting the loose copies)
                lo, hi = (lo + (hi - lo) * 0.95, hi) if "deleting the loose copies" in rest else \
                    (lo, lo + (hi - lo) * 0.95)
            break
    m = _COUNT.search(rest)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
    else:
        m = _SIZES.search(rest)
        if not m:
            return None
        done, total = float(m.group(1)) * _UNIT[m.group(2)], float(m.group(3)) * _UNIT[m.group(4)]
    if total <= 0:
        return None
    f = lo + (hi - lo) * min(1.0, done / total)
    if job == "bake":
        a, b = BAKE_TASKS.get(task, (0.0, 1.0))
        f = a + (b - a) * f
    return f


class Bar:
    """A step's progress bar over several parts (runs of Umbra Effects Setup, a download, an
    unpacking) that never goes back. part(lo, hi) -> show(f, msg): the part fills lo..hi, or
    what is left of that once the bar is past lo (a part that came after one cut short, or a
    stage of Umbra Effects Setup counted again). Thread-safe (the download reports from its
    worker threads)."""

    def __init__(self, progress, step):
        self.progress, self.step, self.at = progress, step, 0.0
        self._lock = threading.Lock()

    def __call__(self, frac, msg=""):
        with self._lock:
            self.at = max(self.at, min(1.0, frac))
            self.progress(self.step, self.at, msg)

    def part(self, lo, hi):
        lo = max(lo, self.at)
        hi = max(hi, lo)
        return lambda f, msg="": self(lo + (hi - lo) * max(0.0, min(1.0, f)), msg)


def remove_old(zgo_dir, keep):
    """Delete what the last bake made (its reports first, so a half-done swap never leaves
    a report naming files that are gone) except the names in keep."""
    old = generated(zgo_dir)
    for rep in REPORTS:
        if rep in old:
            try:
                os.remove(os.path.join(zgo_dir, rep))
            except OSError:
                pass
    for rel in sorted(old - set(REPORTS) - set(keep)):
        try:
            os.remove(os.path.join(zgo_dir, *rel.split("/")))
        except OSError:
            pass


def move_in(src_dir, zgo_dir, names):
    """Move unpacked files into the mod's folder, the reports last (so the bake only counts
    once all its files are there)."""
    reports = [n for n in names if n in REPORTS]
    for rel in [n for n in names if n not in REPORTS] + reports:
        src = os.path.join(src_dir, *rel.split("/"))
        dst = os.path.join(zgo_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)


def disk_free(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None

