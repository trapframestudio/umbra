"""
The UI repaint: G.A.M.M.A.'s panels in Umbra's paint.

    python3 ui.py            build the repainted atlases into out/full
    python3 ui.py previews   before/after screens in out/preview/ui/

A "fresh coat of paint", literally: every panel keeps its shape, bevels,
depth, transparency and baked-in labels, and only its material changes.

1. structure(): G.A.M.M.A.'s skin is carbon-fibre hatching, CRT scanlines
   and scratched glass over the panel's shapes. Two ways to take it off:
   - "median" (most panels): a median filter wider than the hatch's period
     keeps the shapes (frames, slot boxes, bevel tabs) and drops the pattern.
   - "rows" (coarse diagonal hatch, like the trade bars): each row is
     averaged over exactly one horizontal period of the hatch, which cancels
     it completely. Horizontal edges and gradients are untouched, and
     vertical panel edges stop the averaging, so sub-panels stay crisp.
   Either way the labels, which filtering breaks, are put back whole from
   the original.
2. repaint(): the panel's own light and shadow (its luminance, unchanged) is
   carried over onto worn olive-black painted steel from paint.sheet(), with
   chips and rust where the panel's raised frames have edges. Light lettering
   comes out bone, as on the loading screen.

Only the cells listed in CELLS change; everything else in an atlas (icons,
rank badges) is copied as it is.
"""
import os
import re
import sys
import zlib

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dds       # noqa: E402
import paint     # noqa: E402
import uirender  # noqa: E402
import weather   # noqa: E402  (on paint's path)

MOD = os.path.join(HERE, "out", "full")
PREVIEW = os.path.join(HERE, "out", "preview", "ui")

STEEL = "#2c2b23"     # olive-black paint (a shade off the loading plate's PLATE)
BONE = "#d9cca6"


# ------------------------------------------------------------------ structure
def hatch_period(L, lo=5, hi=48):
    """The hatch's horizontal period in px: the strongest peak of the rows'
    autocorrelation (of the fine detail)."""
    blur = Image.fromarray(np.round(L * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(8))
    hp = L - np.asarray(blur, np.float64) / 255.0
    ac = np.fft.irfft(np.abs(np.fft.rfft(hp, axis=1)) ** 2, axis=1).mean(0)
    return int(np.argmax(ac[lo:hi]) + lo)


def vertical_edges(L, span=31, t=0.02):
    """Where a vertical panel edge lies between x-1 and x. The signed
    horizontal step is averaged down `span` px of column: a diagonal hatch
    flips sign along the column and cancels, a real edge keeps its sign."""
    g = np.zeros_like(L)
    g[:, 1:] = L[:, 1:] - L[:, :-1]
    c = np.cumsum(np.pad(g, ((span // 2, span // 2), (0, 0)), mode="edge"), 0)
    c = np.vstack([np.zeros((1, g.shape[1])), c])
    return np.abs((c[span:] - c[:-span]) / span) > t


def rows(rgb, p, edges):
    """Average each row over p px, never across an edge."""
    H, W = edges.shape
    xs = np.arange(W)
    r0, r1 = p // 2, p - 1 - p // 2
    out = np.empty_like(rgb)
    for y in range(H):
        e = edges[y]
        start = np.maximum.accumulate(np.where(e, xs, 0))
        nxt = np.r_[np.where(e, xs, W)[1:], W]
        end = np.minimum.accumulate(nxt[::-1])[::-1] - 1
        lo, hi = np.maximum(xs - r0, start), np.minimum(xs + r1, end)
        c = np.vstack([np.zeros((1, 3)), np.cumsum(rgb[y], 0)])
        out[y] = (c[hi + 1] - c[lo]) / (hi - lo + 1)[:, None]
    return out


def structure(rgb, size=7, soft=1.0, method="median"):
    """The panel without its surface pattern: RGB float in, RGB float out.
    median: `size` px (wider than the hatch's period) then a light blur,
    which smooths the steps an 8-bit median leaves in gradients.
    rows: see the module docstring."""
    L = _lum(rgb)
    if method == "rows":
        flat = rows(rgb, hatch_period(L), vertical_edges(L))
    else:
        u8 = Image.fromarray(np.round(np.clip(rgb, 0, 1) * 255).astype(np.uint8))
        flat = u8.filter(ImageFilter.MedianFilter(size)).filter(ImageFilter.GaussianBlur(soft))
        flat = np.asarray(flat, np.float64) / 255.0
    # labels and glints: much brighter than anything the filter kept; put
    # them back as whole letter shapes, with their own anti-aliasing
    Lf = _lum(flat)
    marks = Image.fromarray(((L - Lf > 0.25) & (L > 0.5)).astype(np.uint8) * 255)
    keep = marks.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(0.7))
    keep = (np.asarray(keep, np.float64) / 255.0)[..., None]
    return flat * (1 - keep) + rgb * keep


# ------------------------------------------------------------------ repaint
def _lum(rgb):
    return rgb @ np.array([0.2126, 0.7152, 0.0722])


def repaint(cell, px=1.33, seed=1, cover=0.10, size=7, soft=1.0, method="median", material=True, grime=1.2):
    """cell: RGBA uint8 (an atlas cell). Returns it repainted (alpha kept).
    cover: how much of the raised frames' paint is chipped; method, size,
    soft: how the hatch comes off (structure()); grime: how dirty the
    recesses are.
    material=False only recolours (olive and bone, the hatch taken off): for
    frame parts and small widgets the game stretches, where wear would smear.
    Recolouring alone doesn't filter: small widgets keep their thin outlines."""
    rgba = cell.astype(np.float64) / 255.0
    rgb, alpha = rgba[..., :3], rgba[..., 3]
    clean = structure(rgb, size, soft, method) if material else rgb
    L = _lum(clean)
    steel = weather.hex_rgb(STEEL)
    ratio = 1.0
    if material:
        # broad bright metal (a brushed-silver PDA bezel) is painted too: tone
        # it down to a light olive; fine marks on it keep their contrast
        Lb = weather.blur(L.astype(np.float32), 4.0 * px).astype(np.float64)
        L = L - 0.7 * np.clip(Lb - 0.38, 0, None)
        ratio = _material(L, px, seed, cover, grime)

    # carry the panel's luminance over in the paint's hue; light parts
    # (lettering, glints) run to bone instead of white
    tint = steel / _lum(steel[None, None, :])[0, 0]
    bone = weather.hex_rgb(BONE)
    lit = weather.smoothstep(0.45, 0.85, L)[..., None]
    new = L[..., None] * tint[None, None, :] * ratio
    new = new * (1 - lit) + (bone * np.clip(L / _lum(bone[None, None, :])[0, 0], 0, 1.15)[..., None]) * lit

    # colour stays as it was: a campfire picture, a blood splash, a coloured
    # light sharing a cell with the panel
    mx, mn = rgb.max(-1), rgb.min(-1)
    chroma = mx - mn
    colour = weather.smoothstep(0.25, 0.4, chroma / np.maximum(mx, 1e-6)) * weather.smoothstep(0.08, 0.16, chroma)
    colour = np.asarray(Image.fromarray(np.round(colour * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(0.8)), np.float64)[..., None] / 255.0
    new = new * (1 - colour) + rgb * colour
    out = np.concatenate([np.clip(new, 0, 1), alpha[..., None]], -1)
    return np.round(out * 255).astype(np.uint8)


_GRIME = []


def _grime_tex():
    """The grime texture for panels: the painted lower half of a base-game
    wall, without the plaster above its dado line (that line ran across big
    plates as a stripe). Its ends are cross-faded so it tiles (a mirror
    made symmetric blots)."""
    if not _GRIME:
        t = weather.T("wall\\wall_walls_paint_01")
        n = t.shape[0]
        body = t[int(0.28 * n):int(0.72 * n)]
        h = body.shape[0]
        k = h // 4
        ramp = np.linspace(0, 1, k, dtype=np.float32)[:, None, None]
        out = body[:h - k].copy()
        out[:k] = body[h - k:] * (1 - ramp) + body[:k] * ramp
        _GRIME.append(np.ascontiguousarray(out))
    return _GRIME[0]


def _material(L, px, seed, cover, grime=1.2):
    """The worn paint's variation (~1 around STEEL) for a panel of luminance L."""
    # raised frames are the lighter, broad parts of the panel; paint only
    # chips along their edges (a band about 12 px wide), never across the
    # middle of a big plate, nor on thin highlight lines (an opening drops
    # anything under ~5 px wide)
    Ls = weather.blur(L.astype(np.float32), 2.0 * px)
    frame = weather.smoothstep(0.13, 0.22, Ls)
    f8 = Image.fromarray(np.round(frame * 255).astype(np.uint8))
    frame = np.asarray(f8.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5)),
                       np.float32) / 255.0
    core = weather.smoothstep(0.8, 0.97, weather.blur(frame.astype(np.float32), 9.0 * px))
    band = frame * (1 - core)

    steel = weather.hex_rgb(STEEL)
    worn = paint.sheet(np.clip(frame + 0.0001, 0, 1).astype(np.float32), STEEL, px=px, cover=cover, chip=0.5,
                       edge=3.0, edge_bias=1.7, rust_tone=0.55, flakes=0.05, grime=1.0, seed=seed,
                       grime_tex=_grime_tex())
    # recesses (where items sit) get grime but no chips: rust specks there
    # read as blood spots
    inner = paint.sheet(np.ones_like(frame, np.float32), STEEL, px=px, cover=0.0, chip=0.5,
                        rust_tone=0.45, flakes=0.0, grime=grime, seed=seed + 7, grime_tex=_grime_tex())
    mat = worn * band[..., None] + inner * (1 - band[..., None])
    return mat / steel[None, None, :]


# ------------------------------------------------------------------ currency
# G.A.M.M.A. bakes a ruble sign into the money box and the trade bars. Umbra
# never shows Russian state symbols (the user: "swap in something that isn't
# rubles"), and the Zone is in Ukraine, so the mark is the hryvnia, in Play
# (our text face; OFL, credited as font-play). Regular matches the ruble's
# stroke; Bold came out heavier.
CURRENCY = "\u20b4"
CURRENCY_FONT = os.path.expanduser("~/.cache/umbra-look/fonts/play/Play-Regular.ttf")
RUBLES = {      # cell: the ruble glyphs' boxes in it (x0, y0, x1, y1)
    "ui_inGame2_left_side_16": [(608, 163, 624, 184)],
    "ui_inGame2_center_trade_devider": [(649, 170, 664, 193)],
    "ui_inGame2_center_trade_devider_16": [(887, 170, 903, 193)],
}


def swap_currency(cell, boxes):
    """Erase each ruble glyph (the panel under it comes from a wide median)
    and letter the hryvnia in its place, same height and colour."""
    from PIL import ImageDraw, ImageFont
    out = cell.copy()
    rgb = cell[..., :3]
    med = np.asarray(Image.fromarray(rgb).filter(ImageFilter.MedianFilter(15)))
    L = _lum(rgb.astype(np.float64) / 255.0)
    for x0, y0, x1, y1 in boxes:
        glyph = L[y0:y1, x0:x1]
        if (glyph > 0.6).sum() < 80:      # the art moved: don't paint blind
            raise SystemExit("ui.py: no ruble glyph at %s; G.A.M.M.A.'s art changed, update RUBLES"
                             % ((x0, y0, x1, y1),))
        ink = rgb[y0:y1, x0:x1][glyph > 0.6]
        color = tuple(int(v) for v in np.percentile(ink, 90, axis=0))
        p = 4
        out[y0 - p:y1 + p, x0 - p:x1 + p, :3] = med[y0 - p:y1 + p, x0 - p:x1 + p]
        # the hryvnia, its ink box the ruble's height, centred where it was
        h = y1 - y0
        k = 4                                  # drawn large, then reduced
        font = ImageFont.truetype(CURRENCY_FONT, h * k * 1.3)
        bb = font.getbbox(CURRENCY)
        scale = h * k / (bb[3] - bb[1])
        font = ImageFont.truetype(CURRENCY_FONT, round(h * k * 1.3 * scale))
        bb = font.getbbox(CURRENCY)
        gw, gh = bb[2] - bb[0], bb[3] - bb[1]
        mask = Image.new("L", (gw + 2 * k, gh + 2 * k), 0)
        ImageDraw.Draw(mask).text((k - bb[0], k - bb[1]), CURRENCY, font=font, fill=255)
        mask = mask.resize((round(mask.width / k), round(mask.height / k)), Image.LANCZOS)
        mx = round((x0 + x1) / 2 - mask.width / 2)
        my = round((y0 + y1) / 2 - mask.height / 2)
        a = (np.asarray(mask, np.float64) / 255.0)[..., None]
        region = out[my:my + mask.height, mx:mx + mask.width, :3].astype(np.float64)
        out[my:my + mask.height, mx:mx + mask.width, :3] = np.round(
            region * (1 - a) + np.array(color, np.float64) * a).astype(np.uint8)
    return out


# ------------------------------------------------------------------ build
# The inventory/trade/loot/repair panels (ui_inventory_16.xml and the 4:3
# ui_inventory.xml), in G.A.M.M.A. UI's ui_actor_menu atlas (the winning copy
# is G.A.M.M.A. Accurate Defense Values').
PANEL = {}                                     # the defaults
CELLS = {
    "ui/ui_actor_menu": {
        "ui_inGame2_repair_panel_16": PANEL, "ui_inGame2_repair_panel": PANEL,
        "ui_inGame2_inventory_back": PANEL,
        "ui_inGame2_left_side": PANEL, "ui_inGame2_left_side_16": PANEL,
        # the trade bars' hatch is coarse and diagonal: a median wide enough
        # smears their sub-panels, the row filter keeps them crisp
        "ui_inGame2_center_trade_devider": {"method": "rows"},
        "ui_inGame2_center_trade_devider_16": {"method": "rows"},
        # a lit, empty plate under the quick slots: hatch over a fine square
        # grid, and nothing to keep but its border and glow; no chips (the
        # glow isn't a raised frame)
        "ui_item_picker_background": {"size": 21, "soft": 4.0, "cover": 0.0},
        "ui_inGame2_artefact_blocker": PANEL,
        "ui_inGame2_big_inventory_button_e": PANEL, "ui_inGame2_big_inventory_button_h": PANEL,
        "ui_inGame2_big_inventory_button_t": PANEL,
    },
}


# Every other sheet of G.A.M.M.A.'s skin: its cells are picked by pick().
# skip: ids to leave alone that pick() would take.
AUTO = {
    "ui/ui_common": {"skip": r"rust|options_tv|frame_error|newPDA_Crcl"},   # CoP's rust trims, the TV, glass discs
    "ui/ui_options": {},                       # options, MCM and PDA pages
    "ui/ui_actor_multiplayer_game_menu": {},   # message boxes, lists, chat
    "ui/ui_actor_hint_wnd": {},                # tooltips, load/save window
    "ui/ui_actor_sleep_screen": {},            # message box, sleep dialog
    "ui/ui_actor_pda": {},                     # PDA
    "ui/items/ui_item_1": {},                  # cooking, item panels
    "ui/items/ui_details": {},                 # item details
    # crafting: one big plate; at the default its whole bottom lip rusted
    "ui/items/ui_workshop": {"opts": {"ui_inGame2_workshop_background": {"cover": 0.05}}},
    "ui/ui_actor_dialog_screen": {},           # talking
    # mutant loot: the lit plate's glow isn't a raised frame (chips there
    # read as blood spots next to G.A.M.M.A.'s own splash)
    "ui/ui_loot_interface": {"opts": {"ui_loot_window": {"cover": 0.0}}},
    "ui/new_game/ui_new_game_popup": {},       # new game: faction screen
    "ui/new_game/ui_new_game_main": {},
    "ui/new_game/ui_new_game_back_t": {},
    # PDA tasks: the device. Its screen is glass: barely any grime
    "ui/ui_pda2": {"opts": {"ui_pda2": {"grime": 0.3}}},
    "ui/ui_actor_menu_new_tabs": {},           # inventory sort tabs
}


def _kind(cell, ids):
    """How to treat an auto cell: "paint", "tint" or None (leave it)."""
    a = cell[..., 3] / 255.0
    rgb = cell[..., :3] / 255.0
    op = a > 0.1
    if op.mean() < 0.02:
        return None
    px = rgb[op]
    mx = px.max(1)
    sat = np.where(mx > 0.04, (mx - px.min(1)) / np.maximum(mx, 1e-6), 0).mean()
    if sat > 0.22 or np.median(_lum(px)) > 0.6:
        return None                     # icons, patches, pictures; white glyphs and bars
    h, w = cell.shape[:2]
    frame = any(t.rpartition("_")[2] in uirender.FRAME for t in ids)
    if frame or min(h, w) < 40 or h * w < 96 * 96 or a[op].mean() < 0.6:
        return "tint"                   # stretched parts, small widgets, translucent overlays
    return "paint"


def pick(T, f, A, skip=None):
    """The cells of sheet `f` (array A) to repaint: [(rect, key, kind)],
    biggest first. Cells sharing a rect are one; key (the id without its
    button-state suffix) seeds the wear, so a button's states match."""
    H, W = A.shape[:2]
    rects = {}
    for tid, (ff, (x, y, w, h)) in T.descr.items():
        if ff != f:
            continue
        r = (max(0, round(x)), max(0, round(y)), min(W, round(x + w)), min(H, round(y + h)))
        if r[2] - r[0] >= 4 and r[3] - r[1] >= 4:
            rects.setdefault(r, []).append(tid)
    out = []
    for r, ids in rects.items():
        ids.sort()
        if skip and any(re.search(skip, t) for t in ids):
            continue
        kind = _kind(A[r[1]:r[3], r[0]:r[2]], ids)
        if kind:
            out.append((r, re.sub(r"_[ethd]$", "", ids[0]), kind))
    out.sort(key=lambda o: -(o[0][2] - o[0][0]) * (o[0][3] - o[0][1]))
    return out


def atlas(T, f):
    """The winning atlas `f` with its cells repainted (CELLS or AUTO)."""
    src, label = T.path(f)
    orig = dds.read(src.read() if not isinstance(src, str) else open(src, "rb").read())
    a = orig.copy()
    if f in CELLS:
        done = set()
        for i, (tid, opts) in enumerate(CELLS[f].items()):
            x, y, w, h = (int(v) for v in T.descr[tid][1])
            if (x, y, w, h) in done:
                continue
            done.add((x, y, w, h))
            cell = orig[y:y + h, x:x + w]
            if tid in RUBLES:
                cell = swap_currency(cell, RUBLES[tid])
            a[y:y + h, x:x + w] = repaint(cell, seed=11 + 7 * i, **opts)
        return a
    cfg = AUTO[f]
    written = np.zeros(a.shape[:2], bool)
    for (x0, y0, x1, y1), key, kind in pick(T, f, orig, cfg.get("skip")):
        new = repaint(orig[y0:y1, x0:x1], seed=zlib.crc32(key.encode()) % 100000,
                      material=(kind == "paint"), **cfg.get("opts", {}).get(key, {}))
        free = ~written[y0:y1, x0:x1]          # overlapping cells: the bigger one wins
        a[y0:y1, x0:x1][free] = new[free]
        written[y0:y1, x0:x1] = True
    return a


def build(sheets=None):
    T = uirender.Textures()
    for f in sheets or list(CELLS) + list(AUTO):
        out = os.path.join(MOD, "gamedata", "textures", f + ".dds")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        dds.write_rgba(out, atlas(T, f))
        print(out)


# Other screens, drawn whole from their layout files (every part at once:
# a script shows only some of them, so tabs and states overlap).
XML_SCREENS = {
    "crafting": "ui_workshop_16.xml", "cooking": "ui_items_cook_16.xml",
    "item-detail": "ui_item_detail_16.xml", "talk": "talk_16.xml", "pda": "pda_16.xml",
    "pda-tasks": "pda_tasks_16.xml", "message-box": "message_box_16.xml",
    "options": "ui_options_16.xml", "load": "ui_mm_load_dlg_16.xml",
    "mutant-loot": "ui_mutant_loot_16.xml", "new-game": "ui_mm_faction_select_16.xml",
    "mcm": "ui_mcm_16.xml", "upgrade": "inventory_upgrade_16.xml",
}


def previews(screens=("inventory", "loot", "trade") + tuple(XML_SCREENS)):
    os.makedirs(PREVIEW, exist_ok=True)
    before, after = uirender.Textures(), uirender.Textures(ours=True)
    for s in screens:
        if s in XML_SCREENS:
            try:
                a = uirender.render_xml(XML_SCREENS[s], T=before)
                b = uirender.render_xml(XML_SCREENS[s], T=after)
            except Exception as e:             # a layout the renderer can't read
                print("  %s: %s" % (s, e))
                continue
        else:
            a, b = uirender.render(s, T=before), uirender.render(s, T=after)
        a.save(os.path.join(PREVIEW, "%s-gamma.png" % s))
        b.save(os.path.join(PREVIEW, "%s-umbra.png" % s))
        c = Image.new("RGB", (a.width, a.height * 2 + 8), (0, 0, 0))
        c.paste(a, (0, 0))
        c.paste(b, (0, a.height + 8))
        c.save(os.path.join(PREVIEW, "compare-%s.png" % s))
        print(os.path.join(PREVIEW, "compare-%s.png" % s))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["previews"]:
        previews(tuple(args[1:]) or None) if args[1:] else previews()
    else:
        build([a for a in args if "/" in a] or None)
        previews()
