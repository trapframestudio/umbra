"""
Side-by-side comparisons for choosing how the HD textures should look.

    <venv>/bin/python -I pilot.py [texture paths under gamedata ...]

For each texture: a 1:1 crop of the busiest area at 4K, as
  1. the original, magnified the way the game does (smooth),
  2. the raw model output,
  3. the locked result (hd.py) at a few strengths,
plus a thumbnail of the whole texture. Sheets go to ../out/preview/upscale/.
"""
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import dds      # noqa: E402
import esrgan   # noqa: E402
import hd       # noqa: E402
import vfs      # noqa: E402

MODELS = os.path.expanduser("~/.cache/umbra-look/models")
OUT = os.path.join(os.path.dirname(HERE), "out", "preview", "upscale")
PILOT = [
    "textures/act/act_faces_3_01.dds",
    "textures/act/act_stalker_ecolog_sts_3.dds",
    "textures/act/act_stalker_nebo_merc_legs.dds",
    "textures/act/act_stalker_monolith_1.dds",
    "textures/act/normalbloodsucker.dds",
    "textures/act/act_arm_wool_travel.dds",
]
CROP = 384


def busiest(rgb, size):
    """Top-left of the size x size window with the most local detail."""
    g = rgb.mean(-1)
    d = np.abs(np.diff(g, axis=0))[:, :-1] + np.abs(np.diff(g, axis=1))[:-1]
    best, pos = -1, (0, 0)
    H, W = d.shape
    step = max(8, size // 4)
    for y in range(0, max(1, H - size), step):
        for x in range(0, max(1, W - size), step):
            s = d[y:y + size, x:x + size].mean()
            if s > best:
                best, pos = s, (y, x)
    return pos


def label(img, text):
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, img.width, 22), fill=(0, 0, 0))
    d.text((6, 4), text, fill=(235, 225, 200))
    return img


def to_img(a):
    return Image.fromarray(np.round(np.clip(a, 0, 1) * 255).astype(np.uint8))


def main(paths):
    os.makedirs(OUT, exist_ok=True)
    v = vfs.VFS(prefix="textures/act/")
    ups = {"RealisticRescaler": esrgan.Upscaler(os.path.join(MODELS, "realistic-rescaler"))}
    for rel in paths:
        src_bytes = v.find(rel).read()
        W, H, _m, fmt = dds.info(src_bytes)
        rgba = dds.read(src_bytes)
        rgb = rgba[..., :3].astype(np.float32) / 255.0
        k = 4096 / max(W, H)
        target = (int(W * k), int(H * k))
        outs = {}
        t = time.time()
        for name, up in ups.items():
            m = up.run(rgb)
            if m.shape[1] != target[0]:
                m = hd.down(m, target)
            outs[name] = m
        dt = time.time() - t
        rr = outs["RealisticRescaler"]
        rr = outs["RealisticRescaler"]
        g, trust = hd.guarded(rgb, rr, fine=0.6, mid=0.8)
        trust_img = np.repeat(hd.up(trust[..., None], target), 3, axis=-1)
        rr = outs["RealisticRescaler"]
        g, trust = hd.guarded(rgb, rr)
        trust_img = np.repeat(hd.up(trust[..., None], target), 3, axis=-1)
        variants = [("ORIGINAL, as the game magnifies it", hd.up(rgb, target)),
                    ("raw AI upscale (what we avoid)", rr),
                    ("UMBRA HD (guarded)", g),
                    ("where the AI was trusted (white)", trust_img)]
        model = outs["RealisticRescaler"]
        y, x = busiest(model, CROP)
        tiles = [label(to_img(a[y:y + CROP, x:x + CROP]), name) for name, a in variants]
        thumb = to_img(rgb).resize((CROP, int(CROP * H / W)))
        d = ImageDraw.Draw(thumb)
        d.rectangle((x / k * CROP / W, y / k * CROP / W, (x + CROP) / k * CROP / W, (y + CROP) / k * CROP / W),
                    outline=(255, 200, 0), width=2)
        label(thumb, "%s  %dx%d %s" % (rel.split("/")[-1], W, H, fmt))
        sheet = Image.new("RGB", (CROP * (len(tiles) + 1) + 4 * len(tiles), max(CROP, thumb.height)), (20, 20, 18))
        sheet.paste(thumb, (0, 0))
        for i, tl in enumerate(tiles):
            sheet.paste(tl, ((CROP + 4) * (i + 1), 0))
        name = os.path.splitext(os.path.basename(rel))[0]
        sheet.save(os.path.join(OUT, name + ".png"))
        print("%s %dx%d -> %dx%d in %.1fs (%s)" % (rel, W, H, target[0], target[1], dt, v.find(rel).label))


if __name__ == "__main__":
    main(sys.argv[1:] or PILOT)
