#!/usr/bin/env python3
"""
Build an Umbra pack from the tested MO2 instance (pack/umbra.toml).

    python3 dev/pack_build.py                 build into dist/ (only what changed is re-packed)
    python3 dev/pack_build.py --dry-run       say what would be packed, and check for personal paths
    python3 dev/pack_build.py --out DIR --volume-mb 1024 --jobs 8

Output (upload all of it with dev/b2_upload.py; see installer/umbra_installer/packdef.py):

    dist/objects/<sha256>.zip|.txt   layer volumes, GAMMA lists, the Umbra load order
    dist/packs/umbra-<version>.json  the pack manifest
    dist/channels/<channel>.json     points at the manifest
    dist/CREDITS.md                  everyone whose work is in the pack

Each layer is the mod folder exactly as it is in the instance (MO2's hidden
*.mohidden files left out, meta.ini rewritten without local paths), split
into volumes of about --volume-mb unpacked. Zips are deterministic, so an
unchanged layer gives the same objects and nothing new to upload.

Personal paths: text files that contain this machine's MO2 or Anomaly path
get it replaced by @@UMBRA_*@@ tokens (the installer puts the player's own
in). Any other mention of your home folder is listed as a warning.

Machine-specific files are never packed, whatever the definition says (see
machine_specific()): the co-op identity xrrazom_lan_id.bin, saves, logs,
shader caches, user.ltx and everything else under appdata/ except the preset
.ltx files mods ship there (Atmospherics), and nothing at all under appdata/
for a layer that goes into the Anomaly folder.

Sources ([sources.<id>] in the definition): downloads pinned by sha256, kept
in ~/.cache/umbra-src/. They are read as data only (never run).

    python3 dev/pack_build.py --fetch-sources     download missing sources, check their sha256

A source with players_download = true is one whose author asks that players
download it from their own page (xrRazom, from ModDB, so its downloads are
counted). It is never packed or uploaded: its layers say what they take from
the archive (take = [["folder in the archive/", "folder in the layer/"], ...]),
and the manifest names the archive, its page and its download link, which the
players' Setup downloads (see packdef "Origins").
"""
import argparse
import concurrent.futures as cf
import datetime
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "installer"))
from umbra_installer import fetch as inst_fetch  # noqa: E402
from umbra_installer import layers as inst_layers  # noqa: E402
from umbra_installer import packdef  # noqa: E402
from umbra_installer import prebake as inst_prebake  # noqa: E402

TEXT_EXT = (".json", ".script", ".ltx", ".txt", ".ini", ".xml", ".cfg", ".lua", ".md")
TEXT_MAX = 64 << 20
STORED_EXT = (".ogg", ".zip", ".7z", ".rar", ".png", ".jpg", ".jpeg", ".gz", ".xz", ".exe", ".dll")
EPOCH_1980 = 315532800

# Umbra Gore & Effects' own release rules (its dev/package.py), but the bake stays:
# Umbra ships it baked for exactly this mod list.
ZGO_SKIP_DIRS = {"dev", "tests", "docs", "native", ".git", "__pycache__"}
ZGO_SKIP_TREES = ("tools/zgo_world_assets/", "tools/zgo_wounds/", "tools/zgo_fracture/assets/")


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- reading the definition

class DefError(SystemExit):
    pass


def load_def(path):
    with open(path, "rb") as f:
        d = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(path))
    d["_base"] = base
    d["_credits"] = []
    cp = os.path.join(base, "credits.toml")
    if os.path.exists(cp):
        with open(cp, "rb") as f:
            d["_credits"] = tomllib.load(f).get("credit", [])
    check_def(d)
    return d


def check_def(d):
    """Profiles and layers make sense together (raises DefError)."""
    d.setdefault("layer", [])
    d.setdefault("profiles", [])
    opts = {o["id"] for o in d.get("option", [])}
    layer_opts = set()                # what a layer's option may say: a tick box's id, or "<option>=<choice>"
    for o in d.get("option", []):
        if "choices" in o:
            cids = [c.get("id") for c in o["choices"]]
            if not cids or not all(cids) or len(set(cids)) != len(cids) or o.get("default") not in cids:
                raise DefError('option "%s": its choices need ids, each once, and a default among them' % o["id"])
            layer_opts |= {packdef.choice_key(o["id"], c) for c in cids}
        else:
            layer_opts.add(o["id"])
    prof = {}
    for pr in d["profiles"]:
        if not pr.get("id") or pr["id"] in prof or pr["id"] == packdef.MAIN:
            raise DefError("profiles: missing or repeated id %r" % pr.get("id"))
        if pr.get("option") not in opts or pr["option"] not in layer_opts:
            raise DefError('profile "%s": option %r is not a tick-box [[option]]' % (pr["id"], pr.get("option")))
        prof[pr["id"]] = pr
    mods_layers = [L for L in d["layer"] if L.get("target", "mods") == "mods"]
    for i, L in enumerate(mods_layers):
        if L.get("first") and i:
            raise DefError("layer %s must come first (highest priority), and %s is above it"
                           % (L["id"], ", ".join(x["id"] for x in mods_layers[:i])))
    for sid, s in d.get("sources", {}).items():
        if s.get("players_download") and not all(re.match(r"^https?://\S+$", str(s.get(k, "")))
                                                 for k in ("page", "download")):
            raise DefError("source %s: players download it, so it needs its page and its download link" % sid)
    for L in d["layer"]:
        src = d.get("sources", {}).get(L.get("source")) or {}
        if src.get("players_download"):
            if not L.get("take"):
                raise DefError("layer %s: players download its source themselves; say what the layer takes "
                               "from it (take = [[\"folder in the archive/\", \"folder in the layer/\"]])" % L["id"])
            bad = [k for k in ("archive_root", "archive_include", "archive_member", "without_files_of", "files",
                               "from", "zgo") if k in L]
            if bad:
                raise DefError("layer %s: %s can't apply to an archive players download as it is" % (L["id"], bad))
        elif L.get("take"):
            raise DefError("layer %s: take is for a source players download themselves (players_download)" % L["id"])
        if L.get("profile"):
            pr = prof.get(L["profile"])
            if not pr:
                raise DefError("layer %s: no profile %r" % (L["id"], L["profile"]))
            if L.get("option", pr["option"]) != pr["option"]:
                raise DefError("layer %s: a profile's layer comes with its option" % L["id"])
            L["option"] = pr["option"]
        if L.get("source") and L["source"] not in d.get("sources", {}):
            raise DefError("layer %s: no [sources.%s]" % (L["id"], L["source"]))
        if L.get("option") and L["option"] not in layer_opts:
            raise DefError("layer %s: option %r is not an [[option]] (or <option>=<choice>)" % (L["id"], L["option"]))
        for oid in L.get("without_files_of", []):
            if oid == L["id"] or oid not in {x["id"] for x in d["layer"]}:
                raise DefError("layer %s: without_files_of names no other layer %r" % (L["id"], oid))
        if "files" in L and any(k in L for k in ("archive", "source", "take")):
            raise DefError("layer %s: files go with a folder (from), not with an archive" % L["id"])
    folders = {L["folder"].lower() for L in d["layer"] if L.get("target", "mods") == "mods"}
    olds = [x.lower() for L in d["layer"] for x in L.get("was", [])]
    if len(olds) != len(set(olds)) or folders & set(olds):
        raise DefError("a layer's earlier folder name (was) is another layer's folder, or listed twice")
    return d


def resolve(base, p):
    return os.path.normpath(os.path.join(base, os.path.expanduser(p)))


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def parse_meta(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), v.strip())
    return out


def zgo_version(files):
    """Umbra Gore & Effects' own version (VERSION in zgo_main.script), from a layer's file list."""
    for rel, src, *_ in files:
        if rel == "gamedata/scripts/zgo_main.script":
            m = re.search(rb'^VERSION\s*=\s*"([^"]+)"', read_source(src), re.M)
            return m.group(1).decode() if m else None
    return None


def read_meta(folder):
    try:
        with open(os.path.join(folder, "meta.ini"), encoding="utf-8", errors="replace") as f:
            return parse_meta(f.read())
    except OSError:
        return {}


# ---------------------------------------------------------------- machine-specific files

MACHINE_FILES = {"xrrazom_lan_id.bin"}                          # xrRazom's per-machine co-op identity
JUNK_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}
MACHINE_DIRS = {"savedgames", "shaders_cache", "logs", "screenshots", "crashdumps"}
INSTALLER_SUFFIXES = (".umbra-new", ".umbra-bak", ".umbra-old", ".umbra-tmp")


def machine_specific(rel, target):
    """Why this file must never be packed (None: it may be)."""
    parts = rel.lower().split("/")
    name = parts[-1]
    if name in MACHINE_FILES:
        return "co-op identity"
    if name in JUNK_FILES:
        return "Windows Explorer's own file"
    if name == inst_layers.MARKER or name.endswith(INSTALLER_SUFFIXES):
        return "installer file"
    if parts[0] != "gamedata" and any(p in MACHINE_DIRS for p in parts[:-1]):
        return "saves, logs or caches"
    if parts[0] == "appdata":
        if target == "anomaly":
            return "appdata"
        # a mod's own presets (appdata/Atmos_Cold.ltx) are fine; user.ltx and the rest are this machine's
        if len(parts) != 2 or not name.endswith(".ltx") or name.startswith("user"):
            return "appdata"
    return None


# ---------------------------------------------------------------- Windows programs' imports

# DLL names Windows looks for next to every program that starts (hooks and
# proxies such as OptiScaler's dxgi.dll): a profile's engine never puts one
# beside the co-op exes, imported or not.
PROXY_DLLS = {"dxgi.dll", "d3d9.dll", "d3d10.dll", "d3d11.dll", "d3d12.dll", "dinput8.dll", "version.dll",
              "winmm.dll", "winhttp.dll", "dsound.dll", "xinput1_3.dll", "xinput1_4.dll", "opengl32.dll"}


def pe_imports(data):
    """The DLLs a Windows program or DLL imports (static and delay-load), lower case."""
    try:
        if data[:2] != b"MZ":
            return set()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return set()
        nsec, = struct.unpack_from("<H", data, pe + 6)
        opt_size, = struct.unpack_from("<H", data, pe + 20)
        opt = pe + 24
        magic, = struct.unpack_from("<H", data, opt)
        dirs = opt + (112 if magic == 0x20B else 96)
        ndirs, = struct.unpack_from("<I", data, dirs - 4)
        secs = [struct.unpack_from("<IIII", data, opt + opt_size + 40 * i + 8) for i in range(nsec)]

        def off(rva):
            for vsize, va, rsize, raw in secs:
                if va <= rva < va + max(vsize, rsize):
                    return raw + rva - va
            return None
        out = set()
        for idx, step, name_at in ((1, 20, 12), (13, 32, 4)):       # import, delay-load import
            if idx >= ndirs:
                continue
            rva, _size = struct.unpack_from("<II", data, dirs + 8 * idx)
            o = off(rva) if rva else None
            while o is not None and o + step <= len(data) and data[o:o + step] != b"\0" * step:
                no = off(struct.unpack_from("<I", data, o + name_at)[0])
                if no is not None:
                    out.add(data[no:data.index(b"\0", no)].decode("ascii", "replace").lower())
                o += step
        return out
    except (struct.error, ValueError):
        return set()


def option_entry(o):
    """An [[option]] as the manifest has it: a tick box, or a pick-one with choices."""
    out = {k: o.get(k, "") for k in ("id", "title", "description")}
    if "choices" in o:
        out["default"] = o["default"]
        out["choices"] = [{k: c[k] for k in ("id", "title", "description") if k in c} for c in o["choices"]]
    else:
        out["default"] = bool(o.get("default"))
    return out


# ---------------------------------------------------------------- sources

_verified = {}


def source_path(d, sid, check=True):
    """The local file of a pinned source, its sha256 checked (once per run)."""
    s = d["sources"][sid]
    p = resolve(d["_base"], s["file"])
    if not os.path.isfile(p):
        if not s.get("url"):
            raise DefError("source %s is not here (%s): download it from %s" % (sid, p, s.get("page") or "its page"))
        raise DefError("source %s is not downloaded (%s): run python3 dev/pack_build.py --fetch-sources" % (sid, p))
    if check:
        st = os.stat(p)
        key = (p, st.st_size, st.st_mtime, s["sha256"])
        if key not in _verified:
            got = packdef.sha256_file(p)
            if got != s["sha256"]:
                raise DefError("source %s: sha256 is %s, the definition pins %s" % (sid, got, s["sha256"]))
            _verified[key] = True
    return p


def fetch_sources(d):
    for sid, s in sorted(d.get("sources", {}).items()):
        p = resolve(d["_base"], s["file"])
        if os.path.isfile(p):
            source_path(d, sid)
            say("source %s: have it, sha256 ok" % sid)
            continue
        if not s.get("url") and not s.get("download"):
            say("source %s: not here, and it has no direct link: download it from %s and save it as %s"
                % (sid, s.get("page") or "its author's page", p))
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if s.get("url"):
            say("source %s: downloading %s" % (sid, s["url"]))
            resp = urllib.request.urlopen(urllib.request.Request(s["url"], headers={"User-Agent": "UmbraPackBuild"}),
                                          timeout=120)
        else:                     # the author's page, as players' Setup downloads it
            say("source %s: downloading from %s" % (sid, s["page"]))
            resp = inst_fetch.open_origin({"origin": {"name": os.path.basename(p), "page": s["page"],
                                                      "download": s["download"]}}, timeout=120)
        with resp as r, open(p + ".part", "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        got = packdef.sha256_file(p + ".part")
        if got != s["sha256"]:
            os.unlink(p + ".part")
            raise DefError("source %s: downloaded sha256 %s does not match the pinned %s" % (sid, got, s["sha256"]))
        os.replace(p + ".part", p)
        say("source %s: saved %s" % (sid, p))


def clean_meta(src_meta, version=None):
    """meta.ini without this machine's paths (MO2 needs only a few keys)."""
    url = src_meta.get("url", "")
    if not url.startswith(("http://", "https://")):
        url = ""
    lines = [
        "[General]",
        "gameName=%s" % (src_meta.get("gameName") or "stalkeranomaly"),
        "modid=0",
        "version=%s" % (version or src_meta.get("version", "")),
        "newestVersion=",
        'category="-1,"',
        "installationFile=",
        "repository=",
        "comments=Installed by Umbra",
        "url=%s" % url,
        "hasCustomURL=%s" % ("true" if url else "false"),
        "",
        "[installedFiles]",
        "size=0",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------- file lists

def walk_folder(folder, zgo=False):
    """[(rel, abspath)] of a mod folder, sorted; meta.ini and hidden files left out."""
    out = []
    for dp, dns, fns in os.walk(folder):
        rel_dir = os.path.relpath(dp, folder).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        if zgo:
            dns[:] = [d for d in dns if d not in ZGO_SKIP_DIRS and not d.startswith(".")
                      and not (rel_dir + d + "/").startswith(ZGO_SKIP_TREES)]
        dns.sort()
        for fn in sorted(fns):
            rel = rel_dir + fn
            if rel == "meta.ini" or fn.endswith(".mohidden"):
                continue
            if zgo and (fn.startswith(".") or fn.endswith((".pyc", ".part"))):
                continue
            out.append((rel, os.path.join(dp, fn)))
    return out


UNPACKED = os.path.join(os.path.expanduser("~"), ".cache", "umbra-src", "unpacked")


def unpacked(apath, root, key):
    """A folder with root (a folder inside a 7z or other non-zip archive)
    unpacked from it, once: 7-Zip reads the archive; the folder is kept in
    ~/.cache/umbra-src/unpacked/<key> for the next build."""
    out = os.path.join(UNPACKED, key[:24])
    done = os.path.join(out + ".done", re.sub(r"[^A-Za-z0-9._-]", "_", root or "_all_"))   # outside what is read
    folder = os.path.join(out, *root.split("/")) if root else out
    if os.path.exists(done):
        return folder
    seven = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    if not seven:
        raise DefError("%s is not a zip: reading it needs 7-Zip (7z) on the PATH" % apath)
    say("  unpacking %s from %s (once; kept in %s)" % (root or "everything", os.path.basename(apath), out))
    cmd = [seven, "x", "-y", "-bso0", "-bsp0", "-o" + out, apath] + ([root] if root else [])
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0 or not os.path.isdir(folder):
        raise DefError("7-Zip could not unpack %r from %s: %s" % (root, apath,
                                                                  r.stdout.decode("utf-8", "replace")[-400:]))
    os.makedirs(os.path.dirname(done), exist_ok=True)
    open(done, "w").close()
    return folder


def archive_entries(zpath, root, include):
    """[(rel, (zpath, member))] for a layer taken from a zip."""
    root = root.strip("/") + "/" if root else ""
    out = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(root):
                continue
            rel = info.filename[len(root):]
            if include and not any(rel.startswith(p) for p in include):
                continue
            out.append((rel, (zpath, info.filename)))
    return sorted(out)


def _zip_mtime(info):
    return time.mktime(info.date_time + (0, 0, -1))


def nested_entries(zpath, member, include):
    """[(rel, ("nested", zpath, member, entry), size, mtime)] for a zip inside a zip.
    Names written with backslashes (some Windows zippers) count as folders."""
    with zipfile.ZipFile(zpath) as z:
        inner = zipfile.ZipFile(io.BytesIO(z.read(member)))
    out = []
    for info in inner.infolist():
        rel = info.filename.replace("\\", "/")
        if rel.endswith("/") or info.is_dir():
            continue
        rel = "/".join(p for p in rel.split("/") if p not in ("", "."))
        if include and not any(rel.startswith(p) for p in include):
            continue
        out.append((rel, ("nested", zpath, member, info.filename), info.file_size, _zip_mtime(info)))
    return sorted(out)


_inner_cache = {}


def _inner_zip(zpath, member):
    key = (zpath, member)
    if key not in _inner_cache:
        if len(_inner_cache) > 8:
            _inner_cache.clear()
        with zipfile.ZipFile(zpath) as z:
            _inner_cache[key] = zipfile.ZipFile(io.BytesIO(z.read(member)))
    return _inner_cache[key]


# ---------------------------------------------------------------- personal paths

def path_forms(path):
    """The ways a path to this machine can appear in a text file."""
    forms = set()
    p = os.path.normpath(path)
    variants = {p, os.path.realpath(p)}
    for v in list(variants):
        if v.startswith("/var/home/"):
            variants.add(v[len("/var"):])
        elif v.startswith("/home/"):
            variants.add("/var" + v)
    for v in variants:
        forms.add(("", v))                                  # /home/x/...
        win = "Z:" + v.replace("/", "\\")
        forms.add(("_WIN", win))                            # Z:\home\x\...
        forms.add(("_WIN_ESC", win.replace("\\", "\\\\")))  # Z:\\home\\x\\...
        forms.add(("_WIN", "Z:" + v))                       # Z:/home/x/... (MO2 writes these)
    return sorted(forms, key=lambda f: -len(f[1]))


def zgo_cache_dir():
    """Umbra Gore & Effects' baker work folder on this machine (its bake.py cache_dir)."""
    if os.environ.get("ZGO_BAKER_CACHE"):
        return os.path.abspath(os.environ["ZGO_BAKER_CACHE"])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], "zgo_baker")
    return os.path.expanduser("~/.cache/zgo_baker")


def relocate_bytes(data, mo2, anomaly):
    """Replace this machine's MO2, Anomaly and ZGO-cache paths with tokens; returns (data, n)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0
    n = 0
    for name, base in (("MO2", mo2), ("ANOMALY", anomaly), ("ZGO_CACHE", zgo_cache_dir())):
        for suffix, form in path_forms(base):
            if form in text:
                n += text.count(form)
                text = text.replace(form, "@@UMBRA_%s%s@@" % (name, suffix))
    return text.encode("utf-8"), n


def home_mentions(data, homes):
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return []
    found = []
    for h in homes:
        for m in re.finditer(re.escape(h) + r"[^\s\"'<>]{0,60}", text):
            found.append(m.group(0))
            if len(found) > 3:
                return found
    return found


# ---------------------------------------------------------------- volumes

def chunk(entries, limit):
    """Split [(rel, src, size)] into volumes of about `limit` unpacked bytes."""
    vols, cur, cur_bytes = [], [], 0
    for e in entries:
        if cur and cur_bytes + e[2] > limit:
            vols.append(cur)
            cur, cur_bytes = [], 0
        cur.append(e)
        cur_bytes += e[2]
    if cur:
        vols.append(cur)
    return vols


def _ut_extra(mtime):
    # Info-ZIP extended timestamp: the exact modification time in UTC
    return struct.pack("<HHBl", 0x5455, 5, 1, int(mtime))


PROGRAM_MAGIC = (b"\x7fELF", b"#!")   # a Linux program or script: packed executable (rwxr-xr-x)


def is_program(head):
    """head: a file's first bytes. Linux programs and scripts are packed executable whatever
    their mode here, since MO2 (and Windows) installs lose the mode bits."""
    return head.startswith(PROGRAM_MAGIC)


def _zinfo(rel, mtime, program=False):
    t = max(int(mtime), EPOCH_1980)
    zi = zipfile.ZipInfo(rel, date_time=time.gmtime(t)[:6])
    zi.external_attr = (0o755 if program else 0o644) << 16
    zi.create_system = 3
    zi.extra = _ut_extra(t)
    zi.compress_type = zipfile.ZIP_STORED if rel.lower().endswith(STORED_EXT) else zipfile.ZIP_DEFLATED
    return zi


def read_source(src):
    """Bytes of a source: a file path, (zip path, member), ('nested', zip, inner zip, entry)
    or ('bytes', data)."""
    if isinstance(src, tuple):
        if src[0] == "bytes":
            return src[1]
        if src[0] == "nested":
            return _inner_zip(src[1], src[2]).read(src[3])
        with zipfile.ZipFile(src[0]) as z:
            return z.read(src[1])
    with open(src, "rb") as f:
        return f.read()


def source_mtime(src):
    if isinstance(src, tuple):
        if src[0] == "bytes":
            return src[2] if len(src) > 2 else EPOCH_1980
        if src[0] == "nested":
            return _zip_mtime(_inner_zip(src[1], src[2]).getinfo(src[3]))
        with zipfile.ZipFile(src[0]) as z:
            return _zip_mtime(z.getinfo(src[1]))
    return os.stat(src).st_mtime


def build_volume(job):
    """Runs in a worker: write one volume, return (sha256, size)."""
    entries, tmp_dir = job
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for rel, src, _size, mtime, override in entries:
                if override is not None or isinstance(src, tuple):
                    data = override if override is not None else read_source(src)
                    z.writestr(_zinfo(rel, mtime, is_program(data[:4])), data)
                else:
                    with open(src, "rb") as fin:
                        program = is_program(fin.read(4))
                    zi = _zinfo(rel, mtime, program)
                    with open(src, "rb") as fin, z.open(zi, "w", force_zip64=True) as fout:
                        while True:
                            b = fin.read(1 << 20)
                            if not b:
                                break
                            fout.write(b)
        sha = packdef.sha256_file(tmp)
        return sha, os.path.getsize(tmp), tmp
    except BaseException:
        os.unlink(tmp)
        raise


# ---------------------------------------------------------------- the build

class Builder:
    def __init__(self, d, out, volume_bytes, jobs, dry):
        self.d, self.out, self.volume_bytes, self.jobs, self.dry = d, out, volume_bytes, jobs, dry
        base = d["_base"]
        src = d["source"]
        self.mo2 = resolve(base, src["mo2"])
        self.anomaly = resolve(base, src["anomaly"])
        self.profile = src["profile"]
        self.warnings = []
        self.homes = sorted({os.path.expanduser("~"), os.path.realpath(os.path.expanduser("~"))}, key=len,
                            reverse=True)
        self.cache_path = os.path.join(out, ".buildcache.json")
        try:
            with open(self.cache_path) as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    # -- objects
    def put_bytes(self, data, ext):
        sha = packdef.sha256_bytes(data)
        rel = "objects/%s.%s" % (sha, ext)
        if not self.dry:
            p = os.path.join(self.out, rel)
            if not os.path.exists(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(p + ".tmp", p)
        return {"path": rel, "sha256": sha, "size": len(data)}

    # -- profiles
    def profiles(self):
        """The main profile ([profile], id "main") and the extra ones ([[profiles]])."""
        main = dict(self.d["profile"])
        main["id"] = packdef.MAIN
        return [main] + [dict(p) for p in self.d.get("profiles", [])]

    def off_folders(self, pr):
        """Mods switched off in this profile: layer ids become their folders."""
        by_id = {L["id"]: L.get("folder") for L in self.d["layer"]}
        return {by_id.get(x) or x for x in pr.get("off", [])}

    # -- the load order of a profile
    def modlist(self, pr=None):
        pr = pr or self.profiles()[0]
        path = os.path.join(self.mo2, "profiles", self.profile, "modlist.txt")
        with open(path, encoding="utf-8-sig") as f:
            lines = [l.rstrip("\r\n") for l in f]
        drop = set(self.d["profile"].get("drop", [])) | set(pr.get("drop", []))
        off = self.off_folders(pr)
        mods_layers = [L for L in self.d["layer"] if L.get("target", "mods") == "mods"]
        # the tested profile lists a layer under its folder there (a renamed one under an
        # earlier name or its source folder's): the layers' own lines above replace those
        lf = {L["folder"] for L in mods_layers} | {x for L in mods_layers for x in L.get("was", [])}
        src_mods = os.path.normcase(os.path.realpath(os.path.join(self.mo2, "mods")))
        for L in mods_layers:
            if "from" in L:
                f = os.path.realpath(resolve(self.d["_base"], L["from"]))
                if os.path.normcase(os.path.dirname(f)) == src_mods:
                    lf.add(os.path.basename(f))
        out = ["# This file was automatically generated by Mod Organizer."]
        for L in mods_layers:
            # every profile lists every layer, so a layer of another profile is in its place, switched off
            on = not L.get("profile") or L["profile"] == pr["id"]
            out.append(("+" if on and L["folder"] not in off else "-") + L["folder"])
        for l in lines:
            if not l or l.startswith("#") or l[1:] in drop or l[1:] in lf:
                continue
            out.append(("-" + l[1:]) if l[1:] in off else l)
        return ("\r\n".join(out) + "\r\n").encode("utf-8")

    # -- a layer's files: [(rel, src, size, mtime, override_bytes|None)]
    def layer_files(self, L):
        base = self.d["_base"]
        target = L.get("target", "mods")
        meta = {}
        if "files" in L and "from" not in L:         # only single files
            files = self.single_files(L)
        elif L.get("take"):                           # the author's own archive, as players download it
            zpath = source_path(self.d, L["source"])
            if not zipfile.is_zipfile(zpath):
                raise DefError("layer %s: players' Setup reads only zips, and %s isn't one" % (L["id"], zpath))
            with zipfile.ZipFile(zpath) as z:
                ents = inst_layers.take_entries(z.infolist(), L["take"])
            files = [(rel, (zpath, info.filename), info.file_size, _zip_mtime(info), None) for rel, info in ents]
            if not files:
                raise DefError("layer %s: take finds nothing in %s" % (L["id"], zpath))
            meta = {"url": self.d["sources"][L["source"]]["page"]}
        elif "archive" in L or "source" in L:
            if "source" in L:
                zpath = source_path(self.d, L["source"])
                sroot = self.d["sources"][L["source"]].get("root", "")
                root = "/".join(x.strip("/") for x in (sroot, L.get("archive_root", "")) if x)
                key = self.d["sources"][L["source"]]["sha256"]
            else:
                zpath, root = resolve(base, L["archive"]), L.get("archive_root", "")
                st = os.stat(zpath)
                key = packdef.sha256_bytes(("%s|%d|%d" % (zpath, st.st_size, st.st_mtime)).encode())
            files = []
            if not zipfile.is_zipfile(zpath):         # a 7z: unpacked once, then read as a folder
                if L.get("archive_member"):
                    raise DefError("layer %s: archive_member needs a zip" % L["id"])
                folder = unpacked(zpath, root.strip("/"), key)
                inc = L.get("archive_include", [])
                for rel, p in walk_folder(folder):
                    if inc and not any(rel.startswith(x) for x in inc):
                        continue
                    st = os.stat(p)
                    files.append((rel, p, st.st_size, st.st_mtime, None))
            elif L.get("archive_member"):             # a zip inside the zip (an MO2 mod)
                member = "/".join(x.strip("/") for x in (root, L["archive_member"]) if x)
                for rel, src, size, mtime in nested_entries(zpath, member, L.get("archive_include", [])):
                    files.append((rel, src, size, mtime, None))
            else:
                ents = archive_entries(zpath, root, L.get("archive_include", []))
                with zipfile.ZipFile(zpath) as z:
                    for rel, (zp, member) in ents:
                        zi = z.getinfo(member)
                        files.append((rel, (zp, member), zi.file_size, _zip_mtime(zi), None))
            if not files:
                raise DefError("layer %s: nothing in %s under %r" % (L["id"], zpath, root))
            m = [f for f in files if f[0].lower() == "meta.ini"]
            if m:
                meta = parse_meta(read_source(m[0][1]).decode("utf-8", errors="replace"))
                files = [f for f in files if f[0].lower() != "meta.ini"]
        else:
            folder = self.layer_folder(L)
            if not os.path.isdir(folder):
                raise DefError("layer %s: %s is not there" % (L["id"], folder))
            meta = read_meta(folder)
            files = []
            for rel, p in walk_folder(folder, zgo=L.get("zgo", False)):
                st = os.stat(p)
                files.append((rel, p, st.st_size, st.st_mtime, None))
            if "files" in L:                          # a folder plus single files (they win)
                singles = self.single_files(L)
                taken = {f[0].lower() for f in singles}
                files = [f for f in files if f[0].lower() not in taken] + singles
        if L.get("without_files_of"):               # e.g. a mod's shared files, minus what each choice brings
            other = set()
            for oid in L["without_files_of"]:
                OL = next(x for x in self.d["layer"] if x["id"] == oid)
                other |= {f[0].lower() for f in self.layer_files(OL)[0] if f[0] != "meta.ini"}
            files = [f for f in files if f[0].lower() not in other]
        if L.get("version"):
            meta["version"] = L["version"]
        elif L.get("zgo") and zgo_version(files):
            meta["version"] = zgo_version(files)    # MO2's own meta.ini may hold a date stamp (d2026.11.14)
        elif not meta.get("version") and L.get("source"):
            meta["version"] = self.d["sources"][L["source"]].get("version", "")
        if L.get("url"):
            meta["url"] = L["url"]
        if target == "mods" and not L.get("take"):     # (an author's archive gets it from the manifest)
            files.append(("meta.ini", ("bytes", b""), 0, EPOCH_1980, clean_meta(meta)))
        return files, meta

    def layer_folder(self, L):
        """A folder layer's source: "from", else the tested MO2's mods/<folder>."""
        return resolve(self.d["_base"], L["from"]) if "from" in L else os.path.join(self.mo2, "mods", L["folder"])

    def single_files(self, L):
        """A layer's files = {"path in the mod" = "file"}: [(rel, src, size, mtime, None)]."""
        files = []
        for rel, srcp in sorted(L["files"].items()):
            p = resolve(self.d["_base"], srcp)
            if not os.path.isfile(p):
                raise DefError("layer %s: %s is not there" % (L["id"], p))
            st = os.stat(p)
            files.append((rel.replace("\\", "/").strip("/"), p, st.st_size, st.st_mtime, None))
        return files

    def origin_volume(self, L, files):
        """The author's archive itself, as the manifest names it for players' Setup to download."""
        s = self.d["sources"][L["source"]]
        zpath = source_path(self.d, L["source"])
        host = re.sub(r"^www\.", "", s["page"].split("/")[2])
        return {"path": "origins/%s.zip" % s["sha256"], "sha256": s["sha256"], "size": os.path.getsize(zpath),
                "files": len(files), "bytes": sum(f[2] for f in files),
                "origin": {"name": s.get("name") or os.path.basename(zpath),
                           "site": s.get("site") or ("ModDB" if host == "moddb.com" else host),
                           "page": s["page"], "download": s["download"]},
                "take": [list(t) for t in L["take"]]}

    def leave_out(self, L, files):
        """Drop machine-specific files, whatever the definition says; returns (kept, [(rel, why)])."""
        kept, out = [], []
        for f in files:
            why = machine_specific(f[0], L.get("target", "mods"))
            if why:
                out.append((f[0], why))
            else:
                kept.append(f)
        return kept, out

    def profile_created(self):
        """Files Umbra's profile layers added to this machine's Anomaly folder (if it has them)."""
        out = set()
        for L in self.d["layer"]:
            if L.get("profile") and L.get("target") == "anomaly":
                m = inst_layers.anomaly_marker(self.anomaly, L["id"]) or {}
                out |= {r.lower() for r in m.get("created", [])}
        return out

    def coop_imports(self, folder, shared, mine):
        """DLLs that the co-op setup's programs in this Anomaly subfolder import."""
        key = folder.lower()
        if key not in self._imports:
            names = set()
            for low, src in shared.items():
                if os.path.dirname(low) == key and low.endswith(".exe"):
                    names |= pe_imports(read_source(src))
            d = inst_layers.find_ci(self.anomaly, folder) if folder else self.anomaly
            for fn in (sorted(os.listdir(d)) if d and os.path.isdir(d) else []):
                rel = (folder + "/" + fn if folder else fn).lower()
                if fn.lower().endswith(".exe") and rel not in mine:
                    names |= pe_imports(read_bytes(os.path.join(d, fn)))
            self._imports[key] = names
        return self._imports[key]

    def check_profile_engine(self, L, files, shared_anomaly):
        """A profile's Anomaly-folder layer may only add new files: never one the co-op
        engine (another Anomaly-folder layer) or the Anomaly install itself has, and
        never a DLL the co-op programs beside it would load (one they import, or a
        proxy name like dxgi.dll), unless coop_shared_dlls names it on purpose."""
        mine = self.profile_created()          # what Umbra's profile layers added here before is theirs
        allowed = {x.lower() for x in L.get("coop_shared_dlls", [])}
        clash = []
        for f in files:
            low = f[0].lower()
            if low in shared_anomaly or (low not in mine and inst_layers.find_ci(self.anomaly, f[0])):
                clash.append("%s (replaces a file)" % f[0])
                continue
            name = os.path.basename(low)
            if name.endswith(".dll") and name not in allowed:
                if name in PROXY_DLLS:
                    clash.append("%s (every program in that folder loads it)" % f[0])
                elif name in self.coop_imports(os.path.dirname(f[0]), shared_anomaly, mine):
                    clash.append("%s (the co-op exes there would load it)" % f[0])
        if clash:
            raise DefError("layer %s (profile %s): %s. A profile's engine may only add files the co-op setup "
                           "doesn't use." % (L["id"], L["profile"], "; ".join(clash[:6])))

    def scan_and_relocate(self, L, files):
        """Tokens for this machine's paths in text files; warnings for other home paths."""
        relocated, out = [], []
        for rel, src, size, mtime, override in files:
            if override is None and rel.lower().endswith(TEXT_EXT) and size <= TEXT_MAX:
                data = read_source(src)
                new, n = relocate_bytes(data, self.mo2, self.anomaly)
                if n:
                    relocated.append(rel)
                    override, size = new, len(new)
                    data = new
                left = home_mentions(data, self.homes)
                if left:
                    self.warnings.append("%s: %s mentions your home folder: %s" % (L["id"], rel, ", ".join(left[:2])))
            out.append((rel, src, size, mtime, override))
        return out, relocated

    # -- Umbra Gore & Effects' bake (packdef "Prebaked bakes")
    def split_bake(self, L, files):
        """(the mod's own files, its bake): what Umbra Effects Setup made in the folder is
        listed in its reports (inst_prebake.generated)."""
        made = inst_prebake.generated(self.layer_folder(L))
        return [f for f in files if f[0] not in made], [f for f in files if f[0] in made]

    def bake_names(self, L, files):
        """The profile and folder names the bake records become tokens: Setup puts in the
        player's (the bake is the same for every profile name and folder name)."""
        folder = os.path.basename(os.path.normpath(self.layer_folder(L)))
        # (a bake made before the mod's folder was renamed records the old name: "was")
        names = {"profile": ({self.profile}, "@@UMBRA_PROFILE@@"),
                 "zgo_mod": ({folder, L["folder"]} | set(L.get("was", [])), "@@UMBRA_ZGO_FOLDER@@")}

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in names and v in names[k][0]:
                        node[k] = names[k][1]
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        out, named = [], []
        for rel, src, size, mtime, override in files:
            if rel in inst_prebake.REPORTS or rel == "gamedata/scripts/zgo_bake_inputs.script":
                data = override if override is not None else read_source(src)
                if rel.endswith(".json"):
                    rep = json.loads(data.decode("utf-8"))
                    walk(rep)
                    data = json.dumps(rep, indent=1, sort_keys=True).encode("utf-8")
                else:
                    for k, (was, tok) in names.items():
                        alts = b"|".join(re.escape(w.encode()) for w in sorted(was))
                        data = re.sub(rb'^%s = "(?:%s)"' % (k.encode(), alts),
                                      ('%s = "%s"' % (k, tok)).encode(), data, flags=re.M)
                override, size = data, len(data)
                named.append(rel)
            out.append((rel, src, size, mtime, override))
        return out, named

    def prebake(self, L, files, pname):
        """Pack the bake into umbra-effects/<fingerprint>/ (its report volume first) and write its
        manifest there; returns the layer's bake object."""
        files, relocated = self.scan_and_relocate(L, files)
        files, named = self.bake_names(L, files)
        body = [f for f in files if f[0] == inst_prebake.BODY_REPORT]
        if not body:
            raise DefError("layer %s: the bake has files but no %s" % (L["id"], inst_prebake.BODY_REPORT))
        rel, src, size, mtime, override = body[0]
        rep = json.loads((override if override is not None else read_source(src)).decode("utf-8"))
        inputs = rep.get("inputs") or {}
        fp = inputs.get("fingerprint") or ""
        if not re.match(r"^[0-9a-f]{32}$", fp):
            raise DefError("layer %s: its bake has no fingerprint (made by an Umbra Gore & Effects before 1.7); "
                           "bake again with Umbra Effects Setup" % L["id"])
        reports = [f for f in files if f[0] in inst_prebake.REPORTS]
        vols = [reports] + chunk([f for f in files if f[0] not in inst_prebake.REPORTS], self.volume_bytes)
        prefix = "%s/%s/" % (packdef.BAKE_DIR, fp)
        key_layer = {"id": "bake-" + fp, "target": "bake"}
        objs, jobs = [None] * len(vols), []
        for i, v in enumerate(vols):
            key = self.cache_key(key_layer, v)
            hit = self.cache.get(key)
            if hit and hit["path"].startswith(prefix) and os.path.exists(os.path.join(self.out, hit["path"])):
                objs[i] = dict(hit)
            else:
                jobs.append((i, key, v))
        nbytes = sum(f[2] for f in files)
        say("  %-22s %6d files %10s in %d volumes (%s; %d to pack)" % (
            "zgo bake", len(files), packdef.human(nbytes), len(vols), prefix, len(jobs)))
        if self.dry:
            return {"path": prefix + "0" * 64 + ".json", "sha256": "0" * 64, "size": 0, "download": 0,
                    "bytes": nbytes}
        os.makedirs(os.path.join(self.out, prefix), exist_ok=True)
        tmp_dir = os.path.join(self.out, ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        with cf.ProcessPoolExecutor(max_workers=self.jobs) as ex:
            futs = {ex.submit(build_volume, (v, tmp_dir)): (i, key, v) for i, key, v in jobs}
            for n, fut in enumerate(cf.as_completed(futs)):
                i, key, v = futs[fut]
                sha, size, tmp = fut.result()
                orel = "%s%s.zip" % (prefix, sha)
                dst = os.path.join(self.out, orel)
                if os.path.exists(dst):
                    os.unlink(tmp)
                else:
                    os.replace(tmp, dst)
                objs[i] = {"path": orel, "sha256": sha, "size": size, "files": len(v), "bytes": sum(f[2] for f in v)}
                self.cache[key] = objs[i]
                self.save_cache()
                say("  packed bake %d/%d: %s" % (n + 1, len(jobs), packdef.human(size)))
        man = {"format": packdef.BAKE_FORMAT, "kind": "umbra-effects-bake", "fingerprint": fp,
               "baker": inputs.get("baker", 0), "zgo": inputs.get("zgo", ""), "made": rep.get("time", ""),
               "models": rep.get("baked", 0), "files": len(files), "bytes": nbytes,
               "relocate": sorted(set(relocated) | set(named)), "report": objs[0], "volumes": objs[1:]}
        packdef.check_bake(man)
        # sorted keys: a volume from the build cache comes back with its keys in another order
        blob = (json.dumps(man, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        sha = packdef.sha256_bytes(blob)
        mrel = "%s%s.json" % (prefix, sha)
        self.write(mrel, blob)
        say("  bake manifest %s (%d models, %s to download)" % (
            mrel, man["models"], packdef.human(sum(o["size"] for o in objs))))
        return {"path": mrel, "sha256": sha, "size": len(blob), "download": sum(o["size"] for o in objs),
                "bytes": nbytes}

    def bake_only(self):
        """Build only Umbra Gore & Effects' prebaked bake (no pack, no channel)."""
        out = []
        for L in self.d["layer"]:
            if not L.get("zgo"):
                continue
            files, _meta = self.layer_files(L)
            files, _left = self.leave_out(L, files)
            _, bake_files = self.split_bake(L, files)
            if not bake_files:
                raise DefError("layer %s: %s has no bake (run Umbra Effects Setup there first)" % (L["id"], self.layer_folder(L)))
            out.append(self.prebake(L, bake_files, self.d["profile"]["name"]))
        if not out:
            raise DefError("the pack has no zgo layer")
        self.save_cache()
        for w in self.warnings:
            say("WARNING " + w)
        return out

    def cache_key(self, L, vol):
        h = packdef.hashlib.sha256()
        src_sha = self.d["sources"][L["source"]]["sha256"] if L.get("source") else ""
        h.update(json.dumps([L["id"], L.get("target", "mods"), self.volume_bytes, 2, src_sha]).encode())
        for rel, src, size, mtime, override in vol:
            h.update(("%s\0%d\0%d\0" % (rel, size, int(mtime))).encode("utf-8"))
            if override is not None:
                h.update(packdef.hashlib.sha256(override).digest())
        return h.hexdigest()

    def build(self):
        d = self.d
        pname = d["profile"]["name"]
        t0 = time.time()
        layers, jobs = [], []
        say("Source: %s (profile %s)" % (self.mo2, self.profile))
        shared_anomaly = {}                   # lower-case path -> source, of the shared Anomaly-folder layers
        self._imports = {}
        # shared Anomaly-folder layers first: a profile's engine is checked against all of them
        order = sorted(d["layer"], key=lambda L: bool(L.get("profile")))
        done_files = {}
        for L in order:
            files, meta = self.layer_files(L)
            files, left_out = self.leave_out(L, files)
            if L.get("target", "mods") == "anomaly":
                if L.get("profile"):
                    self.check_profile_engine(L, files, shared_anomaly)
                else:
                    shared_anomaly.update({f[0].lower(): f[1] for f in files})
            done_files[L["id"]] = (files, meta, left_out)
        for L in d["layer"]:
            files, meta, left_out = done_files[L["id"]]
            bake_obj = None
            if L.get("zgo"):
                # its bake goes up as its own download (packdef "Prebaked bakes"); the layer is the mod
                files, bake_files = self.split_bake(L, files)
                if bake_files:
                    bake_obj = self.prebake(L, bake_files, pname)
                else:
                    self.warnings.append("layer %s: %s has no bake, so every player bakes on their own machine "
                                         "(Umbra Effects Setup, a few minutes)" % (L["id"], L["folder"]))
            if L.get("take"):
                if left_out:
                    raise DefError("layer %s: players get the author's archive as it is, and it has machine-specific "
                                   "files: %s" % (L["id"], ", ".join(r for r, _ in left_out[:4])))
                vol = self.origin_volume(L, files)
                entry = {"id": L["id"], "target": L.get("target", "mods"), "credit": L.get("credit", L["id"]),
                         "version": meta.get("version", ""), "files": len(files), "bytes": vol["bytes"],
                         "relocate": [], "volumes": [vol]}
                if entry["target"] == "mods":
                    entry["folder"] = L["folder"]
                    entry["meta_ini"] = clean_meta(meta).decode("utf-8")
                for k in ("option", "profile", "preserve", "was"):
                    if L.get(k):
                        entry[k] = L[k]
                layers.append(entry)
                say("  %-22s %6d files %10s from the author's %s (players download it from %s)" % (
                    L["id"], len(files), packdef.human(vol["bytes"]), vol["origin"]["name"], vol["origin"]["site"]))
                continue
            files, relocated = self.scan_and_relocate(L, files)
            nbytes = sum(f[2] for f in files)
            vols = chunk(files, self.volume_bytes)
            version = meta.get("version", "") if meta else ""
            if L.get("zgo"):
                m = re.search(r"^version=(\S+)", clean_meta(meta).decode(), re.M)
                version = m.group(1) if m else version
            entry = {"id": L["id"], "target": L.get("target", "mods"), "credit": L.get("credit", L["id"]),
                     "version": version, "files": len(files), "bytes": nbytes, "relocate": relocated,
                     "volumes": [None] * len(vols)}
            if entry["target"] == "mods":
                entry["folder"] = L["folder"]
            for k in ("option", "profile", "preserve", "was"):
                if L.get(k):
                    entry[k] = L[k]
            if bake_obj:
                entry["bake"] = bake_obj
            layers.append(entry)
            say("  %-22s %6d files %10s in %d volume%s%s%s" % (
                L["id"], len(files), packdef.human(nbytes), len(vols), "" if len(vols) == 1 else "s",
                ("; %d with this machine's paths -> tokens" % len(relocated)) if relocated else "",
                ("  [profile %s]" % L["profile"]) if L.get("profile") else ""))
            if left_out:
                say("      left out %d machine-specific file%s: %s" % (
                    len(left_out), "" if len(left_out) == 1 else "s",
                    ", ".join("%s (%s)" % x for x in left_out[:4]) + (" ..." if len(left_out) > 4 else "")))
            for i, v in enumerate(vols):
                key = self.cache_key(L, v)
                hit = self.cache.get(key)
                if hit and os.path.exists(os.path.join(self.out, hit["path"])):
                    entry["volumes"][i] = dict(hit)
                else:
                    jobs.append((entry, i, key, v))
        todo_bytes = sum(sum(f[2] for f in j[3]) for j in jobs)
        say("%d volume%s to pack (%s), %d unchanged" % (
            len(jobs), "" if len(jobs) == 1 else "s", packdef.human(todo_bytes),
            sum(1 for L in layers for v in L["volumes"] if v)))
        for w in self.warnings:
            say("WARNING " + w)
        if self.dry:
            # the manifest as it would be (stand-in volumes), checked like a real one
            for L in layers:
                L["volumes"] = [v or {"path": "objects/%s.zip" % ("0" * 64), "sha256": "0" * 64, "size": 0}
                                for v in L["volumes"]]
            pack = self.manifest(layers)
            packdef.check_pack(pack)
            self.summary(pack)
            return pack          # the stand-in manifest (nothing written)

        os.makedirs(os.path.join(self.out, "objects"), exist_ok=True)
        tmp_dir = os.path.join(self.out, ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        done = 0
        with cf.ProcessPoolExecutor(max_workers=self.jobs) as ex:
            futs = {ex.submit(build_volume, (v, tmp_dir)): (entry, i, key, v) for entry, i, key, v in jobs}
            for fut in cf.as_completed(futs):
                entry, i, key, v = futs[fut]
                sha, size, tmp = fut.result()
                rel = "objects/%s.zip" % sha
                dst = os.path.join(self.out, rel)
                if os.path.exists(dst):
                    os.unlink(tmp)
                else:
                    os.replace(tmp, dst)
                obj = {"path": rel, "sha256": sha, "size": size, "files": len(v), "bytes": sum(f[2] for f in v)}
                entry["volumes"][i] = obj
                self.cache[key] = obj
                done += 1
                say("  packed %d/%d  %s volume %d: %s" % (done, len(jobs), entry["id"], i + 1, packdef.human(size)))
                self.save_cache()
        self.save_cache()

        pack = self.manifest(layers)
        packdef.check_pack(pack)
        self.summary(pack)
        blob = (json.dumps(pack, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        prel = "packs/umbra-%s.json" % pack["version"]
        self.write(prel, blob)
        chan = {"format": packdef.FORMAT, "version": pack["version"], "pack": prel,
                "sha256": packdef.sha256_bytes(blob), "size": len(blob)}
        self.write("channels/%s.json" % pack["channel"], (json.dumps(chan, indent=1) + "\n").encode())
        self.write("CREDITS.md", credits_md(pack).encode("utf-8"))
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        say("Pack %s %s: download %s, installs %s; built in %.0f s -> %s" % (
            pack["name"], pack["version"], packdef.human(packdef.total_download(pack)),
            packdef.human(packdef.total_unpacked(pack)), time.time() - t0, self.out))
        return pack

    def manifest(self, layers):
        d = self.d
        g = d["gamma"]
        gamma = {
            "cli": {"version": g["cli_version"], "assets": {k: dict(v) for k, v in g["cli"].items()}},
            "repos": dict(g["repos"]),
            "mo2_profile": g["mo2_profile"],
            "modpack_maker_list": self.put_bytes(read_bytes(resolve(d["_base"], g["modpack_maker_list"])), "txt"),
            "modlist": self.put_bytes(read_bytes(resolve(d["_base"], g["modlist"])), "txt"),
        }
        profs = []
        for pr in self.profiles():
            out = {"id": pr["id"], "name": pr["name"], "base": pr.get("base") or d["profile"]["base"],
                   "modlist": self.put_bytes(self.modlist(pr), "txt")}
            for k in ("option", "requires", "steam"):
                if pr.get(k):
                    out[k] = pr[k]
            if pr.get("executable"):
                out["executables"] = [{k: e[k] for k in ("title", "in", "binary", "cwd", "arguments") if k in e}
                                      for e in pr["executable"]]
            if pr.get("user_ltx"):
                out["user_ltx"] = {k: pr["user_ltx"][k] for k in ("file", "seed", "lines") if k in pr["user_ltx"]}
            profs.append(out)
        return {
            "format": packdef.FORMAT,
            "name": d["pack"]["name"], "version": d["pack"]["version"], "channel": d["pack"]["channel"],
            "built": datetime.date.today().isoformat(),
            "gamma": gamma,
            "profile": profs[0],
            "profiles": profs[1:],
            "user_ltx": {"lines": list(d.get("user_ltx", {}).get("lines", []))},
            "options": [option_entry(o) for o in d.get("option", [])],
            "layers": layers,
            "dependencies": [{k: x[k] for k in ("id", "title", "url", "profile", "required", "fetched", "notes")
                              if k in x} for x in d.get("dependency", [])],
            "credits": [{k: c.get(k, "") for k in ("id", "mod", "authors", "url", "role", "notes", "version",
                                                    "license")}
                        for c in d["_credits"]],
        }

    def summary(self, pack):
        """What the profiles get, and what is still missing."""
        for pr in packdef.all_profiles(pack):
            mine = [L["id"] for L in pack["layers"] if L.get("profile") == pr["id"]]
            say('Profile "%s"%s: %s%s%s' % (
                pr["name"], (" (option %s)" % pr["option"]) if pr.get("option") else "",
                ("its own layers " + ", ".join(mine)) if mine else "the shared layers",
                ("; Run list: " + ", ".join(e["title"] for e in pr.get("executables", []))) if pr.get("executables")
                else "",
                ("; settings file %s" % pr["user_ltx"]["file"]) if pr.get("user_ltx", {}).get("file") else ""))
        lines = pack.get("user_ltx", {}).get("lines", [])
        say("user.ltx: %s" % ((", ".join(lines)) if lines else "nothing shipped"))
        for dep in pack.get("dependencies", []):
            if not dep.get("fetched"):
                say("NOTE %s (%s) is %s and not in the pack yet: %s" % (
                    dep["title"], dep.get("profile") or "all", "required" if dep.get("required") else "optional",
                    dep.get("url", "")))
        missing = sorted({L["credit"] for L in pack["layers"]} - {c["id"] for c in pack["credits"]})
        if missing:
            say("WARNING no credits.toml entry for: " + ", ".join(missing))

    def write(self, rel, data):
        p = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)

    def save_cache(self):
        with open(self.cache_path + ".tmp", "w") as f:
            json.dump(self.cache, f, indent=0, sort_keys=True)
        os.replace(self.cache_path + ".tmp", self.cache_path)


ROLE_TITLES = [("base", "The base"), ("engine", "Engine"), ("extra", "Mods in Umbra"),
               ("gamma-optional", "G.A.M.M.A. options Umbra turns on"),
               ("baked-models", "Models in Umbra Gore & Effects' bake"), ("tool", "Tools")]


def credits_md(pack):
    out = ["# Umbra credits", "",
           "Umbra is made of other people's work. Thank you, all of you.", ""]
    by_role = {}
    for c in pack["credits"]:
        by_role.setdefault(c.get("role") or "extra", []).append(c)
    for role, title in ROLE_TITLES + [(r, r) for r in sorted(by_role) if r not in dict(ROLE_TITLES)]:
        cs = by_role.get(role)
        if not cs:
            continue
        out += ["## " + title, ""]
        for c in cs:
            who = ", ".join(c.get("authors") or []) or "author unknown"
            name = "[%s](%s)" % (c["mod"], c["url"]) if c.get("url") else c["mod"]
            ver = (" " + c["version"]) if c.get("version") else ""
            lic = (" Licence: %s." % c["license"]) if c.get("license") else ""
            out.append("- %s%s by %s%s%s" % (name, ver, who, (". " + c["notes"]) if c.get("notes") else "", lic))
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--def", dest="defn", default=os.path.join(ROOT, "pack", "umbra.toml"))
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--volume-mb", type=int, default=1024)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fetch-sources", action="store_true", help="download the pinned sources and stop")
    ap.add_argument("--channel", choices=("stable", "testing"),
                    help="the channel this build is for (default: [pack] channel in the definition)")
    ap.add_argument("--bake-only", action="store_true",
                    help="build only Umbra Gore & Effects' prebaked bake into OUT/umbra-effects/ (no pack, no channel)")
    a = ap.parse_args()
    d = load_def(a.defn)
    if a.channel:
        d["pack"]["channel"] = a.channel
    if a.fetch_sources:
        fetch_sources(d)
        return 0
    b = Builder(d, os.path.abspath(a.out), a.volume_mb << 20, a.jobs, a.dry_run)
    if a.bake_only:
        b.bake_only()
        return 0
    b.build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
