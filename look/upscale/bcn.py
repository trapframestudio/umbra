"""
Block-compressed DDS writing (BC1/DXT1, BC3/DXT5, BC7) with a full mip chain.

    python3 bcn.py build        compile the encoder (needs g++ with OpenMP)

The encoders are Rich Geldreich's bc7enc_rdo (MIT / public domain): rgbcx for
BC1/BC3, bc7enc for BC7. Its source is a pinned git checkout in
~/.cache/umbra-look/tools/bc7enc_rdo/src (downloaded, so it stays out of the
repo); bcn.cpp here is our own small C entry point, compiled with it into
~/.cache/umbra-look/tools/bc7enc_rdo/build/libbcn.so and called through ctypes.

Headers are written the way texconv writes them, which is what G.A.M.M.A.'s
own textures carry: DX9 "DXT1"/"DXT5" FourCC, or a DX10 header with
DXGI_FORMAT_BC7_UNORM (98) for BC7. Plain UNORM, never the _SRGB formats:
the game's textures aren't sRGB-flagged.
"""
import ctypes
import os
import struct
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.expanduser("~/.cache/umbra-look/tools/bc7enc_rdo")
SRC = os.path.join(TOOL, "src")
LIB = os.path.join(TOOL, "build", "libbcn.so")
PINNED = "b9438627eef73a1157e84201b6fa6eb2ffd6d9f0"    # bc7enc_rdo master, 2026-07-30

BC1, BC3, BC7 = 1, 3, 7
NAMES = {BC1: "BC1", BC3: "BC3", BC7: "BC7"}
# encoder effort: rgbcx level (0..18) for BC1/BC3, bc7enc uber level (0..4) for BC7
QUALITY = {BC1: 18, BC3: 18, BC7: 4}

DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PIXELFORMAT = 0x1, 0x2, 0x4, 0x1000
DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = 0x20000, 0x80000
DDPF_FOURCC = 0x4
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000
DXGI_FORMAT_BC7_UNORM, D3D10_RESOURCE_DIMENSION_TEXTURE2D = 98, 3


# ---------------------------------------------------------------- the encoder
def build():
    """Compile bcn.cpp with the pinned bc7enc_rdo sources into LIB."""
    head = subprocess.run(["git", "-C", SRC, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != PINNED:
        raise SystemExit("bc7enc_rdo checkout is at %r, expected %s" % (head, PINNED))
    os.makedirs(os.path.dirname(LIB), exist_ok=True)
    cmd = ["g++", "-O3", "-fPIC", "-shared", "-fopenmp", "-fno-strict-aliasing", "-std=c++17",
           "-I", SRC, os.path.join(HERE, "bcn.cpp"),
           os.path.join(SRC, "rgbcx.cpp"), os.path.join(SRC, "bc7enc.cpp"), "-o", LIB]
    subprocess.run(cmd, check=True)
    return LIB


_lib = None


def lib():
    global _lib
    if _lib is None:
        if not os.path.exists(LIB):
            raise SystemExit("encoder not built: python3 %s build" % os.path.join(HERE, "bcn.py"))
        _lib = ctypes.CDLL(LIB)
        _lib.bcn_encode.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        _lib.bcn_encode.restype = ctypes.c_int
        _lib.bcn_init()
    return _lib


def block_bytes(fmt):
    return 8 if fmt == BC1 else 16


def level_size(fmt, w, h):
    return ((w + 3) // 4) * ((h + 3) // 4) * block_bytes(fmt)


def encode(rgba, fmt, quality=None, perceptual=True, threads=0):
    """One image (h, w, 4) uint8 RGBA -> its compressed blocks (bytes).
    The ctypes call releases the GIL; threads=0 lets OpenMP use every core."""
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    h, w = rgba.shape[:2]
    out = np.empty(level_size(fmt, w, h), np.uint8)
    q = QUALITY[fmt] if quality is None else quality
    r = lib().bcn_encode(fmt, rgba.ctypes.data, w, h, q, 1 if perceptual else 0, threads, out.ctypes.data)
    if r:
        raise ValueError("bcn_encode failed (%d)" % r)
    return out.tobytes()


# ---------------------------------------------------------------- mips
def _to_linear(x):
    x = x * np.float32(1 / 255.0)
    return np.where(x <= 0.04045, x / np.float32(12.92), ((x + np.float32(0.055)) / np.float32(1.055)) ** np.float32(2.4))


def _to_srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * np.float32(12.92),
                    np.float32(1.055) * x ** np.float32(1 / 2.4) - np.float32(0.055)) * np.float32(255.0)


def _half(a):
    """Halve each side (an odd edge repeats its last row/column), 2x2 box."""
    if a.shape[0] > 1:
        if a.shape[0] % 2:
            a = np.concatenate([a, a[-1:]], axis=0)
        a = (a[0::2] + a[1::2]) * np.float32(0.5)
    if a.shape[1] > 1:
        if a.shape[1] % 2:
            a = np.concatenate([a, a[:, -1:]], axis=1)
        a = (a[:, 0::2] + a[:, 1::2]) * np.float32(0.5)
    return a


def mip_chain(rgba):
    """Every level down to 1x1 (float32 twin of ../dds.py's mip_chain): colour
    averaged in linear light, weighted by alpha so that transparent pixels
    don't bleed their colour into the visible ones; alpha averaged plainly."""
    levels = [np.ascontiguousarray(rgba, dtype=np.uint8)]
    a = rgba.astype(np.float32)
    col = _to_linear(a[..., :3])
    alpha = a[..., 3:4] * np.float32(1 / 255.0)
    opaque = bool((rgba[..., 3] == 255).all())
    while col.shape[0] > 1 or col.shape[1] > 1:
        if opaque:
            col = _half(col)
        else:
            plain = _half(col)
            pre = _half(col * alpha)
            alpha = _half(alpha)
            col = np.where(alpha > 1e-6, pre / np.maximum(alpha, 1e-6), plain)
        out = np.empty(col.shape[:2] + (4,), np.uint8)
        out[..., :3] = np.clip(np.rint(_to_srgb(col)), 0, 255)
        out[..., 3] = 255 if opaque else np.clip(np.rint(alpha[..., 0] * 255), 0, 255)
        levels.append(out)
    return levels


# ---------------------------------------------------------------- DDS
def header(fmt, w, h, mips):
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    caps = DDSCAPS_TEXTURE
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP
    fourcc = {BC1: b"DXT1", BC3: b"DXT5", BC7: b"DX10"}[fmt]
    hdr = struct.pack("<4s7I44x", b"DDS ", 124, flags, h, w, level_size(fmt, w, h), 1, mips)
    hdr += struct.pack("<2I4s5I", 32, DDPF_FOURCC, fourcc, 0, 0, 0, 0, 0)
    hdr += struct.pack("<5I", caps, 0, 0, 0, 0)
    if fmt == BC7:
        # format, dimension, misc flag, array size, misc flags 2 (alpha mode unknown)
        hdr += struct.pack("<5I", DXGI_FORMAT_BC7_UNORM, D3D10_RESOURCE_DIMENSION_TEXTURE2D, 0, 1, 0)
    return hdr


def write(path, rgba, fmt, quality=None, perceptual=True, threads=0):
    """Compress rgba (h, w, 4 uint8) with a full linear-light mip chain and
    write the DDS atomically (a temp file renamed into place). Returns bytes written."""
    levels = mip_chain(rgba)
    h, w = rgba.shape[:2]
    parts = [header(fmt, w, h, len(levels))]
    for lv in levels:
        parts.append(encode(lv, fmt, quality, perceptual, threads))
    data = b"".join(parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return len(data)


def check(path):
    """Read a written DDS back: (w, h, mips, fourcc/dxgi, ok) where ok says the
    header and the file length agree with a full mip chain."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"DDS " or struct.unpack_from("<I", data, 4)[0] != 124:
        return None
    h, w, linear, _depth, mips = struct.unpack_from("<5I", data, 12)
    fourcc = data[84:88]
    fmt = {b"DXT1": BC1, b"DXT5": BC3, b"DX10": BC7}.get(fourcc)
    off = 128
    if fourcc == b"DX10":
        dxgi, dim, _misc, arr, _m2 = struct.unpack_from("<5I", data, 128)
        if dxgi != DXGI_FORMAT_BC7_UNORM or dim != D3D10_RESOURCE_DIMENSION_TEXTURE2D or arr != 1:
            return w, h, mips, "DXGI%d" % dxgi, False
        off += 20
    full = 1 + int(np.floor(np.log2(max(w, h))))
    size, lw, lh = 0, w, h
    for _ in range(mips):
        size += level_size(fmt, lw, lh)
        lw, lh = max(1, lw // 2), max(1, lh // 2)
    ok = fmt is not None and mips == full and len(data) == off + size and linear == level_size(fmt, w, h)
    return w, h, mips, fourcc.decode("latin1"), ok


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        print(build())
    else:
        print(__doc__)
