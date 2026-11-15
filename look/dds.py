"""
DDS reading and writing for the look tools.

Reading goes through Pillow (DXT1/3/5, BC4/5/7, uncompressed). Writing here is
uncompressed A8R8G8B8, which is what G.A.M.M.A.'s UI textures use; block
compression for the upscaled textures is done by an external encoder
(see upscale/).
"""
import io
import struct

import numpy as np
from PIL import Image

DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PITCH, DDSD_PIXELFORMAT, DDSD_MIPMAPCOUNT = 0x1, 0x2, 0x4, 0x8, 0x1000, 0x20000
DDPF_ALPHAPIXELS, DDPF_FOURCC, DDPF_RGB = 0x1, 0x4, 0x40
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000


def info(data):
    """(width, height, mip count, format name) from DDS bytes."""
    if data[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    h, w, _pitch, _depth, mips = struct.unpack_from("<5I", data, 12)
    fourcc = data[84:88]
    if fourcc == b"DX10":
        fmt = "DXGI%d" % struct.unpack_from("<I", data, 128)[0]
    elif fourcc == b"\0\0\0\0":
        fmt = "RGB%d" % struct.unpack_from("<I", data, 88)[0]
    else:
        fmt = fourcc.decode("latin1")
    return w, h, max(1, mips), fmt


def read(src):
    """RGBA uint8 array (h, w, 4) of the top mip, from a path or bytes."""
    if isinstance(src, (bytes, bytearray)):
        im = Image.open(io.BytesIO(src))
    else:
        im = Image.open(src)
    return np.asarray(im.convert("RGBA")).copy()


# ---------------------------------------------------------------- mips
def srgb_to_linear(x):
    x = x / 255.0
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055) * 255.0


def _half(a):
    """Halve each side (rounding up; an odd edge repeats its last row/column)."""
    if a.shape[0] > 1:
        if a.shape[0] % 2:
            a = np.concatenate([a, a[-1:]], axis=0)
        a = (a[0::2] + a[1::2]) / 2
    if a.shape[1] > 1:
        if a.shape[1] % 2:
            a = np.concatenate([a, a[:, -1:]], axis=1)
        a = (a[:, 0::2] + a[:, 1::2]) / 2
    return a


def mip_chain(rgba, linear=True):
    """Every mip level down to 1x1. Colour is averaged in linear light, weighted
    by alpha so transparent pixels don't bleed their colour in."""
    levels = [rgba]
    a = rgba.astype(np.float64)
    col = srgb_to_linear(a[..., :3]) if linear else a[..., :3] / 255.0
    alpha = a[..., 3:4] / 255.0
    while col.shape[0] > 1 or col.shape[1] > 1:
        pre = _half(col * alpha)
        alpha = _half(alpha)
        col = np.where(alpha > 1e-6, pre / np.maximum(alpha, 1e-6), _half(col))
        rgb = linear_to_srgb(col) if linear else col * 255.0
        out = np.concatenate([rgb, alpha * 255.0], axis=-1)
        levels.append(np.clip(np.rint(out), 0, 255).astype(np.uint8))
    return levels


# ---------------------------------------------------------------- writing
def write_rgba(path, rgba, mips=False):
    """Uncompressed A8R8G8B8 DDS (B,G,R,A bytes in memory)."""
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    h, w = rgba.shape[:2]
    levels = mip_chain(rgba) if mips else [rgba]
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PITCH | DDSD_PIXELFORMAT
    caps = DDSCAPS_TEXTURE
    if mips:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP
    header = struct.pack("<4s7I44x", b"DDS ", 124, flags, h, w, w * 4, 0, len(levels))
    pf = struct.pack("<8I", 32, DDPF_RGB | DDPF_ALPHAPIXELS, 0, 32,
                     0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    tail = struct.pack("<5I", caps, 0, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(header + pf + tail)
        for lv in levels:
            f.write(lv[..., [2, 1, 0, 3]].tobytes())


def save_png(path, rgba):
    Image.fromarray(np.asarray(rgba, dtype=np.uint8), "RGBA").save(path)
