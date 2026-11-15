"""
Which file the game actually loads for a path, the way MO2 and the engine pick it.

    python3 vfs.py textures/ui/ui_common.dds        who wins, and who it beats
    python3 vfs.py --list textures/act/ | head       every winning path under a folder

Order, highest first:
  1. loose files in the profile's enabled MO2 mods, top of modlist.txt first
     (MO2 overlays them on Anomaly's gamedata/; names are case-insensitive);
  2. loose files in Anomaly's own gamedata/;
  3. Anomaly's .db archives, in the order fsgame.ltx mounts them (later wins:
     configs, levels, meshes, sounds, textures, mods, patches, addons).

Paths are given and returned relative to gamedata/, with forward slashes,
lower-cased for lookups.

Locations default to the source instance in ../pack/umbra.toml's [source];
override with UMBRA_MO2, UMBRA_ANOMALY, UMBRA_PROFILE and UMBRA_XDB (the folder
holding ZGO's xdb.py, the .db reader).
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GAMMA_ROOT = os.path.normpath(os.path.join(REPO, ".."))

MO2 = os.environ.get("UMBRA_MO2", os.path.join(GAMMA_ROOT, "gamma"))
ANOMALY = os.environ.get("UMBRA_ANOMALY", os.path.join(GAMMA_ROOT, "anomaly"))
PROFILE = os.environ.get("UMBRA_PROFILE", "G.A.M.M.A")
XDB = os.environ.get("UMBRA_XDB", os.path.join(GAMMA_ROOT, "zgo", "tools", "zgo_baker"))

ARCHIVE_DIRS = ["configs", "levels", "meshes", "sounds", "textures", "mods", "patches", "addons"]


def enabled_mods(mo2=MO2, profile=PROFILE):
    """Enabled mod folders, highest priority first (modlist.txt is written top = highest)."""
    out = []
    with open(os.path.join(mo2, "profiles", profile, "modlist.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.startswith("+") and not line.endswith("_separator"):
                out.append(line[1:])
    return out


def _xdb():
    if XDB not in sys.path:
        sys.path.insert(0, XDB)
    import xdb
    return xdb


class Archive:
    """One .db archive. Only its file list is read up front (archives are GBs);
    members are read by seeking. Format: see ZGO's xdb.py."""

    def __init__(self, path):
        self.path = path
        self.files = {}
        xdb = _xdb()
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            o = 0
            lst = None
            while o + 8 <= size:
                f.seek(o)
                cid, sz = struct.unpack("<II", f.read(8))
                if cid & 0x7FFFFFFF == 1:
                    body = f.read(sz)
                    lst = xdb.lzhuf_decode(body) if cid & 0x80000000 else body
                o += 8 + sz
        if lst is None:
            raise ValueError("%s: no file list" % path)
        o = 0
        while o < len(lst):
            bs, = struct.unpack_from("<H", lst, o)
            real, comp, _crc = struct.unpack_from("<III", lst, o + 2)
            name = lst[o + 14:o + 2 + bs - 4].decode("latin1")
            ptr, = struct.unpack_from("<I", lst, o + 2 + bs - 4)
            self.files[name.lower()] = (ptr, real, comp)
            o += 2 + bs

    def names(self):
        return self.files.keys()

    def read(self, name):
        ptr, real, comp = self.files[name.lower()]
        with open(self.path, "rb") as f:
            f.seek(ptr)
            raw = f.read(comp)
        return raw if real == comp else _xdb().lzo1x_decompress(raw, real)


class Source:
    """Where a winning file comes from: a loose file or an archive member."""

    def __init__(self, label, path=None, archive=None, member=None):
        self.label, self.path, self.archive, self.member = label, path, archive, member

    def read(self):
        if self.path:
            with open(self.path, "rb") as f:
                return f.read()
        return self.archive.read(self.member)

    def __repr__(self):
        return "%s:%s" % (self.label, self.path or self.member)


class VFS:
    def __init__(self, mo2=MO2, anomaly=ANOMALY, profile=PROFILE, archives=True, prefix=""):
        """prefix: only index paths under this folder (e.g. "textures/"), much faster."""
        self.prefix = prefix.lower().replace("\\", "/")
        self.mods = enabled_mods(mo2, profile)
        self.layers = []          # [(label, {rel_lower: Source})], highest first
        for m in self.mods:
            self.layers.append((m, self._walk(os.path.join(mo2, "mods", m, "gamedata"), m)))
        self.layers.append(("anomaly/gamedata", self._walk(os.path.join(anomaly, "gamedata"), "anomaly")))
        if archives:
            arch = {}
            for d in ARCHIVE_DIRS:
                folder = os.path.join(anomaly, "db", d)
                if not os.path.isdir(folder):
                    continue
                for n in sorted(os.listdir(folder)):
                    if ".db" not in n.lower():
                        continue
                    a = Archive(os.path.join(folder, n))
                    for name in a.names():
                        rel = name.replace("\\", "/").lower()
                        if rel.startswith(self.prefix):
                            arch[rel] = Source("db/%s/%s" % (d, n), archive=a, member=name)
            self.layers.append(("archives", arch))

    def _walk(self, root, label):
        out = {}
        if not os.path.isdir(root):
            return out
        start = root
        # descend into the prefix folder, matching names without case
        for part in [p for p in self.prefix.split("/") if p]:
            try:
                hit = [e for e in os.listdir(start) if e.lower() == part and os.path.isdir(os.path.join(start, e))]
            except OSError:
                hit = []
            if not hit:
                return out
            start = os.path.join(start, hit[0])
        for dp, _dn, fns in os.walk(start):
            for fn in fns:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/").lower()
                if rel.startswith(self.prefix):
                    out[rel] = Source(label, path=full)
        return out

    def find(self, rel):
        """The winning Source for a path, or None."""
        rel = rel.lower().replace("\\", "/")
        for _label, files in self.layers:
            s = files.get(rel)
            if s:
                return s
        return None

    def all(self, rel):
        """Every Source for a path, winner first."""
        rel = rel.lower().replace("\\", "/")
        return [files[rel] for _label, files in self.layers if rel in files]

    def winners(self, folder=""):
        """{rel: winning Source} for every path under folder."""
        folder = folder.lower()
        out = {}
        for _label, files in reversed(self.layers):
            for rel, s in files.items():
                if rel.startswith(folder):
                    out[rel] = s
        return out


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--list":
        v = VFS(prefix=args[1])
        for rel, s in sorted(v.winners(args[1]).items()):
            print("%s\t%s" % (rel, s.label))
        return 0
    for rel in args:
        v = VFS(prefix=os.path.dirname(rel) + "/")
        found = v.all(rel)
        if not found:
            print("%s: not found" % rel)
        for i, s in enumerate(found):
            print("%s %s" % ("WIN " if i == 0 else "    ", s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
