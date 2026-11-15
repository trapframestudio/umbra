#!/usr/bin/env python3
"""
Umbra logo: every file in svg/ and png/ is generated from the geometry below.

    python3 build.py          write the SVGs
    python3 build.py --png    also render the PNGs (needs rsvg-convert; falls
                              back to the zgo-dev distrobox when not installed)

The wordmark is hand-built stencil geometry, so it needs no font and belongs
to the project. The emblem is an eclipse, the literal umbra: a dark disk that
almost covers a sickly light, leaving a ring that tapers to nothing, inside a
broken detector bezel. "Worn" files run the shapes through an SVG filter that
roughens the edges and flakes the paint like an old spray stencil. "Clean"
files skip it, for small sizes where texture turns into noise.
"""
import math
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "svg")
PNG = os.path.join(HERE, "png")

BONE = "#d2c9ad"    # dirty off-white
SICK = "#b5b157"    # jaundiced yellow-green
SOOT = "#0c0d0b"    # background
GLINT = "#e9e2c6"   # the bead of light

CAP = 120           # cap height of the wordmark
TRACK = 30          # space between letters

# Stencil letters on a 120-unit cap height: 22-unit strokes, 6-unit stencil
# bridges, 10-unit chamfers on the outer corners. (width, [polygons])
LETTERS = {
    "U": (64, [
        [(0, 0), (22, 0), (22, 98), (29, 98), (29, 120), (10, 120), (0, 110)],
        [(35, 98), (42, 98), (42, 0), (64, 0), (64, 110), (54, 120), (35, 120)],
    ]),
    "M": (92, [
        [(0, 10), (10, 0), (22, 0), (22, 120), (0, 120)],
        [(26, 0), (42, 0), (46, 34), (50, 0), (66, 0), (56, 76), (36, 76)],
        [(70, 0), (82, 0), (92, 10), (92, 120), (70, 120)],
    ]),
    "B": (64, [
        [(0, 10), (10, 0), (22, 0), (22, 120), (10, 120), (0, 110)],
        [(28, 0), (54, 0), (64, 10), (64, 50), (58, 60), (64, 70), (64, 110), (54, 120),
         (28, 120), (28, 98), (42, 98), (42, 71), (28, 71), (28, 49), (42, 49), (42, 22), (28, 22)],
    ]),
    "R": (66, [
        [(0, 10), (10, 0), (22, 0), (22, 120), (0, 120)],
        [(28, 0), (54, 0), (64, 10), (64, 61), (54, 71), (28, 71), (28, 49), (42, 49), (42, 22), (28, 22)],
        [(36, 77), (54, 77), (66, 120), (46, 120)],
    ]),
    "A": (70, [
        [(0, 120), (0, 20), (20, 0), (32, 0), (32, 22), (22, 22), (22, 58), (32, 58), (32, 80), (22, 80), (22, 120)],
        [(70, 120), (70, 20), (50, 0), (38, 0), (38, 22), (48, 22), (48, 58), (38, 58), (38, 80), (48, 80), (48, 120)],
    ]),
}

WORD = "UMBRA"


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def wordmark_width():
    return sum(LETTERS[c][0] for c in WORD) + TRACK * (len(WORD) - 1)


def wordmark(x0=0.0, y0=0.0, scale=1.0, fill=BONE):
    """The wordmark as one path, top-left at (x0, y0)."""
    d, x = [], 0.0
    for c in WORD:
        w, polys = LETTERS[c]
        for p in polys:
            d.append("M" + " L".join(f"{f(x0 + (x + px) * scale)},{f(y0 + py * scale)}" for px, py in p) + " Z")
        x += w + TRACK
    return f'<path fill="{fill}" d="{" ".join(d)}"/>'


def arc(cx, cy, r, a0, a1):
    x0, y0 = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
    x1, y1 = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return f"M{f(x0)},{f(y0)} A{f(r)},{f(r)} 0 {large} 1 {f(x1)},{f(y1)}"


def emblem(cx, cy, R, uid):
    """The eclipse inside a broken bezel; R = outer radius of the bezel."""
    sw = R * 0.055
    rr = R - sw / 2
    gap = 13                                    # degrees per bezel gap
    arcs = " ".join(arc(cx, cy, rr, a + gap / 2, a + 90 - gap / 2) for a in (45, 135, 225, 315))
    rl, rd = R * 0.64, R * 0.607                # light disk, shadow disk
    ox, oy = R * 0.018, -R * 0.014              # the shadow sits a touch up and right
    # the "diamond": where the ring tapers away, a last bead of light
    ang = math.atan2(oy, ox)
    bx, by = cx + math.cos(ang) * (rl - R * 0.006), cy + math.sin(ang) * (rl - R * 0.006)
    return (
        f'<mask id="ecl{uid}" maskUnits="userSpaceOnUse" x="{f(cx - R)}" y="{f(cy - R)}" width="{f(2 * R)}" height="{f(2 * R)}">'
        f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(rl)}" fill="#fff"/>'
        f'<circle cx="{f(cx + ox)}" cy="{f(cy + oy)}" r="{f(rd)}" fill="#000"/></mask>'
        f'<path d="{arcs}" fill="none" stroke="{BONE}" stroke-width="{f(sw)}" stroke-linecap="butt"/>'
        f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(rl)}" fill="{SICK}" mask="url(#ecl{uid})"/>'
        f'<circle cx="{f(bx)}" cy="{f(by)}" r="{f(R * 0.042)}" fill="{GLINT}"/>'
    )


def worn_filter(x, y, w, h, seed=7):
    """Rough edges, flaked paint, uneven coverage. All in user units, so the
    texture keeps its size relative to the shapes at any render size."""
    return (
        f'<filter id="worn" filterUnits="userSpaceOnUse" x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" '
        f'color-interpolation-filters="sRGB">'
        f'<feTurbulence type="turbulence" baseFrequency="0.07" numOctaves="2" seed="{seed}" result="warp"/>'
        f'<feDisplacementMap in="SourceGraphic" in2="warp" scale="2.6" xChannelSelector="R" yChannelSelector="G" result="rough"/>'
        # chips: crisp flakes where the paint came off
        f'<feTurbulence type="fractalNoise" baseFrequency="0.16" numOctaves="3" seed="{seed + 4}" result="n1"/>'
        f'<feColorMatrix in="n1" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  -45 0 0 0 30.8" result="chips"/>'
        # pitting: a fine, partial grain
        f'<feTurbulence type="fractalNoise" baseFrequency="1.1" numOctaves="1" seed="{seed + 6}" result="n2"/>'
        f'<feColorMatrix in="n2" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  -7 0 0 0 5.2" result="pits"/>'
        # coverage: big, soft unevenness, never fully gone
        f'<feTurbulence type="fractalNoise" baseFrequency="0.028" numOctaves="3" seed="{seed + 9}" result="n3"/>'
        f'<feColorMatrix in="n3" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  -2.2 0 0 0 2.3" result="cover"/>'
        f'<feComposite in="rough" in2="chips" operator="in" result="a"/>'
        f'<feComposite in="a" in2="pits" operator="in" result="b"/>'
        f'<feComposite in="b" in2="cover" operator="in"/>'
        f'</filter>'
    )


def doc(w, h, body, worn, bg=None, extra_defs=""):
    pad = 10
    defs = extra_defs + (worn_filter(-pad, -pad, w + 2 * pad, h + 2 * pad) if worn else "")
    g = f'<g filter="url(#worn)">{body}</g>' if worn else f"<g>{body}</g>"
    back = f'<rect width="100%" height="100%" fill="{bg}"/>' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(w)} {f(h)}" width="{f(w)}" height="{f(h)}">'
            f"<title>Umbra</title><defs>{defs}</defs>{back}{g}</svg>\n")


# ---------------------------------------------------------------- lockups
def emblem_doc(worn):
    R = 100
    return doc(2 * R + 20, 2 * R + 20, emblem(R + 10, R + 10, R, "e"), worn)


def wordmark_doc(worn):
    w = wordmark_width()
    return doc(w + 20, CAP + 20, wordmark(10, 10), worn)


def horizontal_doc(worn):
    R = CAP * 0.82                       # emblem a little taller than the caps
    gapx = CAP * 0.45
    ww = wordmark_width()
    w = 2 * R + gapx + ww + 20
    h = 2 * R + 20
    body = emblem(10 + R, 10 + R, R, "h") + wordmark(10 + 2 * R + gapx, 10 + R - CAP / 2)
    return doc(w, h, body, worn)


def stacked_doc(worn):
    ww = wordmark_width()
    R = CAP * 1.25
    gapy = CAP * 0.42
    w = ww + 20
    h = 2 * R + gapy + CAP + 20
    body = emblem(w / 2, 10 + R, R, "s") + wordmark(10, 10 + 2 * R + gapy)
    return doc(w, h, body, worn)


def preview_doc():
    """A sheet for looking at it: dark and light grounds, sizes."""
    W, H = 1600, 1180
    ww = wordmark_width()
    defs = (
        f'<radialGradient id="vig" cx="50%" cy="42%" r="75%">'
        f'<stop offset="0" stop-color="#1a1b17"/><stop offset="1" stop-color="{SOOT}"/></radialGradient>'
        f'<filter id="grain" x="0" y="0" width="100%" height="100%">'
        f'<feTurbulence type="fractalNoise" baseFrequency="0.7" numOctaves="2" seed="2"/>'
        f'<feColorMatrix type="matrix" values="0 0 0 0 0.6  0 0 0 0 0.6  0 0 0 0 0.55  0 0 0 0.07 0"/></filter>'
    )
    # big stacked logo (worn), horizontal lockup, small clean emblems, light-ground version
    s = 1.3
    R = CAP * 1.25
    stack_w = (ww + 20) * s
    x0 = (W - stack_w) / 2
    big = (f'<g transform="translate({f(x0)},40) scale({f(s)})">'
           f'<g filter="url(#worn)">{emblem((ww + 20) / 2, 10 + R, R, "p1")}{wordmark(10, 10 + 2 * R + CAP * 0.42)}</g></g>')
    hs = 0.9
    Rh = CAP * 0.82
    hw = (2 * Rh + CAP * 0.45 + ww + 20) * hs
    hx = (W - hw) / 2
    horiz = (f'<g transform="translate({f(hx)},740) scale({f(hs)})"><g filter="url(#worn)">'
             f'{emblem(10 + Rh, 10 + Rh, Rh, "p2")}{wordmark(10 + 2 * Rh + CAP * 0.45, 10 + Rh - CAP / 2)}</g></g>')
    smalls = ""
    x = 90
    for r in (32, 20, 12, 8):
        smalls += f'<g>{emblem(x + r, 1110 - r, r, f"s{r}")}</g>'
        x += 2 * r + 34
    light = (f'<rect x="{W - 430}" y="1040" width="360" height="110" fill="#d9d4c4"/>'
             f'<g transform="translate({W - 410},1055) scale(0.68)">'
             + emblem(10 + Rh * 0.6, 10 + Rh * 0.6, Rh * 0.6, "lt").replace(BONE, "#24251f")
             + wordmark(10 + 2 * Rh * 0.6 + 24, 10 + Rh * 0.6 - CAP * 0.3, 0.6, "#24251f") + "</g>")
    body = (f'<rect width="{W}" height="{H}" fill="url(#vig)"/>'
            f'<rect width="{W}" height="{H}" filter="url(#grain)"/>'
            + big + horiz + smalls + light)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
            f'<defs>{defs}{worn_filter(0, 0, 4000, 4000)}</defs>{body}</svg>\n')


FILES = {
    "umbra-emblem.svg": lambda: emblem_doc(True),
    "umbra-emblem-clean.svg": lambda: emblem_doc(False),
    "umbra-wordmark.svg": lambda: wordmark_doc(True),
    "umbra-wordmark-clean.svg": lambda: wordmark_doc(False),
    "umbra-logo-horizontal.svg": lambda: horizontal_doc(True),
    "umbra-logo-horizontal-clean.svg": lambda: horizontal_doc(False),
    "umbra-logo-stacked.svg": lambda: stacked_doc(True),
    "umbra-logo-stacked-clean.svg": lambda: stacked_doc(False),
    "preview.svg": preview_doc,
}

# (svg, png, width): worn art for big sizes, clean for small
RENDERS = [
    ("umbra-emblem.svg", "umbra-emblem-1024.png", 1024),
    ("umbra-emblem.svg", "umbra-emblem-512.png", 512),
    ("umbra-emblem-clean.svg", "umbra-icon-256.png", 256),
    ("umbra-emblem-clean.svg", "umbra-icon-64.png", 64),
    ("umbra-emblem-clean.svg", "umbra-icon-32.png", 32),
    ("umbra-wordmark.svg", "umbra-wordmark-2000.png", 2000),
    ("umbra-logo-horizontal.svg", "umbra-logo-horizontal-2400.png", 2400),
    ("umbra-logo-stacked.svg", "umbra-logo-stacked-1280.png", 1280),
    ("preview.svg", "preview.png", 1600),
]


def rsvg():
    if shutil.which("rsvg-convert"):
        return ["rsvg-convert"]
    if shutil.which("distrobox"):
        return ["distrobox", "enter", "zgo-dev", "--", "rsvg-convert"]
    sys.exit("rsvg-convert not found (install librsvg2-tools, or use the zgo-dev container)")


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
        print(f"rendered {len(RENDERS)} PNGs to {PNG}")


if __name__ == "__main__":
    main()
