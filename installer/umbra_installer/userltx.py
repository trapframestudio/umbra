"""
Console settings the pack ships, written into Anomaly's appdata/user.ltx.

user.ltx is one "command value" line per setting ("r3_msaa st_opt_off";
key bindings are "bind <action> <key>", so their key is two words). The
engine runs its default_controls first and user.ltx over them, so a short
file is fine.

Rules (the player's settings always win):
  * only keys the file lacks are added; a key the player has is never changed
  * a missing file is created
  * the file is backed up (<name>.umbra-bak) before it is changed

A profile with its own settings file (its engine started with -ltx <file>)
gets it made once as a copy of the seed file with the profile's lines set
over it (that engine's defaults); after that only missing keys are added.
"""
import os
import shutil

TWO_WORD = ("bind", "bind_sec", "bind_gpad", "unbind", "unbind_sec")


def key(line):
    """The setting a line sets (lower case), or None for blank and comment lines."""
    parts = line.split()
    if not parts or parts[0].startswith(("#", ";", "//")):
        return None
    k = parts[0].lower()
    if k in TWO_WORD and len(parts) > 1:
        return k + " " + parts[1].lower()
    return k


def _read(path):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="surrogateescape")
    nl = "\r\n" if "\r\n" in text else "\n"
    return text, nl


def _write(path, text):
    tmp = path + ".umbra-tmp"
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8", errors="surrogateescape"))
    os.replace(tmp, path)


def merge(path, lines, log=None):
    """Add the lines whose keys the file lacks (making it if missing); returns the lines added."""
    log = log or (lambda msg, level="info": None)
    wanted = []
    for line in lines:
        k = key(line)
        if k and k not in [key(w) for w in wanted]:
            wanted.append(line.strip())
    if not wanted:
        return []
    if os.path.exists(path):
        text, nl = _read(path)
        have = {key(l) for l in text.splitlines()}
        add = [w for w in wanted if key(w) not in have]
        if not add:
            return []
        shutil.copy2(path, path + ".umbra-bak")
        if text and not text.endswith(("\n", "\r")):
            text += nl
        _write(path, text + nl.join(add) + nl)
        log("%s: added %s" % (os.path.basename(path), ", ".join(add)))
        return add
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write(path, "\r\n".join(wanted) + "\r\n")
    log("%s: created with %s" % (os.path.basename(path), ", ".join(wanted)))
    return wanted


def seed(path, seed_path, lines, log=None):
    """A profile's own settings file: made once from the seed with these lines set
    over it; if it exists already, only missing keys are added (merge)."""
    if os.path.exists(path):
        return merge(path, lines, log)
    log = log or (lambda msg, level="info": None)
    over = {}
    for line in lines:
        k = key(line)
        if k:
            over.setdefault(k, line.strip())
    out, nl, done = [], "\r\n", set()
    if seed_path and os.path.exists(seed_path):
        text, nl = _read(seed_path)
        for l in text.splitlines():
            k = key(l)
            if k in over:
                if k not in done:
                    out.append(over[k])
                    done.add(k)
                continue
            out.append(l)
    out += [v for k, v in over.items() if k not in done]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write(path, nl.join(out) + nl)
    log("%s: made%s, with %s" % (os.path.basename(path),
                                  (" from " + os.path.basename(seed_path)) if seed_path and
                                  os.path.exists(seed_path) else "", ", ".join(over.values())))
    return list(over.values())
