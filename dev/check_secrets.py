#!/usr/bin/env python3
"""
Fail if files carry private data: key values, home paths, Steam ids, or the
developer's own private terms. Stdlib only. Never prints a matched value, only
where it is and what kind it is.

    python3 dev/check_secrets.py --staged      what `git commit` would record
    python3 dev/check_secrets.py PATH...       files and/or folders (walks folders, skips .git)

What counts as private is read on this machine at run time, so nothing
private lives in this file:
  - every value in ~/.config/umbra/*.env (KEY=VALUE lines);
  - every line of ~/.config/umbra/private-terms (e.g. your email and real
    name; one per line, # comments allowed, case-insensitive);
  - the home folder in any form (/home/<user>, /var/home/<user>, Z:/home/<user>,
    C:/Users/<user>);
  - Steam account ids, SteamID64s, account and persona names from every Steam
    root on this machine (userdata/*, config/loginusers.vdf);
  - key-shaped strings (GitHub tokens, private key blocks, B2/AWS key shapes).

Override the config folder with UMBRA_PRIVATE_DIR. Exit 0 when clean, 1 on any
hit, 2 on a usage error.

Use it as a git pre-commit hook (hooks aren't committed):
    printf '#!/bin/sh\nexec python3 <path to>/check_secrets.py --staged\n' > .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
"""
import getpass
import glob
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
PRIVATE_DIR = os.environ.get("UMBRA_PRIVATE_DIR", os.path.join(HOME, ".config", "umbra"))
STEAM_ROOTS = [
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
    "~/snap/steam/common/.local/share/Steam",
]
STEAMID64_BASE = 76561197960265728
MIN_TERM = 4  # shorter private terms would match everywhere

# Generic key shapes. Written so that this file doesn't match itself.
SHAPES = [
    ("GitHub token", rb"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ("private key block", rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    ("AWS key id", rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("B2 application key", rb"\bK00[0-9A-Za-z+/]{27}\b"),
]


def _env_values():
    out = []
    for path in sorted(glob.glob(os.path.join(PRIVATE_DIR, "*.env"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if not m:
                continue
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if len(value) >= MIN_TERM:
                out.append((f"value of {m.group(1)} from {os.path.basename(path)}", value))
    return out


def _private_terms():
    path = os.path.join(PRIVATE_DIR, "private-terms")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for n, line in enumerate(lines, 1):
        term = line.strip()
        if term and not term.startswith("#") and len(term) >= MIN_TERM:
            out.append((f"private term on line {n} of private-terms", term))
    return out


def _steam():
    ids, names = set(), set()
    for root in STEAM_ROOTS:
        root = os.path.expanduser(root)
        for d in glob.glob(os.path.join(root, "userdata", "*")):
            base = os.path.basename(d)
            if base.isdigit() and int(base) > 0:
                ids.add(base)
                ids.add(str(STEAMID64_BASE + int(base)))
        try:
            with open(os.path.join(root, "config", "loginusers.vdf"), encoding="utf-8", errors="replace") as f:
                vdf = f.read()
        except OSError:
            continue
        for sid in re.findall(r'"(7656119\d{10})"', vdf):
            ids.add(sid)
            ids.add(str(int(sid) - STEAMID64_BASE))
        for key in ("AccountName", "PersonaName"):
            for name in re.findall(r'"%s"\s+"([^"]*)"' % key, vdf):
                if len(name) >= MIN_TERM:
                    names.add(name)
    out = [("Steam id", i) for i in sorted(ids)]
    out += [("Steam account or persona name", n) for n in sorted(names)]
    return out


def build_rules():
    """[(label, compiled bytes regex)], plus warnings about missing sources."""
    warnings = []
    rules = []
    user = re.escape(getpass.getuser()).encode()
    rules.append(("home path", re.compile(
        rb"(?:/var)?/home/" + user + rb"(?![A-Za-z0-9_.-])"
        rb"|[A-Za-z]:[/\\]+home[/\\]+" + user + rb"(?![A-Za-z0-9_.-])"
        rb"|[A-Za-z]:[/\\]+users[/\\]+" + user + rb"(?![A-Za-z0-9_.-])", re.I)))
    real_home = os.path.realpath(HOME).encode()
    if real_home not in (b"/", b""):
        rules.append(("home path", re.compile(re.escape(real_home) + rb"(?![A-Za-z0-9_.-])")))

    env = _env_values()
    if not env:
        warnings.append(f"no key values found in {PRIVATE_DIR}/*.env")
    for label, value in env:
        rules.append((label, re.compile(re.escape(value.encode()))))

    terms = _private_terms()
    if terms is None:
        warnings.append(f"no {PRIVATE_DIR}/private-terms file: email and name are not checked")
    for label, term in terms or []:
        rules.append((label, re.compile(re.escape(term.encode()), re.I)))

    for label, value in _steam():
        if label == "Steam id":
            rules.append((label, re.compile(rb"(?<![0-9])" + value.encode() + rb"(?![0-9])")))
        else:
            rules.append((label, re.compile(rb"(?<![A-Za-z0-9])" + re.escape(value.encode()) + rb"(?![A-Za-z0-9])", re.I)))

    for label, pattern in SHAPES:
        rules.append((label, re.compile(pattern)))
    return rules, warnings


def scan(name, data, rules):
    """[(name, line, label)] for every rule that matches data (bytes)."""
    hits = []
    for label, rx in rules:
        for m in rx.finditer(data):
            line = data.count(b"\n", 0, m.start()) + 1
            hits.append((name, line, label))
    return hits


def _walk(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d != ".git")
                for f in sorted(files):
                    yield os.path.join(root, f)
        else:
            yield p


def _staged():
    """(path, blob bytes) for every file added, copied, modified or renamed in the index."""
    names = subprocess.run(["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
                           check=True, capture_output=True).stdout.split(b"\0")
    for raw in names:
        if raw:
            path = os.fsdecode(raw)
            blob = subprocess.run(["git", "show", ":" + path], check=True, capture_output=True).stdout
            yield path, blob


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    rules, warnings = build_rules()
    for w in warnings:
        print("check_secrets: warning: " + w, file=sys.stderr)
    hits, count = [], 0
    if argv == ["--staged"]:
        items = _staged()
    else:
        def items_from_disk():
            for path in _walk(argv):
                try:
                    with open(path, "rb") as f:
                        yield path, f.read()
                except OSError as e:
                    print(f"check_secrets: can't read {path}: {e.strerror}", file=sys.stderr)
        items = items_from_disk()
    for name, data in items:
        count += 1
        hits += scan(name, data, rules)
    hits = sorted(set(hits))
    for name, line, label in hits:
        print(f"{name}:{line}: {label}")
    if hits:
        print(f"check_secrets: {len(hits)} hit(s) in {len({h[0] for h in hits})} of {count} file(s); nothing was committed"
              if argv == ["--staged"] else
              f"check_secrets: {len(hits)} hit(s) in {len({h[0] for h in hits})} of {count} file(s)", file=sys.stderr)
        return 1
    print(f"check_secrets: {count} file(s) clean", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
