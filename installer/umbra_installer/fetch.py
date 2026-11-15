"""
Downloads: the channel and pack from the base URL, then every object, into
the cache (kept, so a repair or a second install needs nothing new).

Each object is checked against its sha256; a broken download is resumed with
an HTTP Range request, and a file that fails its check is fetched again from
scratch once before giving up. The base URL may also be a local folder (for
testing, or an offline copy of a published pack).

An author's own download (an "origin" volume, see packdef) never comes from
the base URL: the Fetcher takes it from the player's Downloads folder when it
is there already, or downloads it from the author's page as a browser would,
and raises OriginNeeded (with the page to open) when the site won't let it.
"""
import concurrent.futures as cf
import html
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import packdef

USER_AGENT = "UmbraInstaller"
ORIGIN_AGENT = "Mozilla/5.0 (%s) UmbraSetup" % ("Windows NT 10.0; Win64; x64" if os.name == "nt"
                                                 else "X11; Linux x86_64")
CHUNK = 1 << 20
_MIRROR = re.compile(r'href="([^"]*/mirror/[^"]+)"')


class Cancelled(Exception):
    pass


class FetchError(Exception):
    pass


class OriginNeeded(FetchError):
    """An author's own download that Umbra couldn't get by itself: the player downloads it
    from the page (name, page, site, folder: where Setup looks for it)."""

    def __init__(self, obj, why):
        og = obj["origin"]
        self.obj, self.download = obj, og["download"]
        self.name, self.page, self.site, self.why = og["name"], og["page"], og.get("site") or "its author's page", why
        dirs = downloads_dirs()
        self.folder = dirs[0] if dirs else None
        super().__init__(
            '%s did not let Umbra download "%s" by itself (%s). Its author asks that it be downloaded from %s: '
            'open %s, press Download there, and save the file in your Downloads folder%s. Then press Try again: '
            'Setup finds it there.' % (self.site, self.name, why, self.site, self.page,
                                       " (%s)" % self.folder if self.folder else ""))


def _known_downloads():
    """Windows' own Downloads folder (wherever the player moved it)."""
    try:
        import ctypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                        ("d", ctypes.c_ubyte * 8)]
        g = GUID.from_buffer_copy(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001 - not Windows, or no shell32: the usual place
        return None


def downloads_dirs():
    """Where the player's browser saves downloads (existing folders, best first)."""
    home = os.path.expanduser("~")
    cands = []
    if os.name == "nt":
        cands.append(_known_downloads())
    else:
        cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "user-dirs.dirs")
        try:
            with open(cfg, encoding="utf-8") as f:
                m = re.search(r'^XDG_DOWNLOAD_DIR="([^"]+)"', f.read(), re.M)
            if m:
                cands.append(m.group(1).replace("$HOME", home))
        except OSError:
            pass
    cands.append(os.path.join(home, "Downloads"))
    out, seen = [], set()
    for c in cands:
        if c and os.path.isdir(c) and os.path.realpath(c) not in seen:
            seen.add(os.path.realpath(c))
            out.append(c)
    return out


def find_downloaded(obj, dirs, cancel=None):
    """The author's file already on this computer (in one of dirs, under its own name or a
    browser's "name (1).zip"), checked against its sha256; None if it isn't there."""
    name = obj["origin"]["name"]
    stem, ext = os.path.splitext(name)
    again = re.compile(r"^%s ?\(\d+\)%s$" % (re.escape(stem), re.escape(ext)), re.I)
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in [n for n in names if n.lower() == name.lower()] + [n for n in names if again.match(n)]:
            p = os.path.join(d, fn)
            try:
                if os.path.isfile(p) and os.path.getsize(p) == obj["size"] and \
                        packdef.sha256_file(p, cancel) == obj["sha256"]:
                    return p
            except OSError:
                continue
    return None


def wait_for(obj, cancel, dirs=None, every=2.0):
    """Wait for the author's file to turn up (the player's browser is downloading it): checks
    dirs (a list, or a function giving one; the Downloads folder when None) every few seconds.
    Returns its path; raises Cancelled. Browsers write to a temporary name (.part,
    .crdownload) and rename the file when it is whole, and the size is checked before the
    sha256, so a file still arriving costs nothing."""
    while True:
        if cancel():
            raise Cancelled()
        try:
            p = find_downloaded(obj, (dirs() if callable(dirs) else dirs) or downloads_dirs(), cancel)
        except KeyboardInterrupt:           # sha256_file's way of stopping when asked
            raise Cancelled() from None
        if p:
            return p
        for _ in range(max(1, int(every * 10))):
            if cancel():
                raise Cancelled()
            time.sleep(0.1)


def open_origin(obj, have=0, timeout=60):
    """A response with the author's file (from byte `have`), found the way a browser finds it:
    the download link, and when that gives a page, the mirror link on that page (ModDB's
    "download starts shortly" page). Raises OriginNeeded when the site won't give it."""
    og = obj["origin"]
    url = og["download"]
    for _ in range(4):
        headers = {"User-Agent": ORIGIN_AGENT, "Referer": og["page"]}
        if have:
            headers["Range"] = "bytes=%d-" % have
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (401, 403, 404, 410, 429, 503):
                raise OriginNeeded(obj, "it answered %d %s" % (e.code, e.reason)) from None
            raise
        if "text/html" not in r.headers.get("Content-Type", "").lower():
            return r
        with r:
            page = r.read(2 << 20).decode("utf-8", "replace")
        m = _MIRROR.search(page)
        if not m:
            raise OriginNeeded(obj, "its download page had no download link Umbra could follow")
        url = urllib.parse.urljoin(r.url, html.unescape(m.group(1)))
    raise OriginNeeded(obj, "it kept answering with pages instead of the file")


def _join(base, rel):
    if is_url(base):
        return base.rstrip("/") + "/" + urllib.parse.quote(rel)
    return os.path.join(base, *rel.split("/"))


def is_url(s):
    return s.startswith(("http://", "https://"))


def get_bytes(base, rel, timeout=60, retries=4):
    """A small file (channel, pack) fetched whole."""
    src = _join(base, rel)
    if not is_url(base):
        try:
            with open(src, "rb") as f:
                return f.read()
        except OSError as e:
            raise FetchError("cannot read %s (%s)" % (src, e))
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(src, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, OSError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                break
            time.sleep(min(8, 1 + attempt * 2))
    raise FetchError("cannot download %s (%s)" % (src, last))


def load_pack(base, channel):
    """(channel dict, pack dict) from the base URL or folder, checked."""
    ch = packdef.check_channel(packdef.loads(get_bytes(base, "channels/%s.json" % channel), "the channel file"))
    blob = get_bytes(base, ch["pack"])
    if packdef.sha256_bytes(blob) != ch["sha256"]:
        raise FetchError("the pack manifest does not match its channel (a publish may be in progress; try again "
                         "in a few minutes)")
    return ch, packdef.check_pack(packdef.loads(blob, "the pack manifest"))


class Fetcher:
    """Fetches objects into cache_dir/objects/, in parallel, with progress.

    progress(done_bytes, total_bytes, message) is called from worker threads;
    cancel() returning True stops the downloads (Cancelled is raised)."""

    def __init__(self, base, cache_dir, progress=None, cancel=None, workers=4, timeout=60, look_in=None, log=None):
        self.base, self.cache_dir = base, cache_dir
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel or (lambda: False)
        self.look_in = look_in               # where an author's file may be already (None: Downloads)
        self.log = log or (lambda msg, level="info": None)
        self.workers, self.timeout = workers, timeout
        self._lock = threading.Lock()
        self._done = 0
        self._total = 0

    def path(self, obj):
        return os.path.join(self.cache_dir, *obj["path"].split("/"))

    def have(self, obj):
        """Already in the cache and whole (size check only; verified when fetched)."""
        p = self.path(obj)
        return os.path.isfile(p) and os.path.getsize(p) == obj["size"] and os.path.exists(p + ".ok")

    def _add(self, n, msg=""):
        with self._lock:
            self._done += n
            done, total = self._done, self._total
        self.progress(done, total, msg)

    def fetch_all(self, objs):
        """Fetch every object not already cached; returns their local paths (same order)."""
        uniq = {}
        for o in objs:
            uniq.setdefault(o["sha256"], o)
        todo = [o for o in uniq.values() if not self.have(o)]
        self._total = sum(o["size"] for o in todo)
        self._done = 0
        self.progress(0, self._total, "%d file%s to download" % (len(todo), "" if len(todo) == 1 else "s"))
        if todo:
            with cf.ThreadPoolExecutor(max_workers=self.workers) as ex:
                futs = [ex.submit(self._fetch_one, o) for o in todo]
                try:
                    for f in cf.as_completed(futs):
                        f.result()
                except BaseException:
                    self._stop = True
                    for f in futs:
                        f.cancel()
                    raise
        return [self.path(o) for o in objs]

    def _fetch_one(self, obj):
        dst = self.path(obj)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        part = dst + ".part"
        if "origin" in obj:
            mine = find_downloaded(obj, self.look_in if self.look_in is not None else downloads_dirs(), self.cancel)
            if mine:
                self.log('Using the "%s" you downloaded (%s)' % (obj["origin"]["name"], mine))
                shutil.copyfile(mine, part)
                os.replace(part, dst)
                open(dst + ".ok", "w").close()
                self._add(obj["size"])
                return dst
            self.log('Downloading "%s" from %s (%s), as its author asks'
                     % (obj["origin"]["name"], obj["origin"].get("site") or "its author's page", obj["origin"]["page"]))
        for fresh in (False, True):
            if fresh and os.path.exists(part):
                os.unlink(part)
            self._download(obj, part)
            if packdef.sha256_file(part, self.cancel) == obj["sha256"]:
                os.replace(part, dst)
                open(dst + ".ok", "w").close()
                return dst
            # a damaged part: count its bytes back out and start over once
            self._add(-os.path.getsize(part), "a download was damaged; fetching it again")
        raise FetchError("%s keeps arriving damaged (sha256 mismatch)" % obj["path"])

    def _download(self, obj, part):
        origin = "origin" in obj
        if not is_url(self.base) and not (origin and not os.path.isfile(_join(self.base, obj["path"]))):
            # a local folder (a test, or an offline copy that has the author's file too)
            src = _join(self.base, obj["path"])
            have = os.path.getsize(part) if os.path.exists(part) else 0
            with open(src, "rb") as fin, open(part, "ab") as fout:
                fin.seek(have)
                while True:
                    if self.cancel():
                        raise Cancelled()
                    b = fin.read(CHUNK)
                    if not b:
                        break
                    fout.write(b)
                    self._add(len(b))
            return
        url = None if origin else _join(self.base, obj["path"])
        attempt = 0
        while True:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have >= obj["size"]:
                return
            headers = {"User-Agent": USER_AGENT}
            if have:
                headers["Range"] = "bytes=%d-" % have
            try:
                if origin:
                    resp = open_origin(obj, have, self.timeout)
                else:
                    resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout)
                with resp as r:
                    if have and r.status != 206:      # the server ignored Range: start over
                        self._add(-have)
                        have = 0
                        open(part, "wb").close()
                    with open(part, "ab") as f:
                        while True:
                            if self.cancel():
                                raise Cancelled()
                            b = r.read(CHUNK)
                            if not b:
                                break
                            f.write(b)
                            self._add(len(b))
                return
            except (Cancelled, OriginNeeded):
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                attempt += 1
                if isinstance(e, urllib.error.HTTPError) and e.code in (403, 404, 410):
                    raise FetchError("%s: %s" % (obj["path"], e))
                if attempt > 8:
                    raise FetchError("%s: %s (gave up after %d tries)" % (obj["path"], e, attempt))
                self.progress(self._done, self._total, "connection trouble (%s); retrying" % e)
                for _ in range(min(30, 2 ** attempt)):
                    if self.cancel():
                        raise Cancelled()
                    time.sleep(1)


def free_bytes(path):
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free
