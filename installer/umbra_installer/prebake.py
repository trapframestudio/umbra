"""
Umbra Gore & Effects' body models: a prebaked bake when it fits, a bake on this
machine when it does not.

Umbra Gore & Effects cuts every character model of the player's mod list into
parts (its "bake", made by Umbra Effects Setup, tools/zgo_setup in its folder).
Umbra publishes the bake it made for the pack's own mod list (packdef: "Prebaked
bakes"). Setup asks the installed Umbra Effects Setup:

    --check        is there a bake, and is it up to date?        (RESULT body=.. props=.. kid=..)
    --match DIR    would the prebaked bake whose report is in DIR fit?   (RESULT match=yes|no)
    --bake         bake for this mod list here (minutes; the fallback)
    --props        cut the breakable props here (seconds)

The prebaked files go into the mod's folder, next to the layer's own files. They
are not part of the layer: install_mods_layer() carries them over when the layer
is updated (carry_generated), and Umbra Effects Setup says whether they still fit.
"""
import json
import os
import re
import shutil
import subprocess
import time

# what Umbra Effects Setup makes in its folder, each listed (with its files) in a report there
REPORTS = ("zgo_bake_report.json", "zgo_fracture_report.json")
BODY_REPORT = REPORTS[0]
PROGRAM = "Umbra Effects Setup.exe" if os.name == "nt" else "zgo-setup-linux"


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


_PROGRESS = re.compile(r"(\d+)/(\d+)")


def run(prog, args, work, env=None, on_line=None, cancel=None, timeout=None):
    """Run Umbra Effects Setup without its window; returns (exit code, output lines).
    Its output goes to a log file (the Windows program has no console), read as it
    grows: on_line(line) for each new line. cancel() True stops it."""
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
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.Popen(cmd, cwd=os.path.dirname(prog), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError as e:                         # damaged, or not runnable here
        return None, ["Umbra Effects Setup could not start: %s" % e]
    lines, pos, t0 = [], 0, time.monotonic()

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
            line = raw.decode("utf-8", "replace").rstrip("\r")
            lines.append(line)
            if on_line:
                on_line(line)
    try:
        while p.poll() is None:
            if cancel and cancel():
                p.terminate()
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    p.kill()
                raise KeyboardInterrupt
            if timeout and time.monotonic() - t0 > timeout:
                p.kill()
                raise BakeError("Umbra Effects Setup did not finish in %d minutes" % (timeout // 60))
            read_new()
            time.sleep(0.5)
    finally:
        if p.poll() is None:
            p.kill()
    read_new()
    try:                                        # a last line without its newline
        with open(log, "rb") as f:
            f.seek(pos)
            rest = f.read().decode("utf-8", "replace").strip()
        if rest:
            lines.append(rest)
            if on_line:
                on_line(rest)
    except OSError:
        pass
    return p.returncode, lines


# a bake's stages, as Umbra Effects Setup names them (setup_core.STAGE_TEXT), with their share
# of the whole: each counts from 0 on its own
STAGES = (("Getting ready", 0.0, 0.02), ("Reading the game's own models", 0.02, 0.15),
          ("Reading your mod list", 0.15, 0.2), ("Baking models", 0.2, 0.9), ("Writing the index", 0.9, 0.93),
          ("Installing into the Umbra Effects folder", 0.93, 1.0))


def progress_of(line):
    """The whole job's fraction from a progress line of Umbra Effects Setup
    ('  [models] Baking models 120/4790 actors\\x'), or None."""
    s = line.strip()
    if not s.startswith("[") or "] " not in s:
        return None
    m = _PROGRESS.search(s)
    if not m or int(m.group(2)) == 0:
        return None
    f = min(1.0, int(m.group(1)) / int(m.group(2)))
    text = s.split("] ", 1)[1]
    for name, lo, hi in STAGES:
        if text.startswith(name + " "):
            return lo + (hi - lo) * f
    return f


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

