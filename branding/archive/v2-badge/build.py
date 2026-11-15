#!/usr/bin/env python3
"""
Umbra logo, v2: a late-Soviet pin badge / propaganda poster in faded Zone colours.

    python3 build.py          write the SVGs
    python3 build.py --png    also render the PNGs and the preview sheet
                              (rsvg-convert, or the zgo-dev distrobox; magick for the sheet)

The badge: sunburst rays behind an eclipsed black sun (the umbra), the
reactor's striped ventilation stack in silhouette against it, a ribbon banner
with the name in heavy, condensed block letters. No hammer and sickle and no
red star: those are banned in Ukraine, and the look carries without them.

All lettering is hand-built geometry (Latin UMBRA and Cyrillic УМБРА), so no
fonts are involved. "Worn" files get flaked paint and rough edges; "clean"
files are for small sizes.
"""
import math
import os
import shutil
import subprocess
import sys

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
}
CAP = 200


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def text_width(word, track):
    return sum(GLYPHS[c][0] for c in word) + track * (len(word) - 1)


def text(word, x0, y0, s, fill, track=16):
    d, x = [], 0.0
    for c in word:
        w, polys = GLYPHS[c]
        for p in polys:
            d.append("M" + " L".join(f"{f(x0 + (x + px) * s)},{f(y0 + py * s)}" for px, py in p) + " Z")
        x += w + track
    return f'<path fill="{fill}" fill-rule="evenodd" d="{" ".join(d)}"/>'


def poly(pts, fill, extra=""):
    return f'<path fill="{fill}" {extra} d="M' + " L".join(f"{f(x)},{f(y)}" for x, y in pts) + ' Z"/>'


# ---------------------------------------------------------------- the badge
def badge(uid, with_banner=True):
    """The badge in a 1000x1000 frame, centre (500, 500). Returns (defs, body)."""
    sx, sy = 420, 600                       # the sun, rising behind the plant
    defs = (
        f'<clipPath id="in{uid}"><circle cx="500" cy="500" r="458"/></clipPath>'
        f'<pattern id="dots{uid}" width="13" height="13" patternUnits="userSpaceOnUse" patternTransform="rotate(30)">'
        f'<circle cx="6.5" cy="6.5" r="3" fill="{SOOT}"/></pattern>'
        f'<linearGradient id="fade{uid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0.3" stop-color="#000"/><stop offset="0.72" stop-color="#fff"/></linearGradient>'
        f'<mask id="hm{uid}" maskUnits="userSpaceOnUse" x="0" y="0" width="1000" height="1000">'
        f'<rect width="1000" height="1000" fill="url(#fade{uid})"/></mask>'
        f'<radialGradient id="vig{uid}" cx="500" cy="470" r="470" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0.5" stop-color="{SOOT}" stop-opacity="0"/>'
        f'<stop offset="1" stop-color="{SOOT}" stop-opacity="0.5"/></radialGradient>'
    )
    b = []
    # rim: soot body, a rust ring, the sky inside
    b.append(f'<circle cx="500" cy="500" r="492" fill="{SOOT}"/>')
    b.append(f'<circle cx="500" cy="500" r="474" fill="none" stroke="{RUST}" stroke-width="10"/>')
    inner = [f'<rect width="1000" height="1000" fill="{OCHRE}"/>']
    # sunburst
    rays = []
    for i in range(40):
        a0 = math.radians(i * 9)
        a1 = math.radians(i * 9 + 4.5)
        rays.append(f"M{sx},{sy} L{f(sx + 900 * math.cos(a0))},{f(sy + 900 * math.sin(a0))} "
                    f"L{f(sx + 900 * math.cos(a1))},{f(sy + 900 * math.sin(a1))} Z")
    inner.append(f'<path fill="{OCHRE_D}" d="{" ".join(rays)}"/>')
    # halftone darkening toward the horizon
    inner.append(f'<rect width="1000" height="1000" fill="url(#dots{uid})" mask="url(#hm{uid})" opacity="0.45"/>')
    # the eclipse: a bright corona, the black sun slightly off centre, one bead of light
    ox, oy = 9, -7
    inner.append(f'<circle cx="{sx}" cy="{sy}" r="222" fill="{GLINT}"/>')
    inner.append(f'<circle cx="{sx + ox}" cy="{sy + oy}" r="206" fill="{UMBER}"/>')
    ang = math.atan2(oy, ox)
    inner.append(f'<circle cx="{f(sx + math.cos(ang) * 218)}" cy="{f(sy + math.sin(ang) * 218)}" r="12" fill="{GLINT}"/>')
    # ground
    inner.append(f'<rect x="0" y="700" width="1000" height="300" fill="{OLIVE}"/>')
    # the plant: stepped reactor block
    inner.append(poly([(110, 790), (110, 706), (200, 706), (200, 676), (280, 676), (280, 650), (350, 650), (350, 628),
                       (600, 628), (600, 652), (800, 652), (800, 694), (890, 694), (890, 790)], PLANT))
    # the ventilation stack: lattice frame, chimney, striped top
    lat = []
    legs = ((636, 652, 674, 168), (764, 652, 726, 168))
    for x0, y0, x1, y1 in legs:
        lat.append(f"M{x0},{y0} L{x1},{y1}")
    levels = [168, 290, 410, 530, 652]
    def at(leg, y):
        x0, y0, x1, y1 = leg
        return x0 + (x1 - x0) * (y - y0) / (y1 - y0)
    for y in levels:
        lat.append(f"M{f(at(legs[0], y))},{y} L{f(at(legs[1], y))},{y}")
    for ya, yb in zip(levels, levels[1:]):
        lat.append(f"M{f(at(legs[0], ya))},{ya} L{f(at(legs[1], yb))},{yb}")
        lat.append(f"M{f(at(legs[1], ya))},{ya} L{f(at(legs[0], yb))},{yb}")
    inner.append(f'<path d="{" ".join(lat)}" fill="none" stroke="{SOOT}" stroke-width="7" stroke-linecap="square"/>')
    inner.append(f'<rect x="682" y="118" width="36" height="534" fill="{SOOT}"/>')
    for i in range(5):
        inner.append(f'<rect x="682" y="{118 + i * 26}" width="36" height="26" fill="{RUST if i % 2 == 0 else PAPER}"/>')
    inner.append(f'<rect x="676" y="112" width="48" height="8" fill="{SOOT}"/>')
    # age: darker toward the rim
    inner.append(f'<rect width="1000" height="1000" fill="url(#vig{uid})"/>')
    b.append(f'<g clip-path="url(#in{uid})">{"".join(inner)}</g>')

    if with_banner:
        ban = []
        ban.append(poly([(8, 772), (146, 772), (146, 902), (8, 902), (52, 837)], RUST_D))
        ban.append(poly([(992, 772), (854, 772), (854, 902), (992, 902), (948, 837)], RUST_D))
        ban.append(poly([(100, 882), (146, 902), (146, 882)], RUST_DD))
        ban.append(poly([(900, 882), (854, 902), (854, 882)], RUST_DD))
        ban.append(f'<rect x="100" y="746" width="800" height="136" fill="{RUST}"/>')
        ban.append(f'<path d="M100,758 H900 M100,870 H900" stroke="{PAPER}" stroke-opacity="0.55" stroke-width="4"/>')
        s = 0.5
        w = text_width("UMBRA", 18) * s
        ban.append(text("UMBRA", 500 - w / 2, 814 - CAP * s / 2, s, BONE, 18))
        b.append(f'<g transform="rotate(-4 500 814)">{"".join(ban)}</g>')
    return defs, "".join(b)


def worn_filter(x, y, w, h, seed=7):
    """Rough edges, flaked paint, pitting, uneven coverage, in user units."""
    return (
        f'<filter id="worn" filterUnits="userSpaceOnUse" x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" '
        f'color-interpolation-filters="sRGB">'
        f'<feTurbulence type="turbulence" baseFrequency="0.05" numOctaves="2" seed="{seed}" result="warp"/>'
        f'<feDisplacementMap in="SourceGraphic" in2="warp" scale="4" xChannelSelector="R" yChannelSelector="G" result="rough"/>'
        f'<feTurbulence type="fractalNoise" baseFrequency="0.09" numOctaves="3" seed="{seed + 4}" result="n1"/>'
        f'<feColorMatrix in="n1" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  -40 0 0 0 28.3" result="chips"/>'
        f'<feTurbulence type="fractalNoise" baseFrequency="0.7" numOctaves="1" seed="{seed + 6}" result="n2"/>'
        f'<feColorMatrix in="n2" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  -7 0 0 0 5.4" result="pits"/>'
        f'<feTurbulence type="fractalNoise" baseFrequency="0.016" numOctaves="3" seed="{seed + 9}" result="n3"/>'
        f'<feColorMatrix in="n3" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  -2 0 0 0 2.15" result="cover"/>'
        f'<feComposite in="rough" in2="chips" operator="in" result="a"/>'
        f'<feComposite in="a" in2="pits" operator="in" result="b"/>'
        f'<feComposite in="b" in2="cover" operator="in"/>'
        f'</filter>'
    )


def doc(w, h, defs, body, worn):
    pad = 20
    alldefs = defs + (worn_filter(-pad, -pad, w + 2 * pad, h + 2 * pad) if worn else "")
    g = f'<g filter="url(#worn)">{body}</g>' if worn else f"<g>{body}</g>"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(w)} {f(h)}" width="{f(w)}" height="{f(h)}">'
            f"<title>Umbra</title><defs>{alldefs}</defs>{g}</svg>\n")


# ---------------------------------------------------------------- lockups
def wordmark_block(x0, y0, s=1.0):
    """UMBRA with an offset print shadow, over a rust bar with УМБРА."""
    tw = text_width("UMBRA", 16)
    sh = 12
    out = [text("UMBRA", x0 + sh * s, y0 + sh * s, s, RUST, 16), text("UMBRA", x0, y0, s, BONE, 16)]
    by = y0 + (CAP + sh + 26) * s
    bh = 84
    out.append(f'<rect x="{f(x0)}" y="{f(by)}" width="{f((tw + sh) * s)}" height="{f(bh * s)}" fill="{RUST}"/>')
    cs = 0.3
    cw = text_width("УМБРА", 60) * cs
    out.append(text("УМБРА", x0 + ((tw + sh) * s - cw * s) / 2, by + (bh - CAP * cs) / 2 * s, cs * s, PAPER, 60))
    return "".join(out), (tw + sh) * s, (CAP + sh + 26 + bh) * s


def emblem_doc(worn, banner=True):
    d, body = badge("e", banner)
    return doc(1000, 1000, d, body, worn)


def wordmark_doc(worn):
    body, w, h = wordmark_block(20, 20)
    return doc(w + 40, h + 40, "", body, worn)


def horizontal_doc(worn):
    d, b = badge("h")
    s = 1.55
    _, ww, wh = wordmark_block(0, 0, s)
    gap = 90
    W = 1000 + gap + ww + 30
    body = b + wordmark_block(1000 + gap, (1000 - wh) / 2, s)[0]
    return doc(W, 1000, d, body, worn)


FILES = {
    "umbra-badge.svg": lambda: emblem_doc(True),
    "umbra-badge-clean.svg": lambda: emblem_doc(False),
    "umbra-icon.svg": lambda: emblem_doc(False, banner=False),
    "umbra-wordmark.svg": lambda: wordmark_doc(True),
    "umbra-wordmark-clean.svg": lambda: wordmark_doc(False),
    "umbra-logo-horizontal.svg": lambda: horizontal_doc(True),
    "umbra-logo-horizontal-clean.svg": lambda: horizontal_doc(False),
}

RENDERS = [
    ("umbra-badge.svg", "umbra-badge-1024.png", 1024),
    ("umbra-badge.svg", "umbra-badge-512.png", 512),
    ("umbra-badge-clean.svg", "umbra-badge-clean-1024.png", 1024),
    ("umbra-icon.svg", "umbra-icon-256.png", 256),
    ("umbra-icon.svg", "umbra-icon-64.png", 64),
    ("umbra-icon.svg", "umbra-icon-32.png", 32),
    ("umbra-wordmark.svg", "umbra-wordmark-1600.png", 1600),
    ("umbra-logo-horizontal.svg", "umbra-logo-horizontal-2400.png", 2400),
]


def rsvg():
    if shutil.which("rsvg-convert"):
        return ["rsvg-convert"]
    if shutil.which("distrobox"):
        return ["distrobox", "enter", "zgo-dev", "--", "rsvg-convert"]
    sys.exit("rsvg-convert not found (install librsvg2-tools, or use the zgo-dev container)")


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
                    "(", p("umbra-badge-1024.png"), "-resize", "640x640", ")", "-geometry", "+60+50", "-compose", "over", "-composite",
                    "(", p("umbra-wordmark-1600.png"), "-resize", "780x", ")", "-geometry", "+760+210", "-composite",
                    "(", p("umbra-logo-horizontal-2400.png"), "-resize", "900x", ")", "-geometry", "+60+760", "-composite",
                    "(", p("umbra-icon-256.png"), "-resize", "128x128", ")", "-geometry", "+1080+820", "-composite",
                    "(", p("umbra-icon-64.png"), ")", "-geometry", "+1240+852", "-composite",
                    "(", p("umbra-icon-32.png"), ")", "-geometry", "+1336+868", "-composite",
                    p("preview.png")], check=True)
    os.remove(tmp)


def main():
    os.makedirs(SVG, exist_ok=True)
    for name, make in FILES.items():
        with open(os.path.join(SVG, name), "w", encoding="utf-8") as fh:
            fh.write(make())
    print(f"wrote {len(FILES)} SVGs to {SVG}")
    if "--png" in sys.argv:
        os.makedirs(PNG, exist_ok=True)
        cmd = rsvg()
        for src, dst, w in RENDERS:
            subprocess.run(cmd + ["-w", str(w), "-o", os.path.join(PNG, dst), os.path.join(SVG, src)], check=True)
        preview()
        print(f"rendered {len(RENDERS)} PNGs and preview.png to {PNG}")


if __name__ == "__main__":
    main()
