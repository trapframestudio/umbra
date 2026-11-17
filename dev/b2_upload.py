#!/usr/bin/env python3
"""
Publish a built Umbra pack (dev/pack_build.py's dist/ folder) to a Backblaze B2 bucket.

    export B2_KEY_ID=<keyID> B2_APP_KEY=<applicationKey>   # from the environment only, never the command line
    python3 dev/b2_upload.py --dry-run                      # check the files, list what would go up; no network
    python3 dev/b2_upload.py --bucket NAME                  # upload: shows the plan, then asks
    python3 dev/b2_upload.py --bucket NAME --yes --base-url-check https://f003.backblazeb2.com/file/NAME

What goes up: each channel in DIST/channels (or only the --channel ones), the
pack manifest it points at, every object that manifest names, and CREDITS.md.
Files already in the bucket (same name, same size, same sha256 in the B2 file
info) are skipped, so running it again only sends what changed. A big file
whose upload was cut off resumes from the parts B2 already has.

Order: every object first (with the volumes of Umbra Gore & Effects' prebaked
bakes, umbra-effects/<fingerprint>/), then the bake manifests, then
umbra-effects/index.json (the list of bakes that Umbra Effects Setup reads; written
into DIST here, merged with the one already in the bucket: per fingerprint the
newest manifest), then the packs (and CREDITS.md), then the channels last, so a
player never sees a channel or a list that points at something missing.

--bakes publishes only the prebaked bakes in DIST/umbra-effects (built with
dev/pack_build.py --bake-only): no pack, no channel changes.

Checks before anything is sent: every file to upload must match its content
address (objects/<sha256>.*; a pack must match its channel's sha256), and the
upload is refused if a pack names an object that is neither in DIST nor
already in the bucket. Moving a channel to an older version is refused
unless --allow-downgrade is given.

B2 details: the native API (v3) over plain urllib. Files above
--large-threshold go up as B2 large files in parts (each part's SHA1 checked
by B2), with retries and back-off; a failed upload gets a fresh upload URL,
an expired session signs in again. Content types: .zip application/zip,
.json application/json, .txt text/plain. Cache hints (b2-cache-control):
objects and bakes "public, max-age=31536000, immutable"; packs, channels and
CREDITS.md "public, max-age=300".

The key is never printed: every line this tool writes is scrubbed of the key
ID, the application key and the session tokens.

Exit status: 0 done (or nothing to do), 1 refused or failed (the message says
why; channels are never switched before what they need is in place),
2 bad usage or no key in the environment, 3 uploaded but --base-url-check
failed, 130 stopped with Ctrl-C (run it again to continue).
"""
import argparse
import base64
import concurrent.futures as cf
import hashlib
import http.client
import json
import os
import random
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "installer"))
from umbra_installer import packdef  # noqa: E402

API_URL = "https://api.backblazeb2.com"
API = "/b2api/v3/"
USER_AGENT = "umbra-b2-upload/1"
TIMEOUT = 120                     # seconds per socket operation
RETRIES = 8                       # tries after the first, per request
BACKOFF = 1.0                     # first wait in seconds, doubled each try...
BACKOFF_CAP = 64.0                # ...up to this
CHUNK = 1 << 20
DEFAULT_PART = 100_000_000        # B2's usual recommendedPartSize (the sign-in reply has the real one)
DEFAULT_LARGE = 200_000_000
MAX_SMALL = 5_000_000_000         # b2_upload_file's limit
MAX_PARTS = 10_000
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_SHORT = "public, max-age=300"
CONTENT_TYPES = {".zip": "application/zip", ".json": "application/json", ".txt": "text/plain",
                 ".md": "text/markdown; charset=utf-8"}
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
NET_ERRORS = (OSError, http.client.HTTPException)     # urllib.error.URLError is an OSError
PHASES = ("objects", "bakes", "index", "packs", "channels")
_OBJ = re.compile(r"^objects/([0-9a-f]{64})\.[A-Za-z0-9]+$")
BAKES = packdef.BAKE_DIR + "/"   # umbra-effects/: Umbra Gore & Effects' prebaked bakes
_BAKE = re.compile(r"^%s([0-9a-f]{32})/([0-9a-f]{64})\.(json|zip)$" % BAKES)
# the list of bakes Umbra Effects Setup reads (Umbra Gore & Effects' tools/zgo_setup/prebaked.py)
BAKE_INDEX = BAKES + "index.json"


class Refuse(Exception):
    """A reason not to (go on) uploading; the message is for the user."""


class Stopped(Exception):
    """Ctrl-C, or another upload thread failed."""


class B2Error(Exception):
    def __init__(self, api, status, code, message="", retry_after=None):
        self.api, self.status, self.code, self.message, self.retry_after = api, status, code, message, retry_after
        super().__init__("%s failed: %s%s%s" % (api, ("HTTP %d " % status) if status else "", code,
                                                (" (%s)" % message) if message and message != code else ""))


# ---------------------------------------------------------------- output, with the key kept out of it

_secret_lock = threading.Lock()
_secrets = set()
_print_lock = threading.Lock()


def add_secret(s):
    if s:
        with _secret_lock:
            _secrets.add(str(s))
    return s


def scrub(text):
    with _secret_lock:
        found = sorted(_secrets, key=len, reverse=True)
    for s in found:
        text = text.replace(s, "[hidden]")
    return text


def say(*a, err=False):
    msg = scrub(" ".join(str(x) for x in a))
    with _print_lock:
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def human(n):
    return packdef.human(n)


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def eta(sec):
    sec = int(sec)
    if sec < 90:
        return "%ds" % sec
    if sec < 5400:
        return "%dm" % (sec // 60)
    return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)


_UNITS = {"": 1, "b": 1, "k": 10 ** 3, "kb": 10 ** 3, "m": 10 ** 6, "mb": 10 ** 6, "g": 10 ** 9, "gb": 10 ** 9,
          "kib": 1 << 10, "mib": 1 << 20, "gib": 1 << 30}


def parse_size(s):
    """'100MB', '1.5GB', '64MiB' or plain bytes."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*", s)
    if not m or m.group(2).lower() not in _UNITS:
        raise argparse.ArgumentTypeError("not a size: %r (try 100MB)" % s)
    n = int(float(m.group(1)) * _UNITS[m.group(2).lower()])
    if n <= 0:
        raise argparse.ArgumentTypeError("a size must be more than 0")
    return n


# ---------------------------------------------------------------- the local pack

class Item:
    """One file to publish: rel is its path in DIST and (after --prefix) in the bucket."""

    def __init__(self, rel, path, size, kind, sha256, version=None, channel=None):
        self.rel, self.path, self.size, self.kind = rel, path, size, kind
        self.sha256, self.version, self.channel = sha256, version, channel
        self.pack = None               # a channel's pack (rel path)
        self.local = True
        self.sha1 = None
        self.part_size = None          # set: goes up as a B2 large file in parts of this size
        self.part_sha1s = None
        self.resume_id = None          # an unfinished B2 large file to carry on with
        self.have_parts = set()
        self.cancel_ids = []
        self.note = ""

    @property
    def phase(self):
        return {"object": "objects", "bake-volume": "objects", "bake": "bakes", "bake-index": "index",
                "pack": "packs", "credits": "packs", "channel": "channels"}[self.kind]

    def content_type(self):
        return CONTENT_TYPES.get(os.path.splitext(self.rel)[1].lower(), "application/octet-stream")

    def cache_control(self):
        return CACHE_IMMUTABLE if self.kind in ("object", "bake", "bake-volume") else CACHE_SHORT

    def info(self):
        d = {"sha256": self.sha256, "b2-cache-control": self.cache_control()}
        if self.version:
            d["version"] = self.version
        return d

    def layout(self):
        """[(part number, offset, length)] for a large file."""
        ps = self.part_size
        return [(i + 1, off, min(ps, self.size - off)) for i, off in enumerate(range(0, self.size, ps))]

    def to_send(self):
        if self.part_size:
            return sum(n for i, _, n in self.layout() if i not in self.have_parts)
        return self.size

    def short(self):
        m = _OBJ.match(self.rel)
        if m:
            return "objects/%s...%s" % (m.group(1)[:12], os.path.splitext(self.rel)[1])
        m = _BAKE.match(self.rel)
        return "%s%s.../%s...%s" % (BAKES, m.group(1)[:8], m.group(2)[:12], "." + m.group(3)) if m else self.rel


class Local:
    def __init__(self, dist):
        self.dist = dist
        self.channels = []
        self.packs = {}
        self.objects = {}
        self.bakes = {}                # bake manifests and their volumes (umbra-effects/<fingerprint>/...)
        self.credits = None
        self.index = None              # umbra-effects/index.json (add_index)
        self.unreferenced = 0

    def items(self):
        out = sorted(self.objects.values(), key=lambda i: i.rel)
        out += sorted(self.bakes.values(), key=lambda i: (i.kind == "bake", i.rel))
        if self.index:
            out.append(self.index)
        out += sorted(self.packs.values(), key=lambda i: i.rel)
        if self.credits:
            out.append(self.credits)
        return out + self.channels


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def object_refs(node, out=None):
    """Every {"path": "objects/...", "sha256": ...} entry anywhere in a pack manifest."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if isinstance(node.get("path"), str) and node["path"].startswith("objects/") and "sha256" in node:
            out.append(node)
        for v in node.values():
            object_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            object_refs(v, out)
    return out


def add_bake(loc, ref, where):
    """A prebaked bake (packdef "Prebaked bakes"): its manifest, and if that is in DIST, its
    report and data volumes. One that is not in DIST must already be in the bucket."""
    m = _BAKE.match(str(ref.get("path", "")))
    if not m or m.group(3) != "json" or m.group(2) != ref.get("sha256") or not isinstance(ref.get("size"), int):
        raise Refuse("%s names a bake manifest that is not content-addressed: %r" % (where, ref.get("path")))
    if ref["path"] in loc.bakes:
        return
    path = os.path.join(loc.dist, *ref["path"].split("/"))
    it = loc.bakes[ref["path"]] = Item(ref["path"], path, ref["size"], "bake", ref["sha256"])
    if not os.path.isfile(path):
        it.local = False
        return
    blob = _read(path)
    if packdef.sha256_bytes(blob) != ref["sha256"] or len(blob) != ref["size"]:
        raise Refuse("%s does not match what %s says (damaged; rebuild it)" % (ref["path"], where))
    try:
        b = packdef.check_bake(packdef.loads(blob, ref["path"]), ref)
    except packdef.PackError as e:
        raise Refuse("%s: %s" % (ref["path"], e))
    for v in [b["report"]] + b["volumes"]:
        old = loc.bakes.get(v["path"])
        if old is None:
            vi = loc.bakes[v["path"]] = Item(v["path"], os.path.join(loc.dist, *v["path"].split("/")), v["size"],
                                             "bake-volume", v["sha256"])
            if not os.path.isfile(vi.path):
                vi.local = False
            elif os.path.getsize(vi.path) != vi.size:
                raise Refuse("%s is %d bytes but %s says %d (damaged; rebuild the bake)"
                             % (vi.rel, os.path.getsize(vi.path), ref["path"], vi.size))
        elif old.size != v["size"]:
            raise Refuse("%s is listed with two different sizes" % v["path"])


def _bake_format(e):
    """An index entry's manifest format (an entry without one is format 1)."""
    f = e.get("format", packdef.BAKE_FORMAT)
    return f if isinstance(f, int) else packdef.BAKE_FORMAT


def bake_entries(loc):
    """The list's entries for the bake manifests in DIST: per fingerprint and format the
    newest (by its "made", then by the manifest file's date)."""
    best = {}
    for it in loc.bakes.values():
        if it.kind != "bake" or not it.local:
            continue
        b = packdef.check_bake(packdef.loads(_read(it.path), it.rel))
        e = {"path": it.rel, "sha256": it.sha256, "size": it.size, "fingerprint": b["fingerprint"],
             "format": b["format"], "baker": b.get("baker", 0), "zgo": b.get("zgo", ""), "made": b.get("made", ""),
             "models": b.get("models", 0), "download": sum(v["size"] for v in b["volumes"]),
             "bytes": b.get("bytes", 0)}
        key = (str(e["made"]), os.path.getmtime(it.path))
        slot = (e["fingerprint"], e["format"])
        if slot not in best or key > best[slot][0]:
            best[slot] = (key, e)
    return [e for _, e in best.values()]


def bake_index(entries, remote=()):
    """umbra-effects/index.json: entries, plus those of the bucket's list (remote) for other
    fingerprints or formats; the format-1 (loose) bakes first, newest first within a format.
    (Umbra Effects Setup before 1.8.3 tries the first entries in turn and gives up at a
    manifest in a format it cannot read: packdef, "Prebaked bakes".)"""
    mine = {(e["fingerprint"], _bake_format(e)) for e in entries}
    keep = [e for e in remote or () if isinstance(e, dict) and _BAKE.match(str(e.get("path", "")))
            and (e.get("fingerprint"), _bake_format(e)) not in mine]
    bakes = sorted(entries + keep, key=lambda e: (str(e.get("made", "")), e["path"]), reverse=True)
    bakes.sort(key=_bake_format)            # stable: newest first within each format
    return (json.dumps({"format": 1, "kind": "umbra-effects-bakes", "bakes": bakes}, indent=1, sort_keys=True)
            + "\n").encode("utf-8")


def add_index(loc, remote=()):
    """Write DIST's umbra-effects/index.json for the bakes in DIST (merged with remote, the
    bucket's list) and add it to what goes up. Nothing when DIST has no bake manifest."""
    entries = bake_entries(loc)
    if not entries:
        return None
    blob = bake_index(entries, remote)
    path = os.path.join(loc.dist, *BAKE_INDEX.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(blob)
    os.replace(path + ".tmp", path)
    loc.index = Item(BAKE_INDEX, path, len(blob), "bake-index", packdef.sha256_bytes(blob))
    return loc.index


def remote_index(b2, bucket, prefix):
    """The bake list already in the bucket ([] when there is none)."""
    if not b2.download_url:
        raise Refuse("B2 gave no download address, so the bucket's list of bakes can't be read; nothing uploaded")
    url = "%s/file/%s/%s" % (b2.download_url, urllib.parse.quote(bucket), urllib.parse.quote(prefix + BAKE_INDEX))
    req = urllib.request.Request(url, headers={"Authorization": b2.token, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise Refuse("could not read the bucket's list of bakes (%s %s); nothing uploaded" % (e.code, e.reason))
    except OSError as e:
        raise Refuse("could not read the bucket's list of bakes (%s); nothing uploaded" % scrub(str(e)))
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError:
        d = None
    if not isinstance(d, dict) or d.get("kind") != "umbra-effects-bakes" or not isinstance(d.get("bakes"), list):
        raise Refuse("the bucket's %s is not a list of bakes; nothing uploaded" % (prefix + BAKE_INDEX))
    return d["bakes"]


def scan_bakes(dist):
    """--bakes: every prebaked bake in DIST/umbra-effects, with no pack or channel."""
    dist = os.path.abspath(dist)
    bdir = os.path.join(dist, packdef.BAKE_DIR)
    loc = Local(dist)
    if os.path.isdir(bdir):
        for fp in sorted(os.listdir(bdir)):
            if not os.path.isdir(os.path.join(bdir, fp)):
                continue
            for f in sorted(os.listdir(os.path.join(bdir, fp))):
                if not f.endswith(".json") or f.startswith("."):
                    continue
                rel = "%s%s/%s" % (BAKES, fp, f)
                if not _BAKE.match(rel):
                    raise Refuse("%s is not a %s<fingerprint>/<sha256>.json name" % (rel, BAKES))
                blob = _read(os.path.join(bdir, fp, f))
                add_bake(loc, {"path": rel, "sha256": packdef.sha256_bytes(blob), "size": len(blob)}, rel)
    if not loc.bakes:
        raise Refuse("no bakes in %s; build one first (dev/pack_build.py --bake-only)" % bdir)
    missing = sorted(it.rel for it in loc.bakes.values() if not it.local)
    if missing:
        raise Refuse("the bake manifests in %s name %s that %s not there (rebuild the bake):\n    %s"
                     % (bdir, plural(len(missing), "file"), "is" if len(missing) == 1 else "are",
                        "\n    ".join(missing[:20])))
    return loc


def scan(dist, only=None):
    """Read DIST's channels and packs (cheap checks only; hashing is verify())."""
    dist = os.path.abspath(dist)
    cdir = os.path.join(dist, "channels")
    if not os.path.isdir(cdir):
        raise Refuse("%s has no channels/ folder; build the pack first (dev/pack_build.py)" % dist)
    found = sorted(f[:-5] for f in os.listdir(cdir) if f.endswith(".json") and not f.startswith("."))
    names = found
    if only:
        unknown = sorted(set(only) - set(found))
        if unknown:
            raise Refuse("no channel %s in %s (there is: %s)" % (", ".join(unknown), cdir, ", ".join(found) or "none"))
        names = sorted(set(only))
    if not names:
        raise Refuse("no channel files in %s" % cdir)
    loc = Local(dist)
    for c in names:
        rel = "channels/%s.json" % c
        raw = _read(os.path.join(dist, "channels", c + ".json"))
        try:
            ch = packdef.check_channel(packdef.loads(raw, rel))
        except packdef.PackError as e:
            raise Refuse("%s: %s" % (rel, e))
        prel = ch["pack"]
        if not re.fullmatch(r"packs/[A-Za-z0-9._+-]+\.json", prel):
            raise Refuse("%s points at %r, which is not a packs/*.json name" % (rel, prel))
        ppath = os.path.join(dist, *prel.split("/"))
        if not os.path.isfile(ppath):
            raise Refuse("%s points at %s, which is not in %s" % (rel, prel, dist))
        blob = _read(ppath)
        if packdef.sha256_bytes(blob) != ch["sha256"]:
            raise Refuse("%s does not match the sha256 in %s (rebuild the pack)" % (prel, rel))
        if "size" in ch and ch["size"] != len(blob):
            raise Refuse("%s is %d bytes; %s says %s" % (prel, len(blob), rel, ch["size"]))
        try:
            pack = packdef.check_pack(packdef.loads(blob, prel))
        except packdef.PackError as e:
            raise Refuse("%s: %s" % (prel, e))
        if ch.get("version") not in (None, pack["version"]):
            raise Refuse("%s says version %s but %s is %s" % (rel, ch["version"], prel, pack["version"]))
        loc.packs.setdefault(prel, Item(prel, ppath, len(blob), "pack", ch["sha256"], pack["version"]))
        chan = Item(rel, os.path.join(dist, "channels", c + ".json"), len(raw), "channel",
                    packdef.sha256_bytes(raw), pack["version"], channel=c)
        chan.pack = prel
        loc.channels.append(chan)
        for o in object_refs(pack):
            m = _OBJ.match(str(o.get("path", "")))
            if not m or m.group(1) != o.get("sha256") or not isinstance(o.get("size"), int):
                raise Refuse("%s names an object that is not content-addressed: %r" % (prel, o.get("path")))
            it = loc.objects.get(o["path"])
            if it is None:
                loc.objects[o["path"]] = Item(o["path"], os.path.join(dist, *o["path"].split("/")), o["size"],
                                              "object", o["sha256"])
            elif it.size != o["size"]:
                raise Refuse("%s is listed with two different sizes" % o["path"])
        for L in pack["layers"]:
            if L.get("bake"):
                add_bake(loc, L["bake"], prel)
    for it in loc.objects.values():
        if not os.path.isfile(it.path):
            it.local = False
        elif os.path.getsize(it.path) != it.size:
            raise Refuse("%s is %d bytes but the pack says %d (damaged; rebuild the pack)"
                         % (it.rel, os.path.getsize(it.path), it.size))
    cp = os.path.join(dist, "CREDITS.md")
    if os.path.isfile(cp):
        data = _read(cp)
        loc.credits = Item("CREDITS.md", cp, len(data), "credits", packdef.sha256_bytes(data))
    odir = os.path.join(dist, "objects")
    if os.path.isdir(odir):
        loc.unreferenced = sum(1 for f in os.listdir(odir)
                               if _OBJ.match("objects/" + f) and "objects/" + f not in loc.objects)
    return loc


def hash_file(path, part_size=None, stop=None):
    """(size, sha256, sha1, [sha1 of each part_size slice] or None) in one read."""
    h256, h1 = hashlib.sha256(), hashlib.sha1()
    parts, ph, left = [], (hashlib.sha1() if part_size else None), part_size
    n = 0
    with open(path, "rb") as f:
        while True:
            if stop is not None and stop.is_set():
                raise Stopped()
            b = f.read(CHUNK)
            if not b:
                break
            n += len(b)
            h256.update(b)
            h1.update(b)
            if ph is not None:
                mv = memoryview(b)
                while len(mv):
                    take = min(left, len(mv))
                    ph.update(mv[:take])
                    mv = mv[take:]
                    left -= take
                    if left == 0:
                        parts.append(ph.hexdigest())
                        ph, left = hashlib.sha1(), part_size
    if ph is not None and left != part_size:
        parts.append(ph.hexdigest())
    return n, h256.hexdigest(), h1.hexdigest(), (parts if part_size else None)


def assign_parts(items, part_size, threshold):
    """Mark the files that go up as B2 large files (always 2+ parts, at most 10,000)."""
    limit = max(min(threshold, MAX_SMALL), part_size)
    for it in items:
        if it.size > limit:
            it.part_size = max(part_size, -(-it.size // MAX_PARTS))
        else:
            it.part_size = None


def verify(items, threads, stop, parts=True):
    """Hash every local file to upload against its content address; fills in the SHA1s B2 checks."""
    items = [it for it in items if it.local]
    if not items:
        return
    say("Checking %s (%s) against their content addresses ..." % (plural(len(items), "file"),
                                                                    human(sum(i.size for i in items))))

    def one(it):
        try:
            n, s256, s1, ps = hash_file(it.path, it.part_size if parts else None, stop)
        except OSError as e:
            return "%s: cannot read it (%s)" % (it.rel, e)
        if n != it.size:
            return "%s: %d bytes, expected %d" % (it.rel, n, it.size)
        if s256 != it.sha256:
            return "%s: its sha256 is %s..., expected %s..." % (it.rel, s256[:16], it.sha256[:16])
        it.sha1, it.part_sha1s = s1, ps
        return None

    with cf.ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        bad = [r for r in ex.map(one, items) if r]
    if bad:
        raise Refuse("%s not match their content address (damaged, or changed since the build; rebuild the "
                     "pack):\n    %s" % ("1 file does" if len(bad) == 1 else "%d files do" % len(bad),
                                        "\n    ".join(bad[:20])))


# ---------------------------------------------------------------- B2's native API

def _http_error(api, e):
    try:
        raw = e.read()
    except Exception:  # noqa: BLE001 - the error body is only a nicety
        raw = b""
    finally:
        e.close()
    code, msg = "http_%d" % e.code, ""
    try:
        d = json.loads(raw)
        code, msg = d.get("code") or code, d.get("message") or ""
    except (ValueError, AttributeError):
        pass
    try:
        ra = float(e.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        ra = None
    return B2Error(api, e.code, code, msg, ra)


def _kind(e):
    """'auth' (sign in again / new upload URL), 'retry' (back off and retry) or None (give up)."""
    if isinstance(e, B2Error):
        if e.status == 401 and e.code in ("expired_auth_token", "bad_auth_token"):
            return "auth"
        if e.status in RETRY_STATUS or e.status == 0:
            return "retry"
        return None
    if isinstance(e, NET_ERRORS):
        return "retry"
    return None


def _as_b2(e, api):
    if isinstance(e, NET_ERRORS) and not isinstance(e, B2Error):
        return B2Error(api, 0, "network", "%s: %s" % (type(e).__name__, e))
    return e


def _short(e):
    if isinstance(e, B2Error):
        return ("HTTP %d %s" % (e.status, e.code)) if e.status else e.code
    return "%s %s" % (type(e).__name__, str(e)[:80])


class _Slice:
    """A file-like view of [offset, offset+length) of a file, for the request body; counts progress."""

    def __init__(self, path, offset, length, progress, stop):
        try:
            self.f = open(path, "rb")
        except OSError as e:
            raise Refuse("cannot read %s (%s)" % (path, e)) from None
        self.f.seek(offset)
        self.path, self.left, self.sent, self.progress, self.stop = path, length, 0, progress, stop

    def read(self, n=-1):
        if self.stop.is_set():
            raise Stopped()
        if self.left <= 0:
            return b""
        if n is None or n < 0 or n > self.left:
            n = self.left
        b = self.f.read(n)
        if not b:
            raise Refuse("%s got shorter while it was being uploaded" % self.path)
        self.left -= len(b)
        self.sent += len(b)
        if self.progress:
            self.progress.add(len(b))
        return b

    def undo(self):
        if self.progress:
            self.progress.add(-self.sent)
        self.sent = 0

    def close(self):
        self.f.close()


class _UrlPool:
    """Upload URLs: B2 wants each used by one thread at a time; a failed one is dropped, not returned."""

    def __init__(self, get):
        self._get, self._free, self._lock = get, [], threading.Lock()

    def take(self):
        with self._lock:
            if self._free:
                return self._free.pop()
        u = self._get()
        add_secret(u.get("authorizationToken"))
        return u

    def give(self, u):
        with self._lock:
            self._free.append(u)


def allowed_of(info, d):
    """What the key may do. B2's v3 reply (as seen 2026-11-14) puts bucketId,
    bucketName, capabilities and namePrefix straight into apiInfo.storageApi;
    the docs (and v2) nest them under "allowed". Both are read."""
    a = info.get("allowed") or d.get("allowed")
    if a:
        return a
    a = {k: info[k] for k in ("capabilities", "namePrefix", "buckets") if info.get(k) is not None}
    if info.get("bucketId"):
        a["bucketId"], a["bucketName"] = info["bucketId"], info.get("bucketName")
    return a


class B2:
    def __init__(self, key_id, app_key, api_url=API_URL):
        b64 = base64.b64encode(("%s:%s" % (key_id, app_key)).encode("utf-8")).decode("ascii")
        for s in (key_id, app_key, b64):
            add_secret(s)
        self._basic = "Basic " + b64
        self._auth_url = api_url.rstrip("/") + API + "b2_authorize_account"
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self.token = None
        self.generation = 0
        self.sign_ins = 0
        self.account_id = self.api_url = self.download_url = None
        self.recommended_part, self.min_part = DEFAULT_PART, 5_000_000
        self.allowed = {}

    def _send(self, api, url, data=None, headers=None, method="POST"):
        h = {"User-Agent": USER_AGENT}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise _http_error(api, e) from None
        try:
            return json.loads(raw)
        except ValueError:
            raise B2Error(api, 0, "bad_response", "the reply was not JSON") from None

    def _wait(self, attempt, err, what):
        delay = min(BACKOFF_CAP, BACKOFF * (2 ** attempt)) * (0.5 + random.random() / 2)
        if getattr(err, "retry_after", None):
            delay = max(delay, min(err.retry_after, 300.0))
        say("  %s: %s; trying again in %.1f s" % (what, _short(err), delay))
        if self.stop.wait(delay):
            raise Stopped()

    def authorize(self):
        attempt = 0
        while True:
            try:
                d = self._send("b2_authorize_account", self._auth_url, headers={"Authorization": self._basic},
                               method="GET")
                break
            except Exception as e:  # noqa: BLE001
                if isinstance(e, B2Error) and e.status in (400, 401, 403):
                    raise Refuse("B2 did not accept the key in B2_KEY_ID / B2_APP_KEY (%s). Check both, and that "
                                 "the key has not been deleted or expired." % e.code) from None
                if _kind(e) and attempt < RETRIES:
                    self._wait(attempt, e, "signing in")
                    attempt += 1
                    continue
                raise _as_b2(e, "b2_authorize_account")
        info = (d.get("apiInfo") or {}).get("storageApi") or d        # v3 nests it; v2 had it at the top
        self.token = add_secret(d["authorizationToken"])
        self.account_id = d.get("accountId")
        self.api_url = info["apiUrl"].rstrip("/")
        self.download_url = (info.get("downloadUrl") or "").rstrip("/")
        self.recommended_part = int(info.get("recommendedPartSize") or DEFAULT_PART)
        self.min_part = int(info.get("absoluteMinimumPartSize") or 5_000_000)
        self.allowed = allowed_of(info, d)
        self.generation += 1
        self.sign_ins += 1

    def _sign_in_again(self, gen):
        with self._lock:
            if self.generation == gen:            # not already done by another thread
                say("  B2 says the session expired; signing in again")
                self.authorize()

    def call(self, api, body):
        """A JSON API call, retried with back-off; an expired session signs in again."""
        attempt = renewals = 0
        while True:
            gen, token = self.generation, self.token
            try:
                return self._send(api, self.api_url + API + api, json.dumps(body).encode("utf-8"),
                                  {"Authorization": token, "Content-Type": "application/json"})
            except Exception as e:  # noqa: BLE001
                kind = _kind(e)
                if kind == "auth" and renewals < 3:
                    renewals += 1
                    self._sign_in_again(gen)
                    continue
                if kind and attempt < RETRIES:
                    self._wait(attempt, e, api)
                    attempt += 1
                    continue
                raise _as_b2(e, api)

    def upload(self, api, pool, headers, path, offset, length, progress):
        """b2_upload_file / b2_upload_part: after any failure the URL is dropped and a fresh one fetched."""
        attempt = renewals = 0
        while True:
            if self.stop.is_set():
                raise Stopped()
            u = pool.take()
            h = dict(headers)
            h["Authorization"] = u["authorizationToken"]
            h["Content-Length"] = str(length)
            body = _Slice(path, offset, length, progress, self.stop)
            try:
                res = self._send(api, u["uploadUrl"], body, h)
            except Exception as e:  # noqa: BLE001
                body.undo()
                kind = _kind(e)
                if kind == "auth" and renewals < 5:       # the upload URL's token expired: just get a new URL
                    renewals += 1
                    continue
                if kind and attempt < RETRIES:
                    self._wait(attempt, e, api)
                    attempt += 1
                    continue
                raise _as_b2(e, api)
            finally:
                body.close()
            pool.give(u)
            return res

    # -- lookups
    def find_bucket(self, name):
        """(bucketId, bucketType or None)."""
        a = self.allowed or {}
        buckets = a.get("buckets")
        if buckets is None and a.get("bucketId"):
            buckets = [{"id": a["bucketId"], "name": a.get("bucketName")}]
        if buckets:
            for b in buckets:
                if b.get("name") == name:
                    return b["id"], None
            raise Refuse("this key only works on bucket %s, not %r"
                         % (", ".join(repr(b.get("name")) for b in buckets), name))
        d = self.call("b2_list_buckets", {"accountId": self.account_id, "bucketName": name})
        for b in d.get("buckets", []):
            if b.get("bucketName") == name:
                return b["bucketId"], b.get("bucketType")
        raise Refuse("there is no bucket named %r in this B2 account" % name)

    def list_file_names(self, bucket_id, prefix):
        start = None
        while True:
            body = {"bucketId": bucket_id, "prefix": prefix, "maxFileCount": 1000}
            if start:
                body["startFileName"] = start
            d = self.call("b2_list_file_names", body)
            yield from d.get("files", [])
            start = d.get("nextFileName")
            if not start:
                return

    def list_unfinished(self, bucket_id, prefix):
        start = None
        while True:
            body = {"bucketId": bucket_id, "maxFileCount": 100}
            if prefix:
                body["namePrefix"] = prefix
            if start:
                body["startFileId"] = start
            d = self.call("b2_list_unfinished_large_files", body)
            yield from d.get("files", [])
            start = d.get("nextFileId")
            if not start:
                return

    def list_parts(self, file_id):
        start = None
        while True:
            body = {"fileId": file_id, "maxPartCount": 1000}
            if start:
                body["startPartNumber"] = start
            d = self.call("b2_list_parts", body)
            yield from d.get("parts", [])
            start = d.get("nextPartNumber")
            if not start:
                return


def info_get(f, key):
    for k, v in ((f or {}).get("fileInfo") or {}).items():
        if k.lower() == key:
            return v
    return None


def same(it, f):
    """Is the B2 file f this item (size and the sha256 we stored in its file info)?"""
    return (f is not None and f.get("action", "upload") == "upload"
            and int(f.get("contentLength", -1)) == it.size and info_get(f, "sha256") == it.sha256)


def version_older(a, b):
    """a < b, when both are dotted numbers; None when that can't be told."""
    try:
        return tuple(int(x) for x in a.split(".")) < tuple(int(x) for x in b.split("."))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- publishing

class Progress:
    def __init__(self, total):
        self.total, self.done = max(1, total), 0
        self.t0 = time.monotonic()
        self.lock = threading.Lock()

    def add(self, n):
        with self.lock:
            self.done += n

    def status(self):
        with self.lock:
            done = self.done
        s = "[%3d%%]" % min(100, 100 * done // self.total)
        el = time.monotonic() - self.t0
        if el > 5 and 0 < done < self.total:
            rate = done / el
            s += " %s/s, ~%s left," % (human(rate), eta((self.total - done) / rate))
        return s


class Publisher:
    def __init__(self, b2, bucket_id, prefix, threads):
        self.b2, self.bucket_id, self.prefix, self.threads = b2, bucket_id, prefix, max(1, threads)
        self.files = _UrlPool(lambda: b2.call("b2_get_upload_url", {"bucketId": bucket_id}))
        self.progress = Progress(0)
        self.phase = None

    def name(self, it):
        return self.prefix + it.rel

    def listing(self, subs):
        out = {}
        for s in subs:
            for f in self.b2.list_file_names(self.bucket_id, self.prefix + s):
                if f.get("action", "upload") == "upload":
                    out[f["fileName"]] = f
        return out

    def present(self, it, remote):
        return same(it, remote.get(self.name(it)))

    def resolve_resume(self, large):
        """Match unfinished B2 large files to what is about to go up: resume the matching one, cancel the rest."""
        if not large:
            return
        by_name = {}
        for f in self.b2.list_unfinished(self.bucket_id, self.prefix):
            by_name.setdefault(f["fileName"], []).append(f)
        for it in large:
            for f in by_name.get(self.name(it), []):
                if it.resume_id is None and info_get(f, "sha256") == it.sha256:
                    layout = it.layout()
                    parts = {int(p["partNumber"]): p for p in self.b2.list_parts(f["fileId"])}
                    if all(1 <= n <= len(layout) and int(p.get("contentLength", -1)) == layout[n - 1][2]
                           and p.get("contentSha1") == it.part_sha1s[n - 1] for n, p in parts.items()):
                        it.resume_id, it.have_parts = f["fileId"], set(parts)
                        continue
                it.cancel_ids.append(f["fileId"])

    def pool(self, jobs, fn):
        if not jobs:
            return
        if self.threads == 1 or len(jobs) == 1:
            for j in jobs:
                fn(j)
            return
        ex = cf.ThreadPoolExecutor(max_workers=min(self.threads, len(jobs)))
        futs = [ex.submit(fn, j) for j in jobs]
        try:
            for f in cf.as_completed(futs):
                f.result()
        except BaseException:
            self.b2.stop.set()
            raise
        finally:
            ex.shutdown(wait=True, cancel_futures=True)

    def must_be_there(self, items, sub, before):
        remote = self.listing([sub])
        missing = [it.rel for it in items if not self.present(it, remote)]
        if missing:
            raise Refuse("before uploading %s, B2 should have every file they need, but it is missing %s: %s"
                         % (before, plural(len(missing), "file"), ", ".join(missing[:5])))

    def publish(self, items, loc):
        self.progress = Progress(sum(it.to_send() for it in items))
        for phase in PHASES:
            todo = [it for it in items if it.phase == phase]
            vols = [it for it in loc.bakes.values() if it.kind == "bake-volume"]
            mans = [it for it in loc.bakes.values() if it.kind == "bake"]
            if phase == "bakes" and todo and vols:
                self.must_be_there(vols, BAKES, "the bake manifests")
            if phase == "index" and todo and mans:
                self.must_be_there(mans, BAKES, "the list of bakes")
            if phase == "packs" and (todo or any(it.phase == "channels" for it in items)):
                self.must_be_there(list(loc.objects.values()), "objects/", "the packs")
                if mans:
                    self.must_be_there(mans, BAKES, "the packs")
            if phase == "channels" and todo:
                self.must_be_there(list(loc.packs.values()), "packs/", "the channels")
            if not todo:
                continue
            self.phase = phase
            say("Uploading %s: %s, %s" % (phase, plural(len(todo), "file"), human(sum(i.to_send() for i in todo))))
            self.pool(sorted((i for i in todo if not i.part_size), key=lambda i: i.rel), self.upload_small)
            for it in sorted((i for i in todo if i.part_size), key=lambda i: i.rel):
                self.upload_large(it)
        self.phase = None

    def upload_small(self, it):
        headers = {"X-Bz-File-Name": urllib.parse.quote(self.name(it), safe="/"),
                   "Content-Type": it.content_type(), "X-Bz-Content-Sha1": it.sha1}
        for k, v in it.info().items():
            headers["X-Bz-Info-" + k] = urllib.parse.quote(str(v), safe="")
        res = self.b2.upload("b2_upload_file", self.files, headers, it.path, 0, it.size, self.progress)
        if str(res.get("contentSha1", "")).lower() != it.sha1 or int(res.get("contentLength", -1)) != it.size:
            raise B2Error("b2_upload_file", 0, "mismatch", "B2 stored %s with another size or SHA1" % self.name(it))
        say("  %s %s  %s" % (self.progress.status(), it.short(), human(it.size)))

    def upload_large(self, it):
        name = self.name(it)
        for fid in it.cancel_ids:
            try:
                self.b2.call("b2_cancel_large_file", {"fileId": fid})
                say("  cancelled an unfinished upload of %s that does not match this file's parts" % it.short())
            except B2Error as e:        # only costs storage until B2's lifecycle rules or a later run clear it
                say("  could not cancel an old unfinished upload of %s (%s); carrying on" % (it.short(), _short(e)))
        layout = it.layout()
        if it.resume_id:
            fid = it.resume_id
            say("  %s: resuming, %d of %d parts already in B2" % (it.short(), len(it.have_parts), len(layout)))
        else:
            info = dict(it.info())
            info["large_file_sha1"] = it.sha1
            fid = self.b2.call("b2_start_large_file", {"bucketId": self.bucket_id, "fileName": name,
                                                        "contentType": it.content_type(), "fileInfo": info})["fileId"]
            say("  %s: %s as a large file, %d parts of %s" % (it.short(), human(it.size), len(layout),
                                                               human(it.part_size)))
        pool = _UrlPool(lambda: self.b2.call("b2_get_upload_part_url", {"fileId": fid}))

        def part(p):
            n, off, ln = p
            sha1 = it.part_sha1s[n - 1]
            res = self.b2.upload("b2_upload_part", pool, {"X-Bz-Part-Number": str(n), "X-Bz-Content-Sha1": sha1,
                                                          "Content-Type": "application/octet-stream"},
                                 it.path, off, ln, self.progress)
            if res.get("contentSha1") != sha1 or int(res.get("contentLength", -1)) != ln:
                raise B2Error("b2_upload_part", 0, "mismatch", "B2 stored part %d of %s differently" % (n, name))
            say("    %s part %d/%d of %s" % (self.progress.status(), n, len(layout), it.short()))

        self.pool([p for p in layout if p[0] not in it.have_parts], part)
        try:
            res = self.b2.call("b2_finish_large_file", {"fileId": fid, "partSha1Array": it.part_sha1s})
        except B2Error:
            # a finish that worked but whose reply was lost, then retried, is refused: fine if the file is there
            if not self.present(it, self.listing([it.rel])):
                raise
            res = {"contentLength": it.size}
        if int(res.get("contentLength", -1)) != it.size:
            raise B2Error("b2_finish_large_file", 0, "mismatch", "B2 put %s together with the wrong size" % name)
        say("  %s %s  %s  done" % (self.progress.status(), it.short(), human(it.size)))


# ---------------------------------------------------------------- the public base URL check

def http_get(url, headers=None, tries=4):
    """(status, body, headers) of a plain public GET; no B2 credentials are ever sent here."""
    last = None
    for attempt in range(tries):
        h = {"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
        h.update(headers or {})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:  # noqa: BLE001
                body = b""
            finally:
                e.close()
            if e.code not in RETRY_STATUS or attempt + 1 == tries:
                return e.code, body, e.headers
            last = e
        except NET_ERRORS as e:
            last = e
        time.sleep(min(8.0, BACKOFF * 2 ** attempt))
    return 0, b"", {"error": str(last)}


def check_base_url(base, loc):
    """What players download from the base URL is what was just published."""
    base = base.rstrip("/")
    say("Checking what players get from %s ..." % base)
    ok = True
    for it in loc.channels:
        st, body, hdr = http_get(base + "/" + it.rel)
        local = _read(it.path)
        if st != 200:
            say("  FAIL %s: HTTP %s %s" % (it.rel, st, hdr.get("error", "") if st == 0 else ""))
            ok = False
            continue
        if body != local:
            try:
                served = "version %s" % json.loads(body).get("version")
            except (ValueError, AttributeError):
                served = "something else"
            say("  FAIL %s: the base URL serves %s, not the %s just published. A CDN in front may still have the "
                "old copy (channels are cached up to 5 minutes); run again later to check." % (it.rel, served,
                                                                                           it.version))
            ok = False
            continue
        say("  ok   %s: version %s (Cache-Control: %s)" % (it.rel, it.version, hdr.get("Cache-Control") or "none"))
        ch = json.loads(local)
        st, blob, hdr = http_get(base + "/" + ch["pack"])
        if st == 200 and packdef.sha256_bytes(blob) == ch["sha256"]:
            say("  ok   %s matches its channel's sha256" % ch["pack"])
        else:
            say("  FAIL %s: HTTP %s, %s" % (ch["pack"], st, "wrong sha256" if st == 200 else "not served"))
            ok = False
    for it in sorted((b for b in loc.bakes.values() if b.kind == "bake" and b.local), key=lambda b: b.rel):
        st, blob, hdr = http_get(base + "/" + it.rel)
        if st == 200 and packdef.sha256_bytes(blob) == it.sha256:
            say("  ok   %s (Cache-Control: %s)" % (it.short(), hdr.get("Cache-Control") or "none"))
        else:
            say("  FAIL %s: HTTP %s, %s" % (it.rel, st, "wrong sha256" if st == 200 else "not served"))
            ok = False
    # a Range request on a small object: the installer resumes broken downloads with these
    objs = sorted((o for o in list(loc.objects.values()) + [b for b in loc.bakes.values() if b.kind == "bake-volume"]
                   if o.local and o.size >= 2), key=lambda o: o.size)
    if objs:
        o = objs[0]
        n = min(16, o.size)
        with open(o.path, "rb") as f:
            head = f.read(n)
        st, body, hdr = http_get(base + "/" + o.rel, {"Range": "bytes=0-%d" % (n - 1)})
        if st == 206 and body == head:
            say("  ok   %s: partial downloads work (Cache-Control: %s)" % (o.short(), hdr.get("Cache-Control")
                                                                         or "none"))
        elif st == 200 and body[:n] == head:
            say("  NOTE %s: the base URL ignores Range requests, so a broken download starts over" % o.short())
        else:
            say("  FAIL %s: HTTP %s" % (o.short(), st))
            ok = False
    return ok


# ---------------------------------------------------------------- the command

def print_plan(items, prefix, remote_note=True):
    total = 0
    for i, phase in enumerate(PHASES, 1):
        ph = [it for it in items if it.phase == phase]
        if not ph:
            continue
        nb = sum(it.to_send() for it in ph)
        total += nb
        say("  %d. %-8s %s, %s" % (i, phase, plural(len(ph), "file"), human(nb)))
        for it in ph:
            extra = []
            if it.part_size:
                extra.append("large file, %d parts of %s" % (len(it.layout()), human(it.part_size)))
                if it.have_parts:
                    extra.append("%d already in B2, resumes" % len(it.have_parts))
            if it.note and remote_note:
                extra.append(it.note)
            elif it.kind == "channel":
                extra.append("version %s" % it.version)
            say("       %s%s  %s%s" % (prefix, it.rel, human(it.size), ("  (%s)" % "; ".join(extra)) if extra else ""))
    say("Total: %s, %s (%s bytes)" % (plural(len(items), "file"), human(total), format(total, ",")))
    return total


def _stdin_is_tty():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def confirm(question):
    if not _stdin_is_tty():
        say("Not uploading: there is no terminal to ask on. Pass --yes to upload.")
        return False
    try:
        ans = input(question + " [y/N] ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def norm_prefix(p):
    p = (p or "").strip().strip("/")
    if not p:
        return ""
    if "\\" in p or any(s in ("", ".", "..") for s in p.split("/")):
        raise Refuse("--prefix %r is not a plain folder name" % p)
    return p + "/"


def check_api_url(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")):
        return url
    raise Refuse("--api-url must be https (plain http only for a local test server)")


def parser():
    ap = argparse.ArgumentParser(prog="b2_upload.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", metavar="NAME", help="the B2 bucket to publish to")
    ap.add_argument("--dist", metavar="DIR", default=os.path.join(ROOT, "dist"),
                    help="the built pack (default: %(default)s)")
    ap.add_argument("--channel", metavar="NAME", action="append",
                    help="publish only this channel and what it needs (repeatable; default: every channel in "
                         "DIST/channels)")
    ap.add_argument("--bakes", action="store_true",
                    help="publish only the prebaked bakes in DIST/umbra-effects (Umbra Gore & Effects' body models, "
                         "dev/pack_build.py --bake-only); no pack, no channel changes")
    ap.add_argument("--prefix", metavar="FOLDER", default="",
                    help="publish under this folder of the bucket (default: the bucket root); the base URL then "
                         "ends in it")
    ap.add_argument("--dry-run", action="store_true",
                    help="check the files and list what would go up, with the total size; no network at all")
    ap.add_argument("--yes", action="store_true", help="upload without asking (needed without a terminal)")
    ap.add_argument("--base-url-check", metavar="URL",
                    help="afterwards, download the channel through this public base URL (B2's "
                         "https://fNNN.backblazeb2.com/file/BUCKET, or a CDN in front of it) and check it is the "
                         "one just published, its pack matches, and Range requests work")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="let a channel move to an older version than the one in the bucket")
    ap.add_argument("--threads", type=int, default=4, metavar="N",
                    help="parallel uploads and parts (default: %(default)s)")
    ap.add_argument("--part-size", type=parse_size, metavar="SIZE",
                    help="large-file part size, e.g. 100MB (default: what B2 recommends, usually 100 MB)")
    ap.add_argument("--large-threshold", type=parse_size, metavar="SIZE", default=DEFAULT_LARGE,
                    help="files bigger than this go up as large files in parts (default: 200MB)")
    ap.add_argument("--api-url", default=API_URL, help=argparse.SUPPRESS)      # tests: a local fake B2
    return ap


def dry_run(a, loc, prefix):
    say("Dry run: nothing is sent, and B2 is not asked what it already has, so this is everything the "
        "selected %s need." % ("bakes" if a.bakes else "channels"))
    for it in loc.channels:
        say("  channel %s -> %s (version %s)" % (it.channel, it.pack, it.version))
    for it in sorted((b for b in loc.bakes.values() if b.kind == "bake"), key=lambda b: b.rel):
        say("  bake %s" % it.rel)
    if add_index(loc):
        say("  %s: the bakes in %s (a real run merges in the bucket's own list)" % (BAKE_INDEX, loc.dist))
    items = [it for it in loc.items() if it.local]
    assign_parts(items, a.part_size or DEFAULT_PART, a.large_threshold)
    verify(items, a.threads, threading.Event(), parts=False)
    say("Would upload, in this order%s:" % ((" under %s" % prefix) if prefix else ""))
    print_plan(items, prefix, remote_note=False)
    if loc.unreferenced:
        say("(%s in %s/objects not named by these packs; not uploaded)"
            % (plural(loc.unreferenced, "older file"), loc.dist))
    if a.base_url_check:
        say("(--base-url-check is skipped in a dry run)")
    missing = sorted(it.rel for it in list(loc.objects.values()) + list(loc.bakes.values()) if not it.local)
    if missing:
        say("MISSING: %s the packs need %s not in %s. A real run accepts them only if the bucket already "
            "has them; a dry run can't ask:\n    %s" % (plural(len(missing), "file"),
                                                        "is" if len(missing) == 1 else "are", loc.dist,
                                                        "\n    ".join(missing[:20])))
        return 1
    return 0


def run(a):
    prefix = norm_prefix(a.prefix)
    if a.threads < 1:
        raise Refuse("--threads must be 1 or more")
    if a.bakes and a.channel:
        raise Refuse("--bakes publishes no channel; leave out --channel")
    loc = scan_bakes(a.dist) if a.bakes else scan(a.dist, a.channel)
    if a.dry_run:
        return dry_run(a, loc, prefix)
    key_id = os.environ.get("B2_KEY_ID", "").strip()
    app_key = os.environ.get("B2_APP_KEY", "").strip()
    if not key_id or not app_key:
        say("Set B2_KEY_ID and B2_APP_KEY in the environment (a B2 application key; it is never taken from the "
            "command line).", err=True)
        return 2
    b2 = B2(key_id, app_key, check_api_url(a.api_url))
    say("Signing in to B2 ...")
    b2.authorize()
    bucket_id, btype = b2.find_bucket(a.bucket)
    allowed_prefix = (b2.allowed or {}).get("namePrefix")
    if allowed_prefix and not prefix.startswith(allowed_prefix):
        raise Refuse("this key may only write names starting with %r; use --prefix" % allowed_prefix)
    pub = Publisher(b2, bucket_id, prefix, a.threads)
    remote = pub.listing([BAKES] if a.bakes else ["objects/", BAKES, "packs/", "channels/", "CREDITS.md"])
    if bake_entries(loc):
        add_index(loc, remote_index(b2, a.bucket, prefix))

    missing = sorted(it.rel for it in list(loc.objects.values()) + list(loc.bakes.values())
                     if not it.local and not pub.present(it, remote))
    if missing:
        raise Refuse("%s the packs need %s neither in %s nor in the bucket:\n    %s"
                     % (plural(len(missing), "file"), "is" if len(missing) == 1 else "are", loc.dist,
                        "\n    ".join(missing[:20])))
    items = loc.items()
    there = [it for it in items if pub.present(it, remote)]
    todo = [it for it in items if it.local and not pub.present(it, remote)]
    for it in todo:
        f = remote.get(pub.name(it))
        if it.kind == "channel":
            was = info_get(f, "version") if f else None
            if f is None:
                it.note = "new channel, version %s" % it.version
            elif was and was != it.version:
                it.note = "version %s -> %s" % (was, it.version)
                if version_older(it.version, was) and not a.allow_downgrade:
                    raise Refuse("channel %s is at version %s in the bucket; this would move it back to %s. Pass "
                                 "--allow-downgrade if that is what you want." % (it.channel, was, it.version))
            else:
                it.note = "version %s, a different build replaces the one in the bucket" % it.version
        elif f is not None:
            it.note = "replaces a different file with this name"
            if it.kind == "pack":
                it.note += " (bump the version for releases; players mid-install may have to retry)"

    say("Bucket %s%s: %s already there (%s), skipped." % (a.bucket, ("/" + prefix) if prefix else "",
                                                          plural(len(there), "file"),
                                                          human(sum(i.size for i in there))))
    if loc.unreferenced:
        say("(%s in %s/objects not named by these packs; not uploaded)"
            % (plural(loc.unreferenced, "older file"), loc.dist))
    if btype and btype != "allPublic":
        say("NOTE: bucket %s is %s; players can't download from B2's own address, only through something that "
            "signs requests." % (a.bucket, btype))
    part = a.part_size or b2.recommended_part
    if part < b2.min_part:
        say("NOTE: B2's smallest part is %s; using that instead of %s." % (human(b2.min_part), human(part)))
        part = b2.min_part
    assign_parts(todo, part, a.large_threshold)
    verify(todo, a.threads, b2.stop)
    pub.resolve_resume([it for it in todo if it.part_size])
    if not todo:
        say("Everything is already in the bucket; nothing to upload.")
    else:
        say("To upload, in this order:")
        total = print_plan(todo, prefix)
        if not a.yes and not confirm("Upload %s (%s) to B2 bucket %s? Channels go last."
                                     % (plural(len(todo), "file"), human(total), a.bucket)):
            say("Nothing uploaded.")
            return 1
        t0 = time.monotonic()
        try:
            pub.publish(todo, loc)
        except (B2Error, Refuse, Stopped, KeyboardInterrupt):
            b2.stop.set()
            if pub.phase == "channels":
                say("Stopped while uploading channels: some may already point at the new pack (everything they "
                    "need is uploaded). Run again to finish.", err=True)
            else:
                say("Stopped while uploading %s; no channel was changed. Run again to carry on (finished files are "
                    "skipped, cut-off large files resume)." % (pub.phase or "the plan"), err=True)
            raise
        say("Published %s in %s." % (plural(len(todo), "file"), eta(time.monotonic() - t0)))
    if b2.download_url:
        say("B2's own address for this pack (public buckets): %s/file/%s%s"
            % (b2.download_url, a.bucket, ("/" + prefix.rstrip("/")) if prefix else ""))
    if a.base_url_check and not check_base_url(a.base_url_check, loc):
        return 3
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = parser()
    if not argv:
        ap.print_help()
        return 0
    a = ap.parse_args(argv)
    if not a.dry_run and not a.bucket:
        ap.print_usage(sys.stderr)
        say("--bucket NAME is needed to upload (or --dry-run to only look).", err=True)
        return 2
    try:
        return run(a)
    except Refuse as e:
        say("Not uploading: %s" % e, err=True)
        return 1
    except B2Error as e:
        say("B2 error: %s" % e, err=True)
        return 1
    except (KeyboardInterrupt, Stopped):
        say("Stopped.", err=True)
        return 130
    except Exception:  # noqa: BLE001 - print it scrubbed rather than as a raw traceback
        say(traceback.format_exc(), err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
