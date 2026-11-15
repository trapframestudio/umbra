"""
Worn paint on steel, from the base game's own textures. Used for the splashes
and the UI repaint.

This is ../branding/weather.py's method with the knobs a panel needs: how much
paint is gone, how big the chips are, how dark the rust under them is, how far
in from an edge wear reaches, and rain streaks. Every irregular shape comes
from a real game texture (peeling paint, rust, grime, streaked sheet metal);
nothing here generates noise.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "branding"))

import weather  # noqa: E402
from weather import T, blur, hex_rgb, lum, sample, smoothstep, zscore  # noqa: E402

PEEL = {
    # texture, how to read "bare" from it
    "chips": ("mtl\\mtl_bochka_03_r", "unred"),     # small chips: peeled barrel paint
    "flakes": ("crete\\crete_stena_kraska_02", "dark"),  # larger flaking: paint off concrete
}


def _bare(kind, H, W, ppt, ox, oy, rot):
    tex, how = PEEL[kind]
    t = sample(T(tex), H, W, ppt, ox, oy, rot)
    if how == "dark":
        return 1.0 - lum(t)
    return -(t[..., 0] - 0.5 * (t[..., 1] + t[..., 2]))


def sheet(mask, color, *, px=1.0, cover=0.05, chip=1.0, edge=10.0, edge_bias=1.1,
          rust_tone=0.6, grime=1.0, streaks=0.0, seed=1, flakes=0.3, grime_tex="wall\\wall_walls_paint_01"):
    """
    Paint `color` over `mask` (H x W, 0..1) and wear it.

    px        pixels per "unit"; texture detail scales with it (1 = 1080p-ish UI)
    cover     share of the shape where paint is gone (0..1)
    chip      size of chips (1 = normal; smaller = finer)
    edge      how far from an edge wear is concentrated, in units
    edge_bias how much more edges wear than the middle
    rust_tone brightness of the rust under the paint (1 = as in the texture)
    grime     strength of the grime shading on the paint
    streaks   strength of rain streaks (darkening that runs down the sheet)
    flakes    weight of large flaking vs small chips
    grime_tex the texture whose detail shades the paint (a name or an RGB array)

    Returns rgb (H x W x 3, 0..1). The caller composites it with the mask.
    """
    H, W = mask.shape
    rs = np.random.RandomState(seed)
    off = lambda: (int(rs.randint(0, 1500)), int(rs.randint(0, 1500)))
    rot = int(rs.randint(0, 4))

    bare = np.zeros((H, W), np.float32)
    for i, (kind, wgt) in enumerate((("chips", 1.0 - flakes), ("flakes", flakes))):
        if wgt <= 0:
            continue
        ox, oy = off()
        b = _bare(kind, H, W, 0.9 * px * chip if kind == "chips" else 0.45 * px * chip, ox, oy, (rot + i) % 4)
        bare += wgt * zscore(blur(b, 0.4 * px), mask)
    bare = zscore(bare, mask)

    near_edge = np.clip(1.0 - blur(mask, edge * px), 0, 1) * mask
    ox, oy = off()
    zone = sample(T("crete\\crete_stena_kraska_02"), H, W, 0.25 * px, ox, oy, rot ^ 1)
    zone = zscore(blur(lum(zone), 18 * px), mask)

    score = bare + edge_bias * 2.0 * near_edge + 0.5 * zone
    sel = score[mask > 0.5]
    thr = np.quantile(sel, 1.0 - cover) if sel.size and cover > 0 else np.inf
    exposed = smoothstep(thr - 0.05, thr + 0.05, score) if np.isfinite(thr) else np.zeros_like(score)

    # rust: two real rust textures, dulled and darkened
    ox, oy = off()
    r1 = sample(T("mtl\\mtl_rust_dark"), H, W, 0.6 * px, ox, oy, rot)
    ox, oy = off()
    r2 = sample(T("mtl\\mtl_rusted_01"), H, W, 0.6 * px, ox, oy, (rot + 1) % 4)
    m = 0.25 + 0.4 * smoothstep(-1.0, 1.0, zone)[..., None]
    rust = r1 * (1 - m) + r2 * m
    rust = (rust * 0.8 + lum(rust)[..., None] * 0.2) * rust_tone

    # the paint, shaded by real grime
    ox, oy = off()
    g0 = lum(sample(T(grime_tex) if isinstance(grime_tex, str) else grime_tex, H, W, 0.5 * px, ox, oy, rot))
    gr = np.clip(zscore(blur(g0, 1.2 * px), mask), -2.5, 2.5)
    fine = np.clip(zscore(g0 - blur(g0, 1.5 * px), mask), -3, 3)
    shade = 1.0 + grime * (0.05 * gr + 0.045 * np.clip(zone, -2, 2) + 0.025 * fine)
    paint = hex_rgb(color)[None, None, :] * shade[..., None]

    if streaks > 0:
        # rain streaks: the vertical streaking of a rusted garage gate, stretched down
        ox, oy = off()
        st = lum(sample(T("mtl\\mtl_garage_gate_rjav"), H, W, 0.7 * px, ox, oy, 0))
        st = zscore(st - blur(st, 6 * px), mask)
        paint = paint * (1.0 + streaks * 0.05 * np.clip(st, -3, 3))[..., None]

    lip = np.clip(blur(exposed, 0.6 * px) - exposed, 0, 1) * 2.0
    halo = blur(exposed, 2.0 * px) * (1 - exposed)
    stain = hex_rgb("#4a3020")[None, None, :]
    paint = paint * (1 - 0.35 * lip[..., None])
    paint = paint * (1 - 0.15 * halo[..., None]) + stain * 0.15 * halo[..., None]

    rgb = paint * (1 - exposed[..., None]) + rust * exposed[..., None]
    return np.clip(rgb, 0, 1)


def relief(height, light=(-0.6, -0.8), strength=1.0):
    """Shading of a height field lit from the top left (1 = flat)."""
    gy, gx = np.gradient(height)
    return 1.0 + strength * (gx * light[0] + gy * light[1])
