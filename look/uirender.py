"""
Draw the game's UI screens from their XML, for before/after previews.

    python3 uirender.py inventory|loot|trade [--ours]

This draws the art only (panels, frames, buttons, icons that the XML names),
not text or items, at 1920x1080. It follows the engine's rules:

- Screens are laid out in 1024x768 UI units and stretched to the screen
  (the _16 XML files are the 16:9 layouts).
- A <texture> names either an id from configs/ui/textures_descr/*.xml (a
  pixel rect in an atlas) or a texture file. The descr files load in
  alphabetical order and a later definition of an id replaces an earlier one
  (CMainMenu::ReadTextureInfo, CUITextureMaster::ParseShTexInfo).
- Frames: an id with _lt/_t/_rt/_l/_back/_r/_lb/_b/_rb parts; frame lines:
  _b/_back/_e; buttons: _e (enabled) is the one drawn at rest.

Which elements a screen shows is up to its script, so each screen here lists
what its script shows in that mode (see SCREENS).
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dds  # noqa: E402
import vfs  # noqa: E402

SCREEN = (1920, 1080)
KX, KY = SCREEN[0] / 1024, SCREEN[1] / 768
LAYER = os.path.join(HERE, "out", "full", "gamedata")
FRAME = ("lt", "t", "rt", "l", "back", "r", "lb", "b", "rb")


def _attrs(s):
    return dict(re.findall(r'([\w]+)\s*=\s*"([^"]*)"', s))


class Textures:
    """UI texture ids and files as the game resolves them. With ours=True,
    files we build (out/full) win, as they will in game."""

    def __init__(self, ours=False):
        self.ours = ours
        self.cfg = vfs.VFS(prefix="configs/ui/")
        self.tex = vfs.VFS(prefix="textures/")
        self.descr = {}
        self.where = {}
        files = self.cfg.winners("configs/ui/textures_descr/")
        for rel in sorted(files, key=lambda r: r.lower().rsplit("/", 1)[-1]):
            if not rel.lower().endswith(".xml"):
                continue
            t = files[rel].read().decode("cp1251", "replace")
            t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
            for fm in re.finditer(r'<file\s+name="([^"]+)"[^>]*>(.*?)</file>', t, re.S):
                f = fm.group(1).replace("\\", "/").lower()
                for tm in re.finditer(r"<texture\s([^>]*?)/?>", fm.group(2)):
                    a = _attrs(tm.group(1))
                    if "id" not in a:
                        continue
                    r = tuple(float(a.get(k) or 0) for k in ("x", "y", "width", "height"))
                    self.descr[a["id"]] = (f, r)
                    self.where[a["id"]] = files[rel].label
        self._atlas = {}

    def path(self, f):
        """Where the game's file comes from (layer first when ours)."""
        rel = "textures/%s.dds" % f
        if self.ours:
            p = os.path.join(LAYER, rel)
            if os.path.exists(p):
                return p, "Umbra"
        s = self.tex.find(rel)
        return s, (s.label if s else None)

    def atlas(self, f):
        if f not in self._atlas:
            src, _ = self.path(f)
            if src is None:
                self._atlas[f] = None
            else:
                raw = open(src, "rb").read() if isinstance(src, str) else src.read()
                self._atlas[f] = Image.fromarray(dds.read(raw), "RGBA")
        return self._atlas[f]

    def has(self, tid):
        return tid in self.descr or self.atlas(tid.replace("\\", "/").lower()) is not None

    def image(self, tid, rect=None):
        """The RGBA image for a texture id (or file, with an optional pixel rect)."""
        if tid in self.descr:
            f, (x, y, w, h) = self.descr[tid]
        else:
            f = tid.replace("\\", "/").lower()
            x = y = w = h = 0
        a = self.atlas(f)
        if a is None:
            return None
        if rect:
            x, y, w, h = rect
        if w <= 0 or h <= 0:
            return a
        return a.crop((int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h))))

    def size(self, tid):
        if tid in self.descr:
            return self.descr[tid][1][2:]
        im = self.image(tid)
        return im.size if im else (0, 0)


class Canvas:
    def __init__(self, bg=(0, 0, 0)):
        self.im = Image.new("RGBA", SCREEN, bg + (255,))

    def put(self, img, x, y, w, h, color=None):
        """Draw img stretched over UI rect (x, y, w, h)."""
        X0, Y0 = int(round(x * KX)), int(round(y * KY))
        X1, Y1 = int(round((x + w) * KX)), int(round((y + h) * KY))
        if X1 <= X0 or Y1 <= Y0 or img is None:
            return
        im = img.resize((X1 - X0, Y1 - Y0), Image.LANCZOS)
        if color is not None:
            a = np.asarray(im, np.float32)
            a *= np.array(color, np.float32) / 255.0
            im = Image.fromarray(np.round(a).astype(np.uint8), "RGBA")
        self.im.alpha_composite(im, (X0, Y0))


def draw_texture(cv, T, tnode, x, y, w, h):
    tid = (tnode.text or "").strip()
    if not tid:
        return
    ta = tnode.attrib
    color = None
    if any(k in ta for k in "argb"):
        color = [float(ta.get(k, 255)) for k in "rgba"]
    rect = None
    if "width" in ta and tid not in T.descr:
        rect = tuple(float(ta.get(k, 0)) for k in ("x", "y", "width", "height"))
    if tid in T.descr or (rect is None and T.has(tid)):
        img = T.image(tid, rect)
        if w <= 0 or h <= 0:
            w, h = T.size(tid)
        cv.put(img, x, y, w, h, color)
    elif tid + "_back" in T.descr and tid + "_lt" in T.descr:
        draw_frame(cv, T, tid, x, y, w, h, color)
    elif tid + "_b" in T.descr and tid + "_e" in T.descr:
        draw_frameline(cv, T, tid, x, y, w, h, color, vertical=False)
    elif tid + "_e" in T.descr:
        cv.put(T.image(tid + "_e"), x, y, w, h, color)
    elif rect is not None:
        cv.put(T.image(tid, rect), x, y, w, h, color)


def draw_frame(cv, T, tid, x, y, w, h, color=None):
    sz = {p: T.size("%s_%s" % (tid, p)) for p in FRAME}
    lw, th = sz["lt"]
    rw, bh = sz["rb"]
    cx, cy = x + lw, y + th
    cw, ch = max(0, w - lw - rw), max(0, h - th - bh)
    parts = {"lt": (x, y, lw, th), "t": (cx, y, cw, th), "rt": (cx + cw, y, rw, th),
             "l": (x, cy, lw, ch), "back": (cx, cy, cw, ch), "r": (cx + cw, cy, rw, ch),
             "lb": (x, cy + ch, lw, bh), "b": (cx, cy + ch, cw, bh), "rb": (cx + cw, cy + ch, rw, bh)}
    for p, r in parts.items():
        cv.put(T.image("%s_%s" % (tid, p)), *r, color=color)


def draw_frameline(cv, T, tid, x, y, w, h, color=None, vertical=False):
    bw, _ = T.size(tid + "_b")
    ew, _ = T.size(tid + "_e")
    cv.put(T.image(tid + "_b"), x, y, bw, h, color)
    cv.put(T.image(tid + "_back"), x + bw, y, max(0, w - bw - ew), h, color)
    cv.put(T.image(tid + "_e"), x + w - ew, y, ew, h, color)


def fnum(a, k):
    try:
        return float(a.get(k, 0) or 0)
    except ValueError:
        return 0.0


def load_xml(T, name):
    src = T.cfg.find("configs/ui/" + name)
    t = src.read().decode("cp1251", "replace")
    t = re.sub(r"<\?xml[^>]*\?>", "", t)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+);)", "&amp;", t)
    return ET.fromstring("<root>%s</root>" % t), src.label


def node(root, path):
    n = root
    for part in path.split(":"):
        nxt = None
        for c in n:
            if c.tag == part:
                nxt = c
                break
        if nxt is None:
            for c in n:          # the XML's own root (e.g. <w>)
                for cc in c:
                    if cc.tag == part:
                        nxt = cc
                        break
                if nxt is not None:
                    break
        if nxt is None:
            raise KeyError(path)
        n = nxt
    return n


def offset(root, path):
    x = y = 0.0
    parts = path.split(":")
    for i in range(1, len(parts)):
        a = node(root, ":".join(parts[:i])).attrib
        x, y = x + fnum(a, "x"), y + fnum(a, "y")
    return x, y


def draw_tree(cv, T, n, x, y, skip):
    a = n.attrib
    x, y = x + fnum(a, "x"), y + fnum(a, "y")
    w, h = fnum(a, "width"), fnum(a, "height")
    for c in n:
        if c.tag == "texture":
            draw_texture(cv, T, c, x, y, w, h)
    for c in n:
        if c.tag in ("texture", "text", "font", "texture_color"):
            continue
        if skip and re.search(skip, c.tag):
            continue
        draw_tree(cv, T, c, x, y, skip)


def draw(cv, T, root, paths, skip=None):
    """paths: XML paths whose subtrees are drawn, in order (parents first).
    An entry may be (path, dx, dy) to place it as the script parents it."""
    for p in paths:
        dx = dy = 0.0
        if isinstance(p, tuple):
            p, dx, dy = p
        px, py = offset(root, p)
        draw_tree(cv, T, node(root, p), px + dx, py + dy, skip)


# What ui_inventory.script (G.A.M.M.A. Accurate Defense Values) shows per mode
# (its UpdateMode): bags, trash, npc and trade panels.
_STATE = r"^(highlight|upgrade|blocker_\d+|active|cell$|hint_wnd|trash|bar|progress|state_progress)$"
SCREENS = {
    "inventory": ("ui_inventory_16.xml", ["player:background", "player:icon", "player:community_overlay",
                                          "player:rank_icon", "player:community_icon",
                                          "equipment"]),
    "loot": ("ui_inventory_16.xml", ["player:background", "player:icon", "player:community_overlay",
                                     "player:rank_icon", "player:community_icon", "player:putall_button",
                                     "equipment",
                                     "npc:background", "npc:icon", "npc:takeall_button"]),
    "trade": ("ui_inventory_16.xml", ["player:background", "player:icon", "player:community_overlay",
                                      "player:rank_icon", "player:community_icon", "player:trade_delimiter",
                                      "equipment",
                                      "npc:background", "npc:icon", "npc:trade_delimiter"]),
}


def render_xml(name, ours=False, T=None, skip=_STATE, only=None):
    """Every element of a layout file with art, parents first (what a script
    usually shows; state-only parts like highlights are skipped). `only`:
    top-level element names to draw."""
    T = T or Textures(ours)
    root, _ = load_xml(T, name)
    cv = Canvas()
    for top in root:                     # the file's own root(s), e.g. <w>
        kids = list(top) if len(top) else [top]
        for n in kids:
            if only and n.tag not in only:
                continue
            if skip and re.search(skip, n.tag):
                continue
            draw_tree(cv, T, n, 0.0, 0.0, skip)
    return cv.im.convert("RGB")


def render(screen, ours=False, T=None):
    T = T or Textures(ours)
    name, paths = SCREENS[screen]
    root, label = load_xml(T, name)
    cv = Canvas()
    ok = []
    for p in paths:
        try:
            node(root, p)
            ok.append(p)
        except KeyError:
            print("  (no %s in %s)" % (p, name))
    draw(cv, T, root, ok, skip=_STATE)
    return cv.im.convert("RGB")


def used(screen, T=None):
    """Texture ids a screen draws, with where each comes from."""
    T = T or Textures()
    name, paths = SCREENS[screen]
    root, _ = load_xml(T, name)
    out = {}
    for p in paths:
        try:
            n = node(root, p)
        except KeyError:
            continue
        for t in n.iter("texture"):
            tid = (t.text or "").strip()
            if not tid:
                continue
            key = tid if tid in T.descr else next((tid + s for s in ("_lt", "_b", "_e") if tid + s in T.descr), tid)
            f = T.descr[key][0] if key in T.descr else tid.replace("\\", "/").lower()
            _, lab = T.path(f)
            out[tid] = (f, T.descr.get(key, (None, None))[1], lab, T.where.get(key))
    return out


if __name__ == "__main__":
    screen = sys.argv[1] if len(sys.argv) > 1 else "inventory"
    ours = "--ours" in sys.argv
    out = os.path.join(HERE, "out", "preview", "ui")
    os.makedirs(out, exist_ok=True)
    im = render(screen, ours)
    p = os.path.join(out, "%s-%s.png" % (screen, "umbra" if ours else "gamma"))
    im.save(p)
    print(p)
