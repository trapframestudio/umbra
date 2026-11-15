"""
The game's bitmap fonts: read them, draw text with them, and build new ones
from an open-licence typeface in exactly the same format.

A game font is two files in textures/ui (never configs, so no co-op
fingerprint is touched):

    ui_font_<name>_<res>.dds   glyph sheet: white glyphs, coverage in alpha (DXT5)
    ui_font_<name>_<res>.ini   [width_correction] value = -1.0
                               [symbol_coords] height = <line height in px>
                               NNN = x1, y1, x2, y2   for each cp1251 code 000..255

The engine reads x1, y1, x2 (the box is `height` tall), draws the box 1:1 and
advances by (x2 - x1) + width_correction. configs/fonts.ltx maps the UI's font
names to these files per resolution (_800, _1024, _1600; 1080p uses _1600).

    <venv>/bin/python -I fonts.py build      the chosen fonts into out/full (CHOSEN)
    <venv>/bin/python -I fonts.py previews [key ...]
        each candidate on the main menu, the loading screen and a text sample,
        plus comparison sheets, in out/preview/fonts/ (keys: see CANDIDATES)
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FONTS = os.path.expanduser("~/.cache/umbra-look/fonts")
OUT = os.path.join(HERE, "out")


# ---------------------------------------------------------------- reading
class BitmapFont:
    def __init__(self, sheet_rgba, ini_text):
        self.sheet = sheet_rgba
        self.ini = ini_text
        self.height, self.wc, self.boxes = parse_ini(ini_text)

    @classmethod
    def from_game(cls, v, name):
        import dds
        ini = v.find("textures/ui/%s.ini" % name).read().decode("cp1251", "replace")
        sheet = dds.read(v.find("textures/ui/%s.dds" % name).read())
        return cls(sheet, ini)

    def width(self, text):
        return sum(self.boxes.get(c, (0, 0, 0, 0))[2] - self.boxes.get(c, (0, 0, 0, 0))[0] + self.wc
                   for c in text.encode("cp1251", "replace"))

    def draw(self, canvas, x, y, text, color):
        """Draw like the engine: each glyph's box 1:1, white glyph tinted by color (RGB 0..255)."""
        col = np.array(color, np.float32) / 255.0
        H = self.height
        for c in text.encode("cp1251", "replace"):
            box = self.boxes.get(c)
            if not box:
                continue
            x1, y1, x2, _ = box
            w = x2 - x1
            if w > 0:
                g = self.sheet[y1:y1 + H, x1:x2].astype(np.float32) / 255.0
                a = g[..., 3:4]
                rgb = g[..., :3] * col
                ys, xs = slice(int(y), int(y) + g.shape[0]), slice(int(x), int(x) + g.shape[1])
                dst = canvas[ys, xs]
                h_, w_ = dst.shape[:2]
                dst[...] = dst * (1 - a[:h_, :w_]) + rgb[:h_, :w_] * a[:h_, :w_]
            x += w + self.wc
        return x


def parse_ini(text):
    height, wc, boxes, section = 0, 0.0, {}, ""
    for line in text.splitlines():
        line = line.split(";")[0].strip()
        if line.startswith("["):
            section = line.strip("[] ").lower()
            continue
        if "=" not in line:
            continue
        k, v = (s.strip() for s in line.split("=", 1))
        if section == "width_correction" and k == "value":
            wc = float(v)
        elif section == "symbol_coords":
            if k == "height":
                height = int(float(v))
            elif k.isdigit():
                nums = [int(float(n)) for n in v.replace(" ", "").split(",") if n]
                if len(nums) >= 3:
                    boxes[int(k)] = tuple((nums + [0])[:4])
    return height, int(wc), boxes


def ink_rows(font, ch):
    """(top, bottom) rows of a glyph's ink inside its box, from the sheet's alpha."""
    x1, y1, x2, _ = font.boxes[ch.encode("cp1251")[0]]
    a = font.sheet[y1:y1 + font.height, x1:x2, 3]
    rows = np.where(a.max(axis=1) > 96)[0]
    return int(rows[0]), int(rows[-1])


# ---------------------------------------------------------------- building
# A face is (file under ~/.cache/umbra-look/fonts, {axis name: value}) so a
# variable font can be used at any weight or width.

def open_face(face, size):
    path, axes = face[:2]
    f = ImageFont.truetype(os.path.join(FONTS, path), size)
    if axes:
        info = f.get_variation_axes()
        names = [a["name"].decode() if isinstance(a["name"], bytes) else a["name"] for a in info]
        f.set_variation_by_axes([axes.get(n, a["default"]) for n, a in zip(names, info)])
    return f


# Draw Cyrillic in its Ukrainian forms (a face's optional third item overrides
# the language tag; see CANDIDATES).
LANG = "uk"
# the cp1251 glyphs that reach highest and lowest: they must fit in the game's box
TALL, DEEP = "ЇЙЁЎЃЌ", "gjpqyЩДЦуф"


def lang(face):
    return face[2] if len(face) > 2 else LANG


def build(face, height, cap, baseline, sheet=512, track=0.0):
    """
    A game font from a typeface. cap: cap height in px (match the font it
    replaces so text keeps its size); baseline: row of the baseline inside the
    box; track: px added to every advance (negative tightens). Each glyph is
    drawn clipped to its own box, so nothing bleeds into a neighbour.
    Returns (sheet RGBA uint8, ini text).
    """
    f = open_face(face, _size_for_cap(face, cap))
    alpha = Image.new("L", (sheet, sheet), 0)
    lines = ["[width_correction]", "value = -1.0", "[symbol_coords]", "height = %d" % height]
    x, y = 1, 1
    for code in range(256):
        ch = bytes([code]).decode("cp1251", "replace")
        if code < 32 or ch == "�" or not ch.isprintable():
            lines.append("%03d = 0, 0, 0, 0" % code)
            continue
        adv = max(1, int(round(f.getlength(ch, language=lang(face)) + track)))
        w = adv + 1                      # the engine advances by w - 1
        if x + w + 1 > sheet:
            x, y = 1, y + height + 2
            if y + height > sheet:
                raise ValueError("sheet too small: %d for height %d" % (sheet, height))
        cell = Image.new("L", (w, height), 0)
        ImageDraw.Draw(cell).text((0, baseline), ch, fill=255, font=f, anchor="ls", language=lang(face))
        alpha.paste(cell, (x, y))
        lines.append("%03d = %d, %d, %d, %d" % (code, x, y, x + w, y + height))
        x += w + 2
    rgba = np.zeros((sheet, sheet, 4), np.uint8)
    rgba[..., :3] = 255
    rgba[..., 3] = np.asarray(alpha)
    return rgba, "\r\n".join(lines) + "\r\n"


def fitted(face, game_font, text):
    for sheet in (512, 1024, 2048):
        try:
            return _fitted(face, game_font, text, sheet)
        except ValueError:      # glyphs don't fit the sheet
            continue
    raise ValueError("font too big for a 2048 sheet")


def _fitted(face, game_font, text, sheet):
    """build() matched to a game font: same box height, and text about as wide
    as the game font's. The cap height is the game font's, or up to 3 px taller
    for a narrow face, or smaller (spacing tightened by up to half a pixel
    first) for a wide one. The baseline stays where the game font's is unless
    accents (Ї, Й) or descenders need it moved to fit the box."""
    top, bottom = ink_rows(game_font, "H")
    cap, base, H = bottom - top + 1, bottom + 1, game_font.height
    target = game_font.width(text)
    made = None
    for c in range(cap + 3, cap - 4, -1):
        bl = _baseline(face, c, base, H)
        if bl is None:
            continue
        for track in (0.0, -0.25, -0.5):
            made = BitmapFont(*build(face, H, c, bl, sheet, track))
            if made.width(text) <= target * 1.02:
                return made
    return made


def _baseline(face, cap, base, height):
    """The baseline nearest `base` that keeps the tallest and deepest glyphs
    inside a box `height` tall, or None if they can't both fit."""
    f = open_face(face, _size_for_cap(face, cap))
    top = min(f.getbbox(ch, anchor="ls", language=lang(face))[1] for ch in TALL)
    bottom = max(f.getbbox(ch, anchor="ls", language=lang(face))[3] for ch in DEEP)
    lo, hi = -top, height - bottom
    return None if lo > hi else min(max(base, lo), hi)


def _size_for_cap(face, cap):
    """The px size whose 'H' is cap pixels tall."""
    best = None
    for size in np.arange(6, 120, 0.25):
        b = open_face(face, float(size)).getbbox("H", anchor="ls")
        h = b[3] - b[1]
        if best is None or abs(h - cap) < abs(best[1] - cap):
            best = (float(size), h)
        if h > cap + 2:
            break
    return best[0]


# ---------------------------------------------------------------- candidates
SLOTS = {   # the game's font names at 1080p, and what the UI uses them for
    "graff_32": "titles",
    "graff_19": "main menu buttons",
    "graff_22": "headers, item names",
    "letter_18": "most UI text, the loading screen",
    "letter_16": "descriptions",
    "arial_14": "small print, numbers",
}
# key: (label, body face, heading face); headings are the graff_* slots.
# All OFL with every cp1251 character, Ukrainian Ґґ Єє Іі Її included.
# Tried and rejected by the user (2026-11-15): Arsenal, Fira Sans Condensed.
# Left out: PT Sans (ParaType, a Russian foundry), IBM Plex Sans Condensed (no
# Cyrillic in its OFL build), Alumni Sans (no Ґґ).
CANDIDATES = {
    "oswald": ("Oswald (Vernon Adams)", ("oswald/Oswald[wght].ttf", {"Weight": 400}),
               ("oswald/Oswald[wght].ttf", {"Weight": 500})),
    # Sofia Sans draws Bulgarian shapes by default (т like m, и like u), also when
    # asked for Ukrainian; its standard East Slavic shapes, which Ukrainian uses,
    # are under the "ru" tag.
    "sofia": ("Sofia Sans Condensed (Lettersoup)", ("sofiasanscondensed/SofiaSansCondensed[wght].ttf", {"Weight": 450}, "ru"),
              ("sofiasanscondensed/SofiaSansCondensed[wght].ttf", {"Weight": 650}, "ru")),
    "roboto": ("Roboto Condensed (Christian Robertson)", ("robotocondensed/RobotoCondensed[wght].ttf", {"Weight": 400}),
               ("robotocondensed/RobotoCondensed[wght].ttf", {"Weight": 600})),
    "yanone": ("Yanone Kaffeesatz (Jan Gerner)", ("yanonekaffeesatz/YanoneKaffeesatz[wght].ttf", {"Weight": 400}),
               ("yanonekaffeesatz/YanoneKaffeesatz[wght].ttf", {"Weight": 600})),
    "tektur": ("Tektur Narrow (Adam Jagosz)", ("tektur/Tektur[wdth,wght].ttf", {"Width": 80, "Weight": 400}),
               ("tektur/Tektur[wdth,wght].ttf", {"Width": 80, "Weight": 600})),
    "play": ("Play (Jonas Hecksher)", ("play/Play-Regular.ttf", {}), ("play/Play-Bold.ttf", {})),
    "mix": ("Mix: Oswald headings, Sofia Sans body", ("sofiasanscondensed/SofiaSansCondensed[wght].ttf", {"Weight": 450}, "ru"),
            ("oswald/Oswald[wght].ttf", {"Weight": 500})),
    # the user's pick (2026-11-15): what `fonts.py build` ships
    "umbra": ("Umbra: Tektur Narrow headings, Play text", ("play/Play-Regular.ttf", {}),
              ("tektur/Tektur[wdth,wght].ttf", {"Width": 80, "Weight": 600})),
}
CHOSEN = "umbra"
# every font configs/fonts.ltx names for the UI, at each resolution the engine
# picks from (by screen height: <=600 _800, <1024 _1024, else _1600)
LTX_FONTS = ["arial_14", "arial_21", "graff_19", "graff_22", "graff_32", "graff_40", "graff_50",
             "letter_16", "letter_18", "letter_25"]
RESOLUTIONS = ["800", "1024", "1600"]
LICENSES = {"Tektur": "tektur/OFL.txt", "Play": "play/OFL.txt"}
PANGRAM = ("The quick brown fox jumps over the lazy dog 0123456789 "
           "Ґрунт їжак Європа Тінь Чорнобиля Выброс укрытие")


def candidates(v, keys=None):
    """{key: (label, {slot: BitmapFont})}: the game's fonts now, then each candidate fitted to them."""
    game = {s: BitmapFont.from_game(v, "ui_font_%s_1600" % s) for s in SLOTS}
    out = {"now": ("G.A.M.M.A. now: Tactical Fonts (CryoManne)", game)}
    for key in keys or CANDIDATES:
        label, body, head = CANDIDATES[key]
        out[key] = (label, {s: fitted(head if s.startswith("graff") else body, g, PANGRAM) for s, g in game.items()})
    return out


# ---------------------------------------------------------------- the fonts on real screens
# 1080p 16:9: the UI's 1024x768 units are 1.875 px wide and 1.40625 px tall.
KX, KY = 1920 / 1024, 1080 / 768
SAMPLE = [
    ("graff_32", "BAR - 100 RADS", (217, 204, 166)),
    ("graff_22", "AKM-74/2 Assault Rifle", (217, 204, 166)),
    ("letter_16", "A modernised Kalashnikov: reliable, cheap to repair", (190, 190, 180)),
    ("letter_16", "and found in every corner of the Zone.", (190, 190, 180)),
    ("arial_14", "Weight 3.40 kg    Condition 87%    Price 12,450 RU", (160, 160, 150)),
    ("letter_18", "Sell    Repair    Drop    Unload magazine", (220, 220, 210)),
    ("letter_18", "Ukrainian: Ґрунт, їжак, Європа, Тінь Чорнобиля", (220, 220, 210)),
    ("letter_18", "Russian UI text: Выброс! Ищите укрытие.", (220, 220, 210)),
]


def to_canvas(img):
    return np.asarray(img.convert("RGB"), np.float32) / 255.0


def from_canvas(a):
    return Image.fromarray(np.round(np.clip(a, 0, 1) * 255).astype(np.uint8))


def draw_menu(canvas, F, v, labels, selected=0):
    """ui_mm_main_16.xml: shniaga_wnd at (394,419), buttons_region x 8 y 3 w 220,
    btn_height 34, vert_interval 4, font graffiti19, colour 240; the slider
    (ui_inGame2_slider, 219x61) sits on the selected button."""
    import dds
    ours = os.path.join(OUT, "full", "gamedata", "textures", "ui", "ui_actor_main_menu.dds")
    if os.path.exists(ours):            # the layer's own atlas wins, as it will in game
        sheet = dds.read(open(ours, "rb").read())
    else:
        sheet = dds.read(v.find("textures/ui/ui_actor_main_menu.dds").read())
    sl = Image.fromarray(sheet[773:773 + 61, 5:5 + 274], "RGBA").resize((round(219 * KX), round(61 * KY)), Image.LANCZOS)
    rx, ry, rw = 394 + 8, 419 + 3, 220
    f = F["graff_19"]
    for i, text in enumerate(labels):
        by = (ry + i * (34 + 4)) * KY
        if i == selected:
            img = from_canvas(canvas)
            img.paste(sl, (round((394 + 7) * KX), round(by + 17 * KY - sl.height / 2)), sl)
            canvas[...] = to_canvas(img)
        w = f.width(text)
        f.draw(canvas, round((rx + rw / 2) * KX - w / 2), round(by + (34 * KY - f.height) / 2), text, (240, 240, 240))


def draw_loading(canvas, F, header, number, tip):
    """dxApplicationRender.cpp load_draw_internal: the system font
    (ui_font_letterica18_russian), grey 103, centred at x 960 from y 622 units,
    one line per font height; the tip wrapped at 600 units x 0.8 on widescreen."""
    f = F["letter_18"]
    lines = [header, "", number]
    cur, limit = "", 600 * KX * 0.8
    for wd in tip.split():
        t = (cur + " " + wd).strip()
        if cur and f.width(t) > limit:
            lines.append(cur)
            cur = wd
        else:
            cur = t
    lines.append(cur)
    y = 622 * KY
    for t in lines:
        f.draw(canvas, round(960 - f.width(t) / 2), round(y), t, (103, 103, 103))
        y += f.height
    return y


def draw_sample(F, W=640):
    H = 24 + sum(F[s].height + 10 for s, _, _ in SAMPLE)
    a = np.zeros((H, W, 3), np.float32)
    a[...] = np.array([0x2a, 0x2a, 0x22], np.float32) / 255
    y = 14
    for s, txt, col in SAMPLE:
        F[s].draw(a, 14, y, txt, col)
        y += F[s].height + 10
    return from_canvas(a)


def grid(cells, cols, path):
    """cells: [(label, image)] laid out cols across, labels above."""
    w = max(c[1].width for c in cells)
    h = max(c[1].height for c in cells)
    rows = (len(cells) + cols - 1) // cols
    img = Image.new("RGB", (cols * (w + 12) + 12, rows * (h + 44) + 12), (12, 12, 10))
    d = ImageDraw.Draw(img)
    for k, (label, cimg) in enumerate(cells):
        x, y = 12 + (k % cols) * (w + 12), 12 + (k // cols) * (h + 44)
        d.text((x, y), label, fill=(217, 204, 166), font=ImageFont.load_default(22))
        img.paste(cimg, (x, y + 32))
    img.save(path)


def previews(keys=None):
    import re
    import splash
    import vfs
    v = vfs.VFS(prefix="textures/")
    c = vfs.VFS(prefix="configs/")
    strings = {}
    for rel, src in c.winners("configs/text/eng/").items():
        for sid, txt in re.findall(r'<string\s+id="([^"]+)"\s*>\s*<text>(.*?)</text>',
                                   src.read().decode("cp1251", "replace"), re.S):
            strings[sid] = txt.strip()
    labels = [strings.get(i, i) for i in ("ui_mm_newgame", "ui_mm_load_game", "ui_mm_last_save", "ui_mm_options_n",
                                          "ui_mm_menu_mcm", "ui_mm_originals", "ui_mm_quit2windows")]
    header = splash.loading_header()
    number = strings.get("ls_tip_number", "") + "1"
    tip = strings.get("ls_tip_1", "")
    fonts = candidates(vfs.VFS(prefix="textures/ui/"), keys)
    menu = splash.preview_menu(splash.menu_still(v), splash.menu_logo(), splash.menu_badge())
    pic = splash.dds.read(v.find("textures/intro/intro_l01_escape_1.dds").read())
    loading = splash.preview_loading(splash.loading_plate(), pic, text=False)
    out = os.path.join(OUT, "preview", "fonts")
    os.makedirs(out, exist_ok=True)
    cm, cl, cs = [], [], []
    for n, (key, (label, F)) in enumerate(fonts.items()):
        label = "%d. %s" % (n, label) if n else label
        m = to_canvas(menu)
        draw_menu(m, F, v, labels)
        mi = from_canvas(m)
        mi.save(os.path.join(out, "menu-%s.png" % key))
        l = to_canvas(loading)
        draw_loading(l, F, header, number, tip)
        li = from_canvas(l)
        li.save(os.path.join(out, "loading-%s.png" % key))
        si = draw_sample(F)
        si.save(os.path.join(out, "text-%s.png" % key))
        cm.append((label, mi.crop((660, 560, 1260, 1000))))
        cl.append((label, li.crop((330, 850, 1590, 1075))))
        cs.append((label, si))
        print("%-8s %s" % (key, label))
    grid(cm, 4, os.path.join(out, "compare-menu.png"))
    grid(cl, 2, os.path.join(out, "compare-loading.png"))
    grid(cs, 4, os.path.join(out, "compare-text.png"))
    print(out)


def build_layer(key=CHOSEN):
    """Write the chosen fonts into out/full (for the Umbra mod): every UI font at every
    resolution, each fitted to the font the game has now (G.A.M.M.A.'s Tactical
    Fonts 1k), as uncompressed DDS + .ini in gamedata/textures/ui. The typefaces'
    OFL licences go next to gamedata."""
    import shutil
    import dds
    import vfs
    v = vfs.VFS(prefix="textures/ui/")
    label, body, head = CANDIDATES[key]
    mod = os.path.join(OUT, "full")
    ui = os.path.join(mod, "gamedata", "textures", "ui")
    os.makedirs(ui, exist_ok=True)
    total = 0
    for name in LTX_FONTS:
        for res in RESOLUTIONS:
            base = "ui_font_%s_%s" % (name, res)
            g = BitmapFont.from_game(v, base)
            made = fitted(head if name.startswith("graff") else body, g, PANGRAM)
            sheet = made.sheet
            dds.write_rgba(os.path.join(ui, base + ".dds"), sheet)
            with open(os.path.join(ui, base + ".ini"), "w", encoding="ascii", newline="") as f:
                f.write(made.ini)
            total += os.path.getsize(os.path.join(ui, base + ".dds"))
            print("  %-26s h%-3d %4dx%-4d width %3.0f%% of now (%s)" % (
                base, made.height, sheet.shape[1], sheet.shape[0], 100.0 * made.width(PANGRAM) / g.width(PANGRAM),
                v.find("textures/ui/%s.dds" % base).label))
    lic = os.path.join(mod, "licenses")
    os.makedirs(lic, exist_ok=True)
    for fam, path in LICENSES.items():
        shutil.copyfile(os.path.join(FONTS, path), os.path.join(lic, "%s-OFL.txt" % fam))
    print("%d fonts, %.1f MB, in %s" % (len(LTX_FONTS) * len(RESOLUTIONS), total / 1e6, ui))


if __name__ == "__main__":
    if sys.argv[1:2] == ["previews"]:
        previews(sys.argv[2:] or None)
    elif sys.argv[1:] == ["build"]:
        build_layer()
    else:
        print(__doc__)
