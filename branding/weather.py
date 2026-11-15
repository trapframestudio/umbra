"""
Weathering for the logo, built from real game textures (see textures.py).

Each layer is a flat-painted shape (polygons in the logo's own units). For
each one:

- Where the paint is gone comes from a real peeling-paint texture: the bare
  patches of wood_white_painted_01 or the barrel's peeled red paint, scaled
  onto the logo. Edges and a slow "beaten-up zone" make wear more likely, so
  paint fails at the edges first and some areas are worse than others.
- Where paint is gone, real rust shows: a mix of two game rust textures.
- The paint itself is shaded by the grime in a dirty painted texture, gets a
  dark lip where it breaks, and a faint rust stain creeping out from under it.

No noise generators: all the irregularity is the game's own.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import textures

SS = 3   # supersampling for the shape masks


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], np.float32)


def lum(rgb):
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def blur(a, r):
    """Gaussian blur of a 0..1 float array (radius in pixels)."""
    if r <= 0:
        return a
    lo, hi = float(a.min()), float(a.max())
    span = hi - lo or 1.0
    # PIL only blurs 8-bit images; a coarse pass plus a residual pass keeps enough precision
    q = (a - lo) / span
    c = np.round(q * 255) / 255
    rr = q - c
    bc = np.asarray(Image.fromarray(np.round(c * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r)), np.float32) / 255
    br = np.asarray(Image.fromarray(np.round((rr * 64 + 0.5) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r)), np.float32) / 255
    return lo + span * (bc + (br - 0.5) / 64)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def raster(polys, W, H, scale):
    """Antialiased coverage mask for [(outer, [counters...]), ...] in logo units."""
    k = scale * SS
    img = Image.new("L", (int(round(W * k)), int(round(H * k))), 0)
    d = ImageDraw.Draw(img)
    for outer, holes in polys:
        d.polygon([(x * k, y * k) for x, y in outer], fill=255)
        for h in holes:
            d.polygon([(x * k, y * k) for x, y in h], fill=0)
    img = img.resize((int(round(W * scale)), int(round(H * scale))), Image.BOX)
    return np.asarray(img, np.float32) / 255.0


def sample(tex, H, W, ppt, ox=0, oy=0, rot=0, flip=False):
    """Tile a texture over an HxW canvas at ppt pixels per texel."""
    t = np.rot90(tex, rot)
    if flip:
        t = t[:, ::-1]
    th, tw = t.shape[:2]
    im = Image.fromarray(np.round(t * 255).astype(np.uint8))
    im = im.resize((max(1, int(round(tw * ppt))), max(1, int(round(th * ppt)))), Image.BICUBIC)
    a = np.asarray(im, np.float32) / 255.0
    ry = int(np.ceil((H + oy) / a.shape[0])) + 1
    rx = int(np.ceil((W + ox) / a.shape[1])) + 1
    return np.tile(a, (ry, rx, 1))[oy:oy + H, ox:ox + W]


def zscore(a, m):
    sel = a[m > 0.5]
    if sel.size == 0:
        return a * 0
    return (a - sel.mean()) / (sel.std() + 1e-6)


_T = {}


def T(name):
    if name not in _T:
        _T[name] = textures.load(name)
    return _T[name]


# How each kind of paint wears.
#   peel:  texture whose bare patches give the shapes, and how to read "bare" from it
#   grime: texture whose detail shades the paint
#   peel: [(texture, how to read "bare", texels -> logo units, weight)]
PEEL_CRETE = ("crete\\crete_stena_kraska_02", "dark", 0.42, 0.55)
PEEL_BARREL = ("mtl\\mtl_bochka_03_r", "unred", 0.9, 0.45)
MATERIALS = {
    "pale": dict(peel=[PEEL_CRETE, PEEL_BARREL], grime="wall\\wall_walls_paint_01"),
    "red": dict(peel=[("mtl\\mtl_bochka_03_r", "unred", 0.9, 0.75), PEEL_CRETE[:3] + (0.25,)], grime="mtl\\mtl_red_painted"),
    "dark": dict(peel=[PEEL_CRETE, PEEL_BARREL], grime="wall\\wall_walls_paint_01"),
}


def weather_layer(mask, color, material, scale, cover, seed):
    """Returns (rgb, alpha) for one painted shape. scale = pixels per logo unit."""
    H, W = mask.shape
    mat = MATERIALS[material]
    rs = np.random.RandomState(seed)
    off = lambda: (int(rs.randint(0, 1500)), int(rs.randint(0, 1500)))
    rot = int(rs.randint(0, 4))

    # where paint is gone: the bare patches of a real peeling-paint texture
    bare = np.zeros((H, W), np.float32)
    for i, (tex, kind, k, wgt) in enumerate(mat["peel"]):
        ox, oy = off()
        peel = sample(T(tex), H, W, k * scale, ox, oy, (rot + i) % 4)
        if kind == "dark":
            b = 1.0 - lum(peel)
        else:  # red paint: bare where it isn't red
            b = -(peel[..., 0] - 0.5 * (peel[..., 1] + peel[..., 2]))
        bare += wgt * zscore(blur(b, 0.5 * scale), mask)
    bare = zscore(bare, mask)

    # paint fails at edges first
    inner = blur(mask, 7 * scale)
    edge = np.clip(1.0 - inner, 0, 1) * mask
    # some areas are beaten up, others nearly clean
    ox, oy = off()
    zone = sample(T("crete\\crete_stena_kraska_02"), H, W, 0.25 * scale, ox, oy, rot ^ 1)
    zone = zscore(blur(lum(zone), 18 * scale), mask)

    score = bare + 1.1 * edge + 0.55 * zone
    thr = np.quantile(score[mask > 0.5], 1.0 - cover)
    exposed = smoothstep(thr - 0.06, thr + 0.06, score)

    # rust underneath: two real rust textures, mixed by a slow zone
    ox, oy = off()
    r1 = sample(T("mtl\\mtl_rust_dark"), H, W, 0.6 * scale, ox, oy, rot)
    ox, oy = off()
    r2 = sample(T("mtl\\mtl_rusted_01"), H, W, 0.6 * scale, ox, oy, (rot + 1) % 4)
    m = 0.35 + 0.5 * smoothstep(-1.0, 1.0, zone)[..., None]
    rust = r1 * (1 - m) + r2 * m
    g = lum(rust)[..., None]
    rust = (rust * 0.8 + g * 0.2) * 0.92             # a bit duller, a bit darker

    # the paint: flat colour shaded by real grime
    ox, oy = off()
    g0 = lum(sample(T(mat["grime"]), H, W, 0.5 * scale, ox, oy, rot))
    gr = np.clip(zscore(blur(g0, 1.2 * scale), mask), -2.5, 2.5)
    fine = np.clip(zscore(g0 - blur(g0, 1.5 * scale), mask), -3, 3)   # the surface's own fine grain
    shade = 1.0 + 0.045 * gr + 0.04 * np.clip(zone, -2, 2) + 0.022 * fine
    paint = hex_rgb(color)[None, None, :] * shade[..., None]

    # a dark lip where the paint breaks, and rust creeping out under it
    lip = np.clip(blur(exposed, 0.7 * scale) - exposed, 0, 1) * 2.2
    halo = blur(exposed, 2.5 * scale) * (1 - exposed)
    stain = hex_rgb("#5a3a22")[None, None, :]
    paint = paint * (1 - 0.4 * lip[..., None])
    paint = paint * (1 - 0.18 * halo[..., None]) + stain * 0.18 * halo[..., None]

    rgb = paint * (1 - exposed[..., None]) + rust * exposed[..., None]
    return np.clip(rgb, 0, 1), mask


def composite(W, H, layers, px_width, seed=7):
    """layers: [(polys, color, material, cover)], drawn in order. Returns an RGBA PIL image."""
    scale = px_width / W
    Hp, Wp = int(round(H * scale)), int(round(W * scale))
    out = np.zeros((Hp, Wp, 3), np.float32)
    alpha = np.zeros((Hp, Wp), np.float32)
    for i, (polys, color, material, cover) in enumerate(layers):
        mask = raster(polys, W, H, scale)
        rgb, a = weather_layer(mask, color, material, scale, cover, seed * 31 + i)
        a3 = a[..., None]
        out = rgb * a3 + out * (1 - a3)
        alpha = a + alpha * (1 - a)
    # un-premultiply for straight-alpha PNG
    a3 = alpha[..., None]
    straight = np.where(a3 > 1e-4, out / np.maximum(a3, 1e-4), 0)
    rgba = np.dstack([np.clip(straight, 0, 1), alpha])
    return Image.fromarray(np.round(rgba * 255).astype(np.uint8), "RGBA")


def flat(W, H, layers, px_width):
    """Clean, flat-colour render of the same layers (antialiased)."""
    scale = px_width / W
    Hp, Wp = int(round(H * scale)), int(round(W * scale))
    out = np.zeros((Hp, Wp, 3), np.float32)
    alpha = np.zeros((Hp, Wp), np.float32)
    for polys, color, _, _ in layers:
        a = raster(polys, W, H, scale)
        out = hex_rgb(color)[None, None, :] * a[..., None] + out * (1 - a[..., None])
        alpha = a + alpha * (1 - a)
    a3 = alpha[..., None]
    straight = np.where(a3 > 1e-4, out / np.maximum(a3, 1e-4), 0)
    return Image.fromarray(np.round(np.dstack([np.clip(straight, 0, 1), alpha]) * 255).astype(np.uint8), "RGBA")
