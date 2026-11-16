"""Three-way merge of X-Ray all.spawn files that share a game graph prefix (stdlib only).

  spawnmerge.py BASE OURS THEIRS OUT

BASE   the spawn both sides started from (Anomaly's own)
OURS   the side whose existing levels win (G.A.M.M.A.'s, with New Storylines)
THEIRS the side that adds levels at the end of the graph (New Levels)
OUT    the merged all.spawn

The result takes THEIRS' game graph (chunk 4) and header GUIDs, THEIRS' objects on its new
levels, and on the existing levels OURS' objects plus the ones THEIRS added there, minus the ones
it removed. Where THEIRS changed an existing object or patrol, OURS' copy stays (New Levels' are
decompile/recompile artifacts: quotes dropped from waypoint names, Cyrillic comments turned to
"???"). Patrol paths: OURS' plus THEIRS' new ones, minus the ones THEIRS removed. Every choice
is reported.
"""
import hashlib, struct, sys
from collections import Counter, defaultdict


def chunks(data, o=0, end=None):
    end = len(data) if end is None else end
    while o + 8 <= end:
        cid, sz = struct.unpack_from("<II", data, o)
        yield cid, o + 8, sz
        o += 8 + sz


def sub(data, off, size, want):
    for cid, o, sz in chunks(data, off, off + size):
        if cid & 0x7FFFFFFF == want:
            return o, sz
    raise KeyError(want)


def stringz(b, o):
    e = b.index(b"\0", o)
    return b[o:e].decode("cp1251", "replace"), e + 1


class Obj:
    __slots__ = ("name", "section", "gvid", "raw", "sid_off", "key")

    def __init__(self, raw):
        # raw = one vertex chunk's body: sub 0 (u16 vertex id), sub 1 (wrapper: 0 spawn packet, 1 update)
        self.raw = raw
        wo, ws = sub(raw, 0, len(raw), 1)
        so, ss = sub(raw, wo, ws, 0)
        n, = struct.unpack_from("<H", raw, so)
        p = raw[so + 2:so + 2 + n]
        o = 2
        self.section, o = stringz(p, o)
        self.name, o = stringz(p, o)
        o += 1 + 1 + 12 + 12 + 2 + 2 + 2 + 2
        flags, = struct.unpack_from("<H", p, o); o += 2
        ver = 0
        if flags & 0x20:
            ver, = struct.unpack_from("<H", p, o); o += 2
        if ver > 120: o += 2
        if ver > 69: o += 2
        if ver > 70:
            k, = struct.unpack_from("<H", p, o); o += 2 + k
        if ver <= 79:
            raise ValueError("spawn packet version %d has no spawn id" % ver)
        self.sid_off = so + 2 + o                       # offset of m_tSpawnID inside raw
        o += 2 + 2
        self.gvid, = struct.unpack_from("<H", p, o)     # CSE_ALifeObject: m_tGraphID first
        masked = bytearray(raw)
        masked[self.sid_off:self.sid_off + 2] = b"\0\0"
        io_, _ = sub(raw, 0, len(raw), 0)
        masked[io_:io_ + 2] = b"\0\0"
        self.key = (self.name, self.section, hashlib.sha1(bytes(masked)).digest())

    def with_id(self, i):
        b = bytearray(self.raw)
        io_, _ = sub(self.raw, 0, len(self.raw), 0)
        struct.pack_into("<H", b, io_, i)
        struct.pack_into("<H", b, self.sid_off, i)
        return bytes(b)


class Spawn:
    def __init__(self, path):
        self.path = path
        d = self.data = open(path, "rb").read()
        self.top = [(cid, o, sz) for cid, o, sz in chunks(d)]
        if [c for c, _, _ in self.top] != [0, 1, 2, 3, 4]:
            raise ValueError("%s: unexpected chunks %s" % (path, [c for c, _, _ in self.top]))
        t = {c: (o, sz) for c, o, sz in self.top}
        o1, s1 = t[1]
        sc = [(c, o, sz) for c, o, sz in chunks(d, o1, o1 + s1)]
        if [c for c, _, _ in sc] != [0, 1, 2] or sc[2][2] != 0:
            raise ValueError("%s: spawn graph with edges or extra parts isn't handled" % path)
        vo, vs = sc[1][1], sc[1][2]
        self.objs = []
        for i, (cid, o, sz) in enumerate(chunks(d, vo, vo + vs)):
            if cid != i:
                raise ValueError("%s: vertex %d has id %d" % (path, i, cid))
            self.objs.append(Obj(d[o:o + sz]))
        cnt, = struct.unpack_from("<I", d, sc[0][1])
        if cnt != len(self.objs):
            raise ValueError("%s: count %d but %d objects" % (path, cnt, len(self.objs)))
        o3, s3 = t[3]
        self.patrol_head = []                   # chunk 3's parts before the patrol list, kept as they are
        self.patrols = []                       # [(name, raw body)]
        for c, o, sz in chunks(d, o3, o3 + s3):
            if c == 1:
                for _, po, ps in chunks(d, o, o + sz):
                    fo, fs = sub(d, po, ps, 0)
                    self.patrols.append((stringz(d, fo)[0], d[po:po + ps]))
            else:
                self.patrol_head.append((c, d[o:o + sz]))
        self.header = d[t[0][0]:t[0][0] + t[0][1]]

    def chunk(self, cid):
        for c, o, sz in self.top:
            if c == cid:
                return self.data[o:o + sz]


def vertex_levels(graph):
    """Level id of every game vertex. CoP game graph: u8 version, u16 vertices, u32 edges,
    u32 death points, guid[16], u8 levels; per level stringz name, vec3 offset, u8 id, stringz
    section, guid[16]; then 42-byte vertices (level id = low 8 bits of the u32 at +24)."""
    nv, = struct.unpack_from("<H", graph, 1)
    nl = graph[1 + 2 + 4 + 4 + 16]
    o = 1 + 2 + 4 + 4 + 16 + 1
    for _ in range(nl):
        o = graph.index(b"\0", o) + 1 + 12 + 1
        o = graph.index(b"\0", o) + 1 + 16
    return [struct.unpack_from("<I", graph, o + 42 * i + 24)[0] & 0xFF for i in range(nv)]


def level_count(spawn):
    return struct.unpack_from("<I", spawn.header, 40)[0]


def first_new_gvid(theirs, ours):
    """THEIRS must extend OURS' game graph: the same vertices first, its own after."""
    g_o, g_t = ours.chunk(4), theirs.chunk(4)
    vo, = struct.unpack_from("<H", g_o, 4 + 1)[0:1] if False else (None,)
    # header: u8 version, u16 vertex_count, u32 edge_count, u32 death_point_count, guid[16], u8 level_count
    ver_o, nv_o = g_o[0], struct.unpack_from("<H", g_o, 1)[0]
    ver_t, nv_t = g_t[0], struct.unpack_from("<H", g_t, 1)[0]
    if ver_o != ver_t:
        raise ValueError("graph versions differ: %d, %d" % (ver_o, ver_t))
    if nv_t <= nv_o:
        raise ValueError("THEIRS' graph has no more vertices than OURS' (%d <= %d)" % (nv_t, nv_o))
    return nv_o


def write(path, base_header, top_chunks):
    out = bytearray()
    for cid, body in top_chunks:
        out += struct.pack("<II", cid, len(body)) + body
    with open(path + ".tmp", "wb") as f:
        f.write(out)
    import os
    os.replace(path + ".tmp", path)


def merge(base, ours, theirs, report=print):
    new_from = first_new_gvid(theirs, ours)
    report("graph: %s's first %d vertices, then %d of its own" % ("THEIRS", new_from,
           struct.unpack_from("<H", theirs.chunk(4), 1)[0] - new_from))
    bag = lambda objs: Counter(o.key for o in objs)
    old = lambda objs: [o for o in objs if o.gvid < new_from]
    b_bag, t_old = bag(base.objs), old(theirs.objs)
    t_bag = bag(t_old)
    # On the existing levels THEIRS' own work is objects with new names (its level changers) and
    # names it dropped (the routes it replaced). A name both have is OURS' (THEIRS' differing copies
    # are toolchain artifacts). Breakables are named by mesh index (meshes\brkbl#N.ogf), which
    # every rebuild renumbers: never theirs to add or remove.
    brk = lambda nm: nm.startswith("meshes\\brkbl")
    lv = vertex_levels(theirs.chunk(4))
    at = lambda o: (o.name, lv[o.gvid] if o.gvid < len(lv) else -1)
    b_names = {at(o) for o in base.objs}
    t_names = {at(o) for o in t_old}
    o_bag = bag(ours.objs)
    take, drop = Counter(), Counter()
    kept = {o.name for o in t_old if at(o) in b_names and not brk(o.name)} - \
        {k[0] for k in (t_bag & b_bag)}
    for o in t_old:
        if at(o) not in b_names and not brk(o.name):
            take[o.key] += 1
            report("  THEIRS added   %-45s %s" % (o.name, o.section))
    for o in base.objs:
        if o.gvid < new_from and at(o) not in t_names and not brk(o.name) and o_bag.get(o.key):
            drop[o.key] += 1
            report("  THEIRS removed %-45s %s" % (o.name, o.section))
    for nm in sorted(kept):
        report("  OURS kept      %-45s (THEIRS changed it; G.A.M.M.A. ships and tests its copy)" % nm)
    result = []
    left = Counter(drop)
    for o in ours.objs:
        if left.get(o.key):
            left[o.key] -= 1
            continue
        result.append(o)
    pending = Counter(take)
    for o in t_old:
        if pending.get(o.key):
            pending[o.key] -= 1
            result.append(o)
    result += [o for o in theirs.objs if o.gvid >= new_from]
    report("objects: OURS %d, -%d +%d on existing levels, +%d on THEIRS' new levels = %d" % (
        len(ours.objs), sum(drop.values()) - sum(left.values()), sum(take.values()) - sum(pending.values()),
        sum(1 for o in theirs.objs if o.gvid >= new_from), len(result)))
    if len(result) > 0xFFFE:
        raise ValueError("too many objects for 16-bit spawn ids: %d" % len(result))
    names = Counter(o.name for o in result)
    # patrols
    P = lambda s: {n: hashlib.sha1(b).digest() for n, b in s.patrols}
    pb, po, pt = P(base), P(ours), P(theirs)
    t_raw = dict(theirs.patrols)
    pat = list(ours.patrols)
    idx = {n: i for i, (n, _) in enumerate(pat)}
    np_add = np_chg = np_del = 0
    for n, h in pt.items():
        if n in pb:
            if pb[n] != h:
                np_chg += 1                   # THEIRS changed an existing patrol: OURS' stays
            continue
        if n in idx:
            report("  patrol %s added on both sides: OURS' kept" % n); continue
        pat.append((n, t_raw[n])); idx[n] = len(pat) - 1; np_add += 1
    # a patrol THEIRS removed goes only on a level where it removed objects (the reject ways of the
    # level changers it replaced); elsewhere it's an unrelated cleanup and OURS' stays
    changed_levels = {lv[o.gvid] for o in base.objs if o.key in drop}
    def levels_of(body):
        po_, ps_ = sub(body, 0, len(body), 1); lo_, ls_ = sub(body, po_, ps_, 1); out = set()
        for _, o_, sz_ in chunks(body, lo_, lo_ + ls_):
            do_, _ = sub(body, o_, sz_, 1); q = body.index(b"\0", do_) + 1
            out.add(lv[struct.unpack_from("<H", body, q + 20)[0]])
        return out
    b_raw = dict(base.patrols)
    gone = []
    for n in pb:
        if n not in pt and n in idx and po[n] == pb[n]:
            if levels_of(b_raw[n]) & changed_levels:
                gone.append(n); report("  THEIRS removed patrol %s" % n)
            else:
                report("  OURS kept patrol %s (THEIRS removed it, on a level it didn't otherwise change)" % n)
    if gone:
        pat = [(n, b) for n, b in pat if n not in set(gone)]
        np_del = len(gone)
    report("patrols: OURS %d, +%d added by THEIRS, %d THEIRS changed (OURS kept), -%d removed = %d" % (len(ours.patrols), np_add, np_chg, np_del, len(pat)))
    return serialize(theirs.header, result, theirs.chunk(2), ours.patrol_head, pat, theirs.chunk(4))


def serialize(header, objs, chunk2, patrol_head, patrols, chunk4):
    """The five chunks of an all.spawn, objects renumbered in order."""
    hdr = bytearray(header)
    struct.pack_into("<I", hdr, 36, len(objs))
    verts = bytearray()
    for i, o in enumerate(objs):
        body = o.with_id(i)
        verts += struct.pack("<II", i, len(body)) + body
    c1 = struct.pack("<II", 0, 4) + struct.pack("<I", len(objs)) + struct.pack("<II", 1, len(verts)) + verts \
        + struct.pack("<II", 2, 0)
    plist = bytearray()
    for i, (n, b) in enumerate(patrols):
        plist += struct.pack("<II", i, len(b)) + b
    c3 = bytearray()
    for c, body in patrol_head:
        if c == 0:                            # the patrol count
            body = struct.pack("<I", len(patrols))
        c3 += struct.pack("<II", c, len(body)) + body
    c3 += struct.pack("<II", 1, len(plist)) + plist
    return [(0, bytes(hdr)), (1, bytes(c1)), (2, chunk2), (3, bytes(c3)), (4, chunk4)]


def main():
    args = sys.argv[1:]
    base, ours, theirs, out = args
    chunks_ = merge(Spawn(base), Spawn(ours), Spawn(theirs))
    write(out, None, chunks_)
    print("wrote", out)


if __name__ == "__main__":
    main()
