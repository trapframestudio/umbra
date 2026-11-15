"""
HD textures that stay true to the originals.

The rule (the user's): it must not look "AI". So the model is only allowed to
add detail finer than the original texture could hold. Everything the original
does have (colour, tone, shapes, grain at its own scale) is kept exactly:

    up(x)    = Lanczos upscale of x to the target size
    down(x)  = area-average downscale of x to the source size
    model(x) = the SR model's output at the target size

    hd = up(src) + strength * (model - up(down(model)))

The bracket is the model's detail above the source's resolution. Averaging hd
back down to the source size gives the source again (up to resampling), so
from a distance an HD texture looks exactly like the original. strength (0..1)
scales how much new detail goes in.
"""
import numpy as np
from PIL import Image


def _resize(a, size, method):
    """Resize float HxWxC (any range) channel by channel with PIL in float mode."""
    w, h = size
    out = np.empty((h, w, a.shape[2]), np.float32)
    for c in range(a.shape[2]):
        out[..., c] = np.asarray(Image.fromarray(a[..., c].astype(np.float32), "F").resize((w, h), method))
    return out


def up(a, size):
    return _resize(a, size, Image.LANCZOS)


def down(a, size):
    return _resize(a, size, Image.BOX)


def locked(src, model_out, strength=0.7):
    """src: HxWx3 float 0..1; model_out: the model's output at the target size."""
    H, W = src.shape[:2]
    th, tw = model_out.shape[:2]
    base = up(src, (tw, th))
    detail = model_out - up(down(model_out, (W, H)), (tw, th))
    return np.clip(base + strength * detail, 0, 1)


def upscale_rgb(upscaler, src, target):
    """Model output at target size (target = 4x or 2x the source). For a 2x
    target the model runs at 4x and is averaged down (as the Doom and
    Morrowind upscale projects do), which keeps it honest."""
    H, W = src.shape[:2]
    out = upscaler.run(src)
    if out.shape[0] != target[1]:
        out = down(out, target)
    return out


def alpha_up(a, size):
    """Alpha: Lanczos; a mask that was 0/255 only stays 0/255."""
    binary = np.isin(a, (0, 255)).mean() > 0.98
    out = up(a[..., None].astype(np.float32), size)[..., 0]
    if binary:
        out = np.where(out >= 127.5, 255.0, 0.0)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _blur(a, sigma):
    """Separable Gaussian blur of float HxWxC (edges reflected)."""
    if sigma <= 0:
        return a
    r = max(1, int(np.ceil(3 * sigma)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k = (k / k.sum()).astype(np.float32)
    p = np.pad(a.astype(np.float32), ((r, r), (r, r), (0, 0)), mode="reflect")
    H, W = a.shape[:2]
    tmp = sum(k[i] * p[i:i + H] for i in range(2 * r + 1))
    return sum(k[i] * tmp[:, i:i + W] for i in range(2 * r + 1))


def banded(src, model_out, fine=0.6, mid=0.6, sigma=1.2):
    """
    Three bands, judged at the source's resolution:
      broad (blur sigma source px and up): the original's, exactly;
      mid   (from there to the source's finest detail, where DXT block edges
             and compression noise live): mid parts the model's cleaned-up
             version, the rest the original's;
      fine  (finer than the source could hold): `fine` times the model's.
    mid=0, fine=x is `locked(..., x)` with the broad band widened; mid=1, fine=1
    is the raw model.
    """
    H, W = src.shape[:2]
    th, tw = model_out.shape[:2]
    m_small = down(model_out, (W, H))
    s_lo = _blur(src, sigma)
    m_lo = _blur(m_small, sigma)
    at_src = s_lo + (1 - mid) * (src - s_lo) + mid * (m_small - m_lo)
    detail = model_out - up(m_small, (tw, th))
    return np.clip(up(at_src, (tw, th)) + fine * detail, 0, 1)


def agreement(a, b, win=3.0):
    """Local correlation (-1..1) of two zero-mean detail images (luminance),
    in a Gaussian window of `win` pixels."""
    la = a.mean(-1, keepdims=True)
    lb = b.mean(-1, keepdims=True)
    num = _blur(la * lb, win)
    den = np.sqrt(_blur(la * la, win) * _blur(lb * lb, win)) + 1e-6
    return (num / den)[..., 0]


def coherence(a, win=3.0):
    """How strongly one direction dominates locally (0 = none, 1 = stripes):
    structure-tensor coherence of the luminance of a detail image."""
    l = a.mean(-1)
    gy, gx = np.gradient(l)
    jxx = _blur((gx * gx)[..., None], win)[..., 0]
    jyy = _blur((gy * gy)[..., None], win)[..., 0]
    jxy = _blur((gx * gy)[..., None], win)[..., 0]
    return np.sqrt((jxx - jyy) ** 2 + 4 * jxy ** 2) / (jxx + jyy + 1e-6)


def guarded(src, model_out, fine=0.6, mid=0.8, sigma=1.2, win=3.0, lo=0.15, hi=0.6, stripes=0.15):
    """
    banded(), but the model only gets its say where its detail agrees with the
    original's. At the source's resolution we compare the mid band of the
    original and of the model (local correlation). Where they agree (cleaned-up
    versions of the same pattern: a DXT block smoothed, stubble sharpened) the
    model's mid and fine detail are used; where they don't (the model invented
    a pattern: stripes on a mesh, worms on skin) we fall back to the original.
    lo/hi: correlation at which trust starts and is full. stripes: how much
    more one-directional the model's detail may be than the original's
    (a crosshatch mesh turned into diagonal stripes is the classic failure).
    """
    H, W = src.shape[:2]
    th, tw = model_out.shape[:2]
    m_small = down(model_out, (W, H))
    s_lo = _blur(src, sigma)
    m_lo = _blur(m_small, sigma)
    s_mid = src - s_lo
    m_mid = m_small - m_lo
    trust = np.clip((agreement(s_mid, m_mid, win) - lo) / (hi - lo), 0, 1)
    # the model turned a pattern without a direction into stripes: don't trust it
    gain = coherence(m_mid, win) - coherence(s_mid, win)
    trust = (trust * np.clip(1 - (gain - stripes) / 0.25, 0, 1))[..., None]
    w_mid = mid * trust
    at_src = s_lo + (1 - w_mid) * s_mid + w_mid * m_mid
    detail = model_out - up(m_small, (tw, th))
    t_up = up(trust, (tw, th))
    return np.clip(up(at_src, (tw, th)) + fine * t_up * detail, 0, 1), trust[..., 0]
