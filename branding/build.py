#!/usr/bin/env python3
"""
Umbra logo, v3: late-Soviet propaganda lettering in faded Zone colours.

    python3 build.py          write the clean SVGs
    python3 build.py --png    also make the PNGs (weathered and clean) and the preview sheet

The logo is type: UMBRA in heavy, condensed block letters with an offset
rust print shadow, over a rust bar with the name in Cyrillic, УМБРА. The icon
is the U from it on a soot tile. (v1, a minimal eclipse, and v2, a pin badge,
are in archive/.)

All lettering is hand-built geometry (Latin UMBRA and Cyrillic УМБРА), so no
fonts are involved.

The SVGs are clean, flat colour. The weathered PNGs are painted from real
game textures: peeling paint, rust and grime from the base game's archives
(weather.py, textures.py). The icon's small sizes stay clean.
"""
import os
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "svg")
PNG = os.path.join(HERE, "png")

# Faded print on dirty paper
PAPER = "#cdbf99"
OCHRE = "#b0883b"      # the sky
OCHRE_D = "#95722f"    # sun rays
RUST = "#84301f"       # faded propaganda red
RUST_D = "#5a2116"
RUST_DD = "#3c160e"
SOOT = "#14130f"
OLIVE = "#3f4026"
BONE = "#d9cca6"
GLINT = "#efe5c4"
UMBER = "#2b251a"      # the eclipsed sun: dark, but not as dark as the plant
PLANT = "#0f0e0b"

# ---------------------------------------------------------------- lettering
# Heavy condensed block letters on a 200-unit cap height, ~38-unit strokes.
# Each letter: (width, [outline, counter, ...]); counters cut out (evenodd).
M_ = (136, [[(0, 200), (0, 0), (42, 0), (68, 86), (94, 0), (136, 0), (136, 200), (98, 200), (98, 92),
             (80, 152), (56, 152), (38, 92), (38, 200)]])
A_ = (120, [[(0, 200), (24, 0), (96, 0), (120, 200), (82, 200), (76, 150), (44, 150), (38, 200)],
            [(57, 46), (63, 46), (71, 112), (49, 112)]])
GLYPHS = {
    "U": (112, [[(0, 0), (40, 0), (40, 160), (72, 160), (72, 0), (112, 0), (112, 180), (92, 200), (20, 200), (0, 180)]]),
    "M": M_,
    "B": (112, [[(0, 0), (88, 0), (112, 24), (112, 80), (100, 100), (112, 120), (112, 176), (88, 200), (0, 200)],
                [(38, 34), (74, 34), (74, 82), (38, 82)], [(38, 118), (74, 118), (74, 166), (38, 166)]]),
    "R": (112, [[(0, 0), (88, 0), (112, 24), (112, 96), (96, 112), (112, 200), (72, 200), (58, 124), (38, 124),
                 (38, 200), (0, 200)], [(38, 34), (74, 34), (74, 90), (38, 90)]]),
    "A": A_,
    "У": (112, [[(0, 0), (38, 0), (38, 80), (74, 80), (74, 0), (112, 0), (112, 176), (88, 200), (10, 200), (10, 166),
                 (74, 166), (74, 114), (24, 114), (0, 90)]]),
    "М": M_,
    "Б": (112, [[(0, 0), (108, 0), (108, 34), (38, 34), (38, 80), (88, 80), (112, 104), (112, 176), (88, 200), (0, 200)],
                [(38, 114), (74, 114), (74, 166), (38, 166)]]),
    "Р": (112, [[(0, 0), (88, 0), (112, 24), (112, 100), (88, 124), (38, 124), (38, 200), (0, 200)],
                [(38, 34), (74, 34), (74, 90), (38, 90)]]),
    "А": A_,
    # for the launcher's PLAY button
    "L": (100, [[(0, 0), (38, 0), (38, 166), (100, 166), (100, 200), (0, 200)]]),
    "Y": (120, [[(0, 0), (40, 0), (60, 70), (80, 0), (120, 0), (79, 116), (79, 200), (41, 200), (41, 116)]]),
}
GLYPHS["P"] = GLYPHS["Р"]
CAP = 200


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def text_width(word, track):
    return sum(GLYPHS[c][0] for c in word) + track * (len(word) - 1)


def glyph_polys(word, x0, y0, s, track=16):
    """[(outer, [counters])] in logo units."""
    out, x = [], 0.0
    for c in word:
        w, polys = GLYPHS[c]
        pts = [[(x0 + (x + px) * s, y0 + py * s) for px, py in p] for p in polys]
        out.append((pts[0], pts[1:]))
        x += w + track
    return out


def rect_polys(x, y, w, h):
    return [([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], [])]


def svg_path(polys, fill):
    d = []
    for outer, holes in polys:
        for p in [outer] + holes:
            d.append("M" + " L".join(f"{f(px)},{f(py)}" for px, py in p) + " Z")
    return f'<path fill="{fill}" fill-rule="evenodd" d="{" ".join(d)}"/>'


# ---------------------------------------------------------------- the pieces
# Each lockup is (W, H, layers); a layer is (polys, colour, material, wear).
# material: how the paint wears (weather.MATERIALS); wear: share of the shape
# where paint is gone.

def logo(light_ground=False):
    """UMBRA with an offset print shadow, over a bar with УМБРА."""
    x0 = y0 = 20
    tw = text_width("UMBRA", 16)
    sh = 12
    by = y0 + CAP + sh + 26
    bh = 84
    cs = 0.3
    cw = text_width("УМБРА", 60) * cs
    ink, mat = (SOOT, "dark") if light_ground else (BONE, "pale")
    layers = [
        (glyph_polys("UMBRA", x0 + sh, y0 + sh, 1), RUST, "red", 0.30),
        (glyph_polys("UMBRA", x0, y0, 1), ink, mat, 0.17),
        (rect_polys(x0, by, tw + sh, bh), RUST, "red", 0.22),
        (glyph_polys("УМБРА", x0 + (tw + sh - cw) / 2, by + (bh - CAP * cs) / 2, cs, 60), PAPER, "pale", 0.10),
    ]
    return tw + sh + 2 * x0, CAP + sh + 26 + bh + 2 * y0, layers


def name():
    """UMBRA alone, no bar: for tight horizontal spaces."""
    tw = text_width("UMBRA", 16)
    layers = [
        (glyph_polys("UMBRA", 32, 32, 1), RUST, "red", 0.30),
        (glyph_polys("UMBRA", 20, 20, 1), BONE, "pale", 0.17),
    ]
    return tw + 52, CAP + 52, layers


def icon():
    """The U from the wordmark, print shadow and all, over a bar, on a soot tile."""
    S = 256
    s = 0.86
    w, h = (112 + 12) * s, (CAP + 12) * s
    x0, y0 = (S - w) / 2, 14
    by = y0 + h + 10
    layers = [
        (rect_polys(0, 0, S, S), SOOT, "dark", 0.10),
        (glyph_polys("U", x0 + 12 * s, y0 + 12 * s, s), RUST, "red", 0.22),
        (glyph_polys("U", x0, y0, s), BONE, "pale", 0.10),
        (rect_polys(22, by, S - 44, S - by - 14), RUST, "red", 0.18),
    ]
    return S, S, layers


def play():
    """PLAY, for the launcher's button: paper letters with a dark print shadow (the button is rust)."""
    sh = 14
    tw = text_width("PLAY", 18)
    layers = [
        (glyph_polys("PLAY", 8 + sh, 8 + sh, 1, 18), RUST_DD, "red", 0.0),
        (glyph_polys("PLAY", 8, 8, 1, 18), PAPER, "pale", 0.0),
    ]
    return tw + sh + 16, CAP + sh + 16, layers


def svg_doc(piece):
    W, H, layers = piece
    body = "".join(svg_path(p, colour) for p, colour, _, _ in layers)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(H)}" width="{f(W)}" height="{f(H)}">'
            f"<title>Umbra</title>{body}</svg>\n")


FILES = {
    "umbra-logo.svg": lambda: svg_doc(logo()),
    "umbra-logo-light-ground.svg": lambda: svg_doc(logo(light_ground=True)),
    "umbra-name.svg": lambda: svg_doc(name()),
    "umbra-icon.svg": lambda: svg_doc(icon()),
}

# weathered PNGs: (piece, file, width, seed); smaller sizes are downscaled from the first
WORN = [
    (logo, {}, [("umbra-logo-2400.png", 2400), ("umbra-logo-1280.png", 1280)], 7),
    (logo, dict(light_ground=True), [("umbra-logo-light-ground-1280.png", 1280)], 11),
    (name, {}, [("umbra-name-1600.png", 1600)], 5),
    (icon, {}, [("umbra-icon-512.png", 512), ("umbra-icon-256.png", 256)], 3),
]

def clean_png(piece, width, path):
    import weather
    W, H, layers = piece
    weather.flat(W, H, layers, width).save(path)


def worn_pngs():
    import weather
    for make, kw, outs, seed in WORN:
        W, H, layers = make(**kw)
        big = weather.composite(W, H, layers, outs[0][1], seed)
        big.save(os.path.join(PNG, outs[0][0]))
        for fn, w in outs[1:]:
            big.resize((w, round(big.height * w / big.width)), Image.LANCZOS).save(os.path.join(PNG, fn))


def preview():
    """One sheet on a dingy dark ground, composed from the rendered PNGs."""
    p = lambda n: os.path.join(PNG, n)
    tmp = p("_ground.png")
    subprocess.run(["magick", "-size", "1600x1200", f"xc:{SOOT}",
                    "(", "+clone", "+noise", "Random", "-colorspace", "gray", "-blur", "0x40", "-auto-level",
                    "-evaluate", "multiply", "0.10", ")", "-compose", "plus", "-composite",
                    "(", "+clone", "+noise", "Gaussian", "-colorspace", "gray", "-evaluate", "multiply", "0.05", ")",
                    "-compose", "plus", "-composite", tmp], check=True)
    subprocess.run(["magick", tmp,
                    "(", p("umbra-logo-2400.png"), "-resize", "1100x", ")", "-geometry", "+250+90", "-compose", "over", "-composite",
                    "(", "-size", "700x330", f"xc:{PAPER}", ")", "-geometry", "+70+790", "-composite",
                    "(", p("umbra-logo-light-ground-1280.png"), "-resize", "600x", ")", "-geometry", "+120+820", "-composite",
                    "(", p("umbra-name-1600.png"), "-resize", "520x", ")", "-geometry", "+880+780", "-composite",
                    "(", p("umbra-icon-256.png"), "-resize", "160x160", ")", "-geometry", "+900+1000", "-composite",
                    "(", p("umbra-icon-64.png"), ")", "-geometry", "+1100+1048", "-composite",
                    "(", p("umbra-icon-32.png"), ")", "-geometry", "+1200+1064", "-composite",
                    p("preview.png")], check=True)
    os.remove(tmp)


def main():
    os.makedirs(SVG, exist_ok=True)
    for fn, make in FILES.items():
        with open(os.path.join(SVG, fn), "w", encoding="utf-8") as fh:
            fh.write(make())
    print(f"wrote {len(FILES)} SVGs to {SVG}")
    if "--png" in sys.argv:
        os.makedirs(PNG, exist_ok=True)
        clean_png(logo(), 1280, os.path.join(PNG, "umbra-logo-clean-1280.png"))
        clean_png(icon(), 64, os.path.join(PNG, "umbra-icon-64.png"))
        clean_png(icon(), 32, os.path.join(PNG, "umbra-icon-32.png"))
        clean_png(play(), 104, os.path.join(PNG, "umbra-play-104.png"))
        worn_pngs()
        preview()
        print(f"rendered the PNGs and preview.png to {PNG}")


if __name__ == "__main__":
    main()
