"""
The Mod Organizer 2 instance G.A.M.M.A. installs: finding it, the Umbra
profile, and which profile MO2 opens.

The Umbra profile is made next to G.A.M.M.A.'s (which is never changed): a
copy of G.A.M.M.A.'s profile files with Umbra's load order. MO2 drops list
entries for mods that aren't there and adds unlisted mods itself.
"""
import os
import re
import shutil
import subprocess
import sys

PROFILE_MARKER = "umbra.profile"


class Mo2Error(Exception):
    pass


def is_instance(path):
    return bool(path) and os.path.isfile(os.path.join(path, "ModOrganizer.exe")) and \
        os.path.isdir(os.path.join(path, "profiles"))


def read_ini(path):
    with open(path, "rb") as f:
        raw = f.read()
    return raw.decode("utf-8", errors="replace")


def ini_value(text, section, key):
    sec = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1]
        elif sec == section and "=" in s and s.split("=", 1)[0].strip() == key:
            return s.split("=", 1)[1].strip()
    return None


def unbytearray(v):
    if v and v.startswith("@ByteArray(") and v.endswith(")"):
        v = v[len("@ByteArray("):-1]
    return v


def native_path(win):
    """A path MO2 (a Windows program) wrote -> this system's form."""
    if not win:
        return win
    p = win.replace("\\\\", "\\")
    if os.name == "nt":
        return p
    if re.match(r"^[Zz]:[\\/]", p):
        return "/" + p[3:].replace("\\", "/")
    return p.replace("\\", "/")


def game_path(mo2):
    """The Anomaly folder this instance points at (None if unknown)."""
    try:
        text = read_ini(os.path.join(mo2, "ModOrganizer.ini"))
    except OSError:
        return None
    p = native_path(unbytearray(ini_value(text, "General", "gamePath")))
    return p if p and os.path.isdir(p) else None


def profiles(mo2):
    d = os.path.join(mo2, "profiles")
    try:
        return sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n, "modlist.txt")))
    except OSError:
        return []


def selected_profile(mo2):
    try:
        return unbytearray(ini_value(read_ini(os.path.join(mo2, "ModOrganizer.ini")), "General", "selected_profile"))
    except OSError:
        return None


def set_selected_profile(mo2, name):
    """Make MO2 open this profile (ModOrganizer.ini, line endings kept)."""
    path = os.path.join(mo2, "ModOrganizer.ini")
    with open(path, "rb") as f:
        raw = f.read()
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    lines = raw.split(nl)
    val = b"selected_profile=@ByteArray(" + name.encode("utf-8") + b")"
    sec, done = None, False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(b"[") and s.endswith(b"]"):
            if sec == b"General" and not done:
                lines.insert(i, val)
                done = True
                break
            sec = s[1:-1]
        elif sec == b"General" and s.split(b"=", 1)[0].strip() == b"selected_profile":
            lines[i] = val
            done = True
            break
    if not done:
        if sec == b"General":
            lines.append(val)
        else:
            lines = [b"[General]", val] + lines
    shutil.copy2(path, path + ".umbra-bak")
    with open(path + ".tmp", "wb") as f:
        f.write(nl.join(lines))
    os.replace(path + ".tmp", path)


# ---------------------------------------------------------------- executables (the Run dropdown)
#
# ModOrganizer.ini keeps them as a QSettings array (MO2 2.5):
#     [customExecutables]
#     size=10
#     1\arguments=
#     1\binary=Z:/home/x/anomaly/bin/AnomalyDX11AVX.exe
#     1\hide=false ... 1\title=Anomaly (DX11-AVX) ... 1\workingDirectory=Z:/home/x/anomaly/bin
# Values use QSettings escaping (\\ and \" inside, quotes around a value with
# , ; = or edge spaces). They are global to the instance, not per profile.

EXE_KEYS = ("arguments", "binary", "hide", "ownicon", "steamAppID", "title", "toolbar", "workingDirectory")
_QS_ESC = {"\0": "\\0", "\a": "\\a", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t",
           "\v": "\\v", '"': '\\"', "\\": "\\\\"}
_QS_UNESC = {"0": "\0", "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def qs_escape(s):
    """A string as QSettings writes it into an INI file."""
    out = "".join(_QS_ESC.get(c, c) if (c in _QS_ESC or ord(c) >= 0x20) else "\\x%x" % ord(c) for c in s)
    if any(c in s for c in ",;=") or s[:1] == " " or s[-1:] == " ":
        out = '"' + out + '"'
    return out


def qs_unescape(raw):
    s = raw.strip()
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '"':
            i += 1
            continue
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "x":
                j = i + 2
                while j < len(s) and j < i + 6 and s[j] in "0123456789abcdefABCDEF":
                    j += 1
                out.append(chr(int(s[i + 2:j] or "0", 16)))
                i = j
                continue
            out.append(_QS_UNESC.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def mo2_path(path, under_wine):
    """A path as MO2 stores it (C:/x/y, or Z:/home/x/y under Wine)."""
    p = os.path.abspath(path)
    if under_wine:
        p = "Z:" + p
    return p.replace("\\", "/")


_EXE_LINE = re.compile(rb"^(\d+)\\([^=]+)=(.*)$")


def _exe_section(lines):
    """(start, end) of [customExecutables] in the lines (end exclusive), or None."""
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(b"[") and s.endswith(b"]"):
            if start is not None:
                return start, i
            if s == b"[customExecutables]":
                start = i
    return (start, len(lines)) if start is not None else None


def _parse_exes(body):
    """Section lines -> ([[(key, raw value bytes)], ...] in index order, trailing blank lines)."""
    entries, tail = {}, []
    for line in body:
        if not line.strip():
            tail.append(line)
            continue
        tail = []
        m = _EXE_LINE.match(line.strip())
        if m:
            entries.setdefault(int(m.group(1)), []).append((m.group(2).decode("utf-8"), m.group(3)))
    return [entries[k] for k in sorted(entries)], tail


def read_executables(mo2):
    """The Run dropdown's entries: [{"title": ..., "binary": ..., ...}] (values unescaped)."""
    try:
        with open(os.path.join(mo2, "ModOrganizer.ini"), "rb") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    sec = _exe_section(lines)
    if not sec:
        return []
    entries, _ = _parse_exes(lines[sec[0] + 1:sec[1]])
    return [{k: qs_unescape(v.decode("utf-8", errors="replace")) for k, v in e} for e in entries]


def _title(entry):
    for k, v in entry:
        if k == "title":
            return qs_unescape(v.decode("utf-8", errors="replace"))
    return ""


def set_executables(mo2, exes, remove=(), log=None):
    """Add or update Run-dropdown entries, found by title; remove entries titled
    in `remove`. Every other entry and every other line stays as it was.
    exes: [{"title", "binary", "arguments", "workingDirectory"}] in MO2's path form.
    Returns True when ModOrganizer.ini changed (backed up first)."""
    log = log or (lambda msg, level="info": None)
    path = os.path.join(mo2, "ModOrganizer.ini")
    with open(path, "rb") as f:
        raw = f.read()
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    lines = raw.split(nl)
    sec = _exe_section(lines)
    if sec:
        entries, tail = _parse_exes(lines[sec[0] + 1:sec[1]])
    else:
        entries, tail = [], [b""]
    before = [list(e) for e in entries]
    drop = {t.lower() for t in remove} - {e["title"].lower() for e in exes}
    entries = [e for e in entries if _title(e).lower() not in drop]
    for e in exes:
        want = {"arguments": e.get("arguments", ""), "binary": e["binary"], "hide": "false", "ownicon": "true",
                "steamAppID": "", "title": e["title"], "toolbar": "false",
                "workingDirectory": e.get("workingDirectory", "")}
        found = [x for x in entries if _title(x).lower() == e["title"].lower()]
        if found:
            cur = found[0]
            keys = [k for k, _ in cur]
            for k in ("arguments", "binary", "workingDirectory"):
                v = qs_escape(want[k]).encode("utf-8")
                if k in keys:
                    cur[keys.index(k)] = (k, v)
                else:
                    cur.append((k, v))
        else:
            entries.append([(k, qs_escape(want[k]).encode("utf-8")) for k in EXE_KEYS])
    if entries == before:
        return False
    body = [b"size=%d" % len(entries)]
    for i, e in enumerate(entries, 1):
        body += [b"%d\\%s=%s" % (i, k.encode("utf-8"), v) for k, v in e]
    if sec:
        lines = lines[:sec[0] + 1] + body + tail + lines[sec[1]:]
    else:
        end = lines[:-1] if lines and lines[-1] == b"" else lines
        gap = [b""] if end and end[-1].strip() else []
        lines = end + gap + [b"[customExecutables]"] + body + [b""]
    shutil.copy2(path, path + ".umbra-bak")
    with open(path + ".tmp", "wb") as f:
        f.write(nl.join(lines))
    os.replace(path + ".tmp", path)
    titles = [e["title"] for e in exes]
    log("Mod Organizer 2's Run list: %s" % (", ".join(titles) if titles else "Umbra entries removed"))
    return True


def make_profile(mo2, name, base, modlist, renames=None, off=(), drop=()):
    """Create or update the Umbra profile. modlist: the pack's modlist.txt
    bytes; renames: {pack folder: folder actually used} for layers that had
    to go in "<folder> (Umbra)"; off: folders listed but switched off; drop:
    folders left out of the list (options the player didn't pick)."""
    pdir = os.path.join(mo2, "profiles", name)
    bdir = os.path.join(mo2, "profiles", base)
    if not os.path.isdir(bdir):
        raise Mo2Error('MO2 has no "%s" profile to start from (%s)' % (base, bdir))
    ours = os.path.exists(os.path.join(pdir, PROFILE_MARKER))
    if os.path.isdir(pdir) and not ours:
        # someone's own profile of that name: keep its load order
        shutil.copy2(os.path.join(pdir, "modlist.txt"), os.path.join(pdir, "modlist.txt.umbra-bak"))
    os.makedirs(pdir, exist_ok=True)
    if not ours:
        for fn in os.listdir(bdir):
            src = os.path.join(bdir, fn)
            if os.path.isfile(src) and fn not in ("modlist.txt",) and not fn.startswith("modlist.txt."):
                if not os.path.exists(os.path.join(pdir, fn)):
                    shutil.copy2(src, os.path.join(pdir, fn))
    text = modlist.decode("utf-8")
    out = []
    for line in text.splitlines():
        if line[:1] in "+-":
            if line[1:] in drop:
                continue
            if renames and line[1:] in renames:
                line = line[0] + renames[line[1:]]
            if line[1:] in off:
                line = "-" + line[1:]
        out.append(line)
    text = "\r\n".join(out) + "\r\n"
    with open(os.path.join(pdir, "modlist.txt.tmp"), "wb") as f:
        f.write(text.encode("utf-8"))
    os.replace(os.path.join(pdir, "modlist.txt.tmp"), os.path.join(pdir, "modlist.txt"))
    with open(os.path.join(pdir, PROFILE_MARKER), "w") as f:
        f.write("This profile is managed by the Umbra installer.\n")
    return pdir


def running():
    """Is Mod Organizer 2 open? (It rewrites its files on exit.)"""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ModOrganizer.exe", "/NH"], capture_output=True,
                                 text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "ModOrganizer.exe" in out.stdout
        if sys.platform.startswith("linux"):
            # only MO2's own process: Wine names it after the exe (comm "ModOrganizer.ex"). Not any
            # process with ModOrganizer.exe in its arguments: Steam's reaper and the Umbra launcher
            # carry the command that starts it for as long as the launcher is open.
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open("/proc/%s/comm" % pid, "rb") as f:
                        comm = f.read().strip().lower()
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        a0 = f.read().split(b"\0", 1)[0]
                except OSError:
                    continue
                a0 = a0.decode("utf-8", "replace").replace("\\", "/").rsplit("/", 1)[-1].lower()
                if comm.startswith(b"modorganizer") or a0 == "modorganizer.exe":
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False
