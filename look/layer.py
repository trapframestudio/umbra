"""
The look's part of the Umbra mod, made from what the other tools build in out/full.

    python3 layer.py           write out/Umbra (pack/umbra.toml's "umbra" layer packs it)
    python3 layer.py --check   say what would ship, write nothing

The tools build whole files: G.A.M.M.A.'s atlas with our cells painted in, its string
table with one line changed. Shipping those would replace everything else in them too,
and keep our old copy of the rest when G.A.M.M.A. changes it. So only what we changed
ships, on top of G.A.M.M.A.'s own files:

- **UI atlases the game reaches through texture ids** (configs/ui/textures_descr): the
  cells we changed go into an atlas of our own (textures/umbra/<same path>.dds), and
  configs/ui/textures_descr/zzzzzzzz_umbra.xml points those ids at it. The engine reads
  those files in name order and a later definition of an id replaces the earlier one
  (CUITextureMaster::ParseShTexInfo), so G.A.M.M.A.'s atlas stays in use for the rest.
  A layout or script that names the atlas by path still gets G.A.M.M.A.'s pixels; the
  build says where that happens.
- **String tables:** the strings we changed go in configs/text/<lang>/zzzzzzzz_umbra.xml
  (a later id replaces an earlier one, CStringTable::Load).
- **Whole files**, where nothing narrower works: files the engine or a layout names by
  path (the loading screen, the menu logo and badge, the background loop, the faction
  screen's atlas), the fonts (picked by file name; fonts.ltx stays G.A.M.M.A.'s), and
  files that are only ours (the currency strings, the font licences).
"""
import os
import re
import shutil
import sys

import numpy as np

import dds
import vfs

HERE = os.path.dirname(os.path.abspath(__file__))
FULL = os.path.join(HERE, "out", "full")
OUT = os.path.join(HERE, "out", "Umbra")
OVERRIDE = "zzzzzzzz_umbra.xml"       # sorts after G.A.M.M.A.'s zzzz_ and xrRazom's zzzzzz_ files
OWN_DIR = "umbra"                     # our atlases: textures/umbra/<the atlas's path>.dds
MARGIN = 2                            # pixels kept around each packed cell, so filtering at its
                                      # edge reads the same neighbours as in G.A.M.M.A.'s atlas
DESCR_DIR = "configs/ui/textures_descr/"


def _attrs(s):
    return re.findall(r'([\w]+)\s*=\s*"([^"]*)"', s)


def texture_ids(cfg):
    """{id: (atlas, {attribute: value})} as the game ends up with them: files in name
    order, a later definition replacing an earlier one. atlas: "ui/ui_common" style."""
    files = cfg.winners(DESCR_DIR)
    out = {}
    for rel in sorted(files, key=lambda r: r.rsplit("/", 1)[-1]):
        name = rel.rsplit("/", 1)[-1]
        if not name.endswith(".xml") or name == OVERRIDE:
            continue
        t = files[rel].read().decode("cp1251", "replace")
        t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
        for fm in re.finditer(r'<file\s+name\s*=\s*"([^"]+)"[^>]*>(.*?)</file>', t, re.S):
            atlas = fm.group(1).replace("\\", "/").lower()
            for tm in re.finditer(r"<texture\s([^>]*?)/?>", fm.group(2)):
                a = dict(_attrs(tm.group(1)))
                if "id" in a:
                    out[a["id"]] = (atlas, a)
    return out


def rect(a):
    return tuple(float(a.get(k) or 0) for k in ("x", "y", "width", "height"))


def path_refs(cfg, scripts, atlas):
    """Where a layout or script names an atlas by path instead of by id:
    [(file, (x, y, w, h) or None)]."""
    pat = re.escape(atlas.replace("/", "\\")).replace(r"\\", r"\\{1,2}")
    out = []
    for V, folder in ((cfg, "configs/ui/"), (scripts, "scripts/")):
        for rel, src in V.winners(folder).items():
            if rel.startswith(DESCR_DIR) or not rel.endswith((".xml", ".script")):
                continue
            t = src.read().decode("cp1251", "replace")
            if not re.search(pat + r"(?![\w\\])", t, re.I):
                continue
            hits = list(re.finditer(r"<texture([^>]*)>\s*" + pat + r"\s*</texture>", t, re.I))
            if not hits:
                out.append((rel, None))
            for m in hits:
                a = dict(_attrs(m.group(1)))
                r = rect(a)
                out.append((rel, r if r[2] and r[3] else None))
    return out


def groups(rects):
    """Overlapping cells grouped (they're packed together): [[index, ...], ...]."""
    boxes = [(int(np.floor(x)), int(np.floor(y)), int(np.ceil(x + w)), int(np.ceil(y + h)))
             for x, y, w, h in rects]
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, a in enumerate(boxes):
        for j in range(i):
            b = boxes[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                parent[find(i)] = find(j)
    out = {}
    for i in range(len(boxes)):
        out.setdefault(find(i), []).append(i)
    return list(out.values()), boxes


def _pow2(n):
    p = 1
    while p < n:
        p *= 2
    return p


def shelf_pack(sizes):
    """Places for boxes (w, h) in the smallest power-of-two atlas a shelf packer finds:
    (width, height, [(x, y), ...])."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0]))
    best = None
    for W in (256, 512, 1024, 2048, 4096, 8192):
        if W < max(w for w, _ in sizes):
            continue
        places = [None] * len(sizes)
        x = y = shelf = 0
        for i in order:
            w, h = sizes[i]
            if x + w > W:
                x, y, shelf = 0, y + shelf, 0
            places[i] = (x, y)
            x += w
            shelf = max(shelf, h)
        H = _pow2(y + shelf)
        if H <= 8192 and (best is None or W * H < best[0] * best[1]):
            best = (W, H, places)
    if best is None:
        raise ValueError("the changed cells don't fit an 8192x8192 atlas")
    return best


def crop_padded(img, box):
    """img[box] where box may reach past the edges (the edge pixels repeat there)."""
    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
    part = img[cy0:cy1, cx0:cx1]
    return np.pad(part, ((cy0 - y0, y1 - cy1), (cx0 - x0, x1 - cx1), (0, 0)), mode="edge")


def num(v):
    return str(int(v)) if float(v).is_integer() else repr(round(float(v), 4))


def narrow_atlas(atlas, ours, orig, ids, report):
    """Our cells of one atlas: (packed RGBA, {id: attributes for the override}), or None
    when nothing an id shows changed."""
    changed = np.any(ours != orig, axis=2)
    mine = [(tid, rect(a), a) for tid, (f, a) in sorted(ids.items()) if f == atlas]
    hit = []
    covered = np.zeros_like(changed)
    for tid, (x, y, w, h), a in mine:
        y0, x0 = int(np.floor(y)), int(np.floor(x))
        y1, x1 = int(np.ceil(y + h)), int(np.ceil(x + w))
        covered[max(y0, 0):y1, max(x0, 0):x1] = True
        if w > 0 and h > 0 and changed[max(y0, 0):y1, max(x0, 0):x1].any():
            hit.append((tid, (x, y, w, h), a))
    loose = int((changed & ~covered).sum())
    if not hit:
        report.append("  %s: no cell a texture id shows changed; nothing ships" % atlas)
        return None, loose
    gs, boxes = groups([r for _, r, _ in hit])
    regions = []
    for g in gs:
        bx = (min(boxes[i][0] for i in g) - MARGIN, min(boxes[i][1] for i in g) - MARGIN,
              max(boxes[i][2] for i in g) + MARGIN, max(boxes[i][3] for i in g) + MARGIN)
        regions.append((g, bx))
    W, H, places = shelf_pack([(b[2] - b[0], b[3] - b[1]) for _, b in regions])
    out = np.zeros((H, W, 4), np.uint8)
    defs = {}
    for (g, bx), (px, py) in zip(regions, places):
        piece = crop_padded(ours, bx)
        out[py:py + piece.shape[0], px:px + piece.shape[1]] = piece
        for i in g:
            tid, (x, y, w, h), a = hit[i]
            d = dict(a)
            d["x"], d["y"] = num(px + (x - bx[0])), num(py + (y - bx[1]))
            defs[tid] = d
    return (out, defs), loose


# ---------------------------------------------------------------- strings
STRING = re.compile(r'<string\s+id="([^"]+)"\s*>\s*<text>(.*?)</text>', re.S)


def strings_of(data):
    t = data.decode("cp1251", "surrogateescape")
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    return {m.group(1): m.group(2) for m in STRING.finditer(t)}


def string_table(strings, why):
    body = "".join('\t<string id="%s">\n\t\t<text>%s</text>\n\t</string>\n' % (sid, text)
                   for sid, text in sorted(strings.items()))
    xml = ('<?xml version="1.0" encoding="windows-1251" ?>\n'
           "<!-- Umbra: %s. Generated by look/layer.py; don't edit by hand. -->\n"
           "<string_table>\n%s</string_table>\n" % (why, body))
    return xml.encode("cp1251", "surrogateescape")


# ---------------------------------------------------------------- the build
def build(check=False):
    if not os.path.isdir(FULL):
        sys.exit("%s is not there: run splash.py, ui.py, fonts.py and currency.py first" % FULL)
    cfg = vfs.VFS(prefix="configs/")
    tex = vfs.VFS(prefix="textures/")
    scripts = vfs.VFS(prefix="scripts/")
    ids = texture_ids(cfg)
    ship = {}             # rel in the mod -> bytes, or a path to copy
    report = []
    overrides = {}        # atlas -> {id: attributes}
    texts = {}            # lang -> {id: text}
    whole = 0
    for dp, _dns, fns in os.walk(FULL):
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, FULL).replace(os.sep, "/")
            low = rel.lower()
            game = low[len("gamedata/"):] if low.startswith("gamedata/") else None
            if game and game.startswith("textures/") and game.endswith(".dds"):
                atlas = game[len("textures/"):-len(".dds")]
                src = tex.find(game)
                has_ids = any(f == atlas for f, _ in ids.values())
                if src and has_ids:
                    ours, orig = dds.read(open(full, "rb").read()), dds.read(src.read())
                    if ours.shape == orig.shape:
                        got, loose = narrow_atlas(atlas, ours, orig, ids, report)
                        if got:
                            img, defs = got
                            ship["gamedata/textures/%s/%s.dds" % (OWN_DIR, atlas)] = img
                            overrides[atlas] = defs
                            report.append("  %s: %d ids -> %s/%s (%dx%d)" % (
                                atlas, len(defs), OWN_DIR, atlas, img.shape[1], img.shape[0]))
                        refs = path_refs(cfg, scripts, atlas) if (loose or got) else []
                        if loose:
                            report.append("    %d changed pixels no texture id shows: %s" % (
                                loose, "left out (nothing names the atlas by path)" if not refs else
                                "left out; named by path in " + ", ".join(sorted({r for r, _ in refs}))))
                        if got:
                            changed = np.any(ours != orig, axis=2)
                            seen = {}
                            for r, box in refs:
                                if box is not None:
                                    x, y, w, h = (int(round(v)) for v in box)
                                    if not changed[y:y + h, x:x + w].any():
                                        continue
                                    box = "(%d,%d %dx%d)" % (x, y, w, h)
                                seen[(r, box)] = seen.get((r, box), 0) + 1
                            for (r, box), n in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
                                report.append("    %s names it by path%s%s: G.A.M.M.A.'s pixels there" % (
                                    r, " for " + box if box else " (whole, or in code)",
                                    " (%d times)" % n if n > 1 else ""))
                        continue
                    why = "a different size from G.A.M.M.A.'s"
                else:
                    why = "no texture id names it" if src else "only ours"
                ship[rel] = full
                whole += os.path.getsize(full)
                if not re.match(r"textures/ui/ui_font_", game):
                    report.append("  whole: %s (%s)" % (game, why))
                continue
            m = re.match(r"configs/text/(\w+)/([^/]+\.xml)$", game or "")
            if m:
                src = cfg.find(game)
                if src:
                    old, new = strings_of(src.read()), strings_of(open(full, "rb").read())
                    diff = {k: v for k, v in new.items() if old.get(k) != v}
                    texts.setdefault(m.group(1), {}).update(diff)
                    report.append("  %s: %d string(s) -> %s" % (game, len(diff), OVERRIDE))
                    continue
                if m.group(2).lower() == OVERRIDE:
                    sys.exit("%s: out/full may not have a file named %s" % (rel, OVERRIDE))
            if game and game.startswith("configs/") and game.endswith(".ltx"):
                report.append("  WARNING %s: an .ltx counts in xrRazom's co-op config fingerprint" % game)
            ship[rel] = full
            whole += os.path.getsize(full) if rel.startswith("gamedata/") else 0
            if rel.startswith("gamedata/") and not re.match(r"textures/ui/ui_font_", game):
                report.append("  whole: %s" % game)
    # our string overrides must not fight the other files we ship
    for lang, d in texts.items():
        if not d:
            continue
        for rel, p in ship.items():
            if isinstance(p, str) and re.match(r"gamedata/configs/text/%s/" % lang, rel.lower()):
                clash = set(d) & set(strings_of(open(p, "rb").read()))
                if clash:
                    sys.exit("%s and %s both change %s" % (OVERRIDE, rel, ", ".join(sorted(clash)[:5])))
        ship["gamedata/configs/text/%s/%s" % (lang, OVERRIDE)] = string_table(
            d, "the lines Umbra changes in G.A.M.M.A.'s string tables (only those)")
    if overrides:
        body = []
        for atlas, defs in sorted(overrides.items()):
            body.append('\t<file name="%s">' % ("%s/%s" % (OWN_DIR, atlas)).replace("/", "\\"))
            for tid, a in sorted(defs.items()):
                attrs = " ".join('%s="%s"' % (k, v) for k, v in
                                 [("id", tid)] + [(k, a[k]) for k in ("x", "y", "width", "height") if k in a] +
                                 [(k, v) for k, v in a.items() if k not in ("id", "x", "y", "width", "height")])
                body.append("\t\t<texture %s />" % attrs)
            body.append("\t</file>")
        xml = ('<?xml version="1.0" encoding="windows-1251" ?>\n'
               "<!-- Umbra: the UI cells Umbra repaints, in its own atlases (textures\\umbra).\n"
               "     Later definitions win, so every other id stays G.A.M.M.A.'s. Generated by\n"
               "     look/layer.py; don't edit by hand. -->\n<w>\n%s\n</w>\n" % "\n".join(body))
        ship["gamedata/" + DESCR_DIR + OVERRIDE] = xml.encode("cp1251")
    fonts = [r for r in ship if re.match(r"gamedata/textures/ui/ui_font_", r.lower())]
    if fonts:
        report.append("  whole: the fonts, %d files (the game picks them by file name)" % len(fonts))
    print("\n".join(report))
    total = sum(len(v) if isinstance(v, bytes) else (v.nbytes + 128 if isinstance(v, np.ndarray)
                                                     else os.path.getsize(v)) for v in ship.values())
    full_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(FULL) for f in fs)
    print("%d files, %.0f MB (out/full: %.0f MB; %.0f MB of it whole files)" % (
        len(ship), total / 1e6, full_size / 1e6, whole / 1e6))
    if check:
        return
    tmp = OUT + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    for rel, v in sorted(ship.items()):
        dst = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if isinstance(v, np.ndarray):
            dds.write_rgba(dst, v)
        elif isinstance(v, bytes):
            with open(dst, "wb") as f:
                f.write(v)
        else:
            shutil.copy2(v, dst)
    shutil.rmtree(OUT, ignore_errors=True)
    os.replace(tmp, OUT)
    print("->", OUT)


if __name__ == "__main__":
    build(check="--check" in sys.argv)
