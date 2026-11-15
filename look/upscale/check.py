"""
Contact sheets and checks for written HD textures.

    <venv>/bin/python -I check.py OUT_LAYER [--png DIR] [--name batch-check] rel ...

For each texture (a path under gamedata/, as batch.py writes it to OUT_LAYER):
  * the written DDS is read back (Pillow decodes BC1/BC7 itself, so this is an
    independent decoder) and its header checked (format, full mip chain, size);
  * PSNR of the compressed top level against the uncompressed result, when
    batch.py --keep-png saved it in DIR;
  * colour drift: the HD texture averaged back down to the source size, against
    the source (mean signed difference per channel, in 0..255);
  * one row on a sheet: the source thumbnail, then 1:1 crops at 4K of the
    busiest area and of a quieter one, original (magnified bilinearly, as the
    game samples it) next to Umbra HD (decoded from the DDS); alpha textures
    get a second row: both composited over a checkerboard, to show halos.
Sheets go to ../out/preview/upscale/<name>-NN.png, four textures per sheet.
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
LOOK = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, LOOK)

import bcn      # noqa: E402
import dds      # noqa: E402
import hd       # noqa: E402
import vfs      # noqa: E402

SHEETS = os.path.join(LOOK, "out", "preview", "upscale")
CROP = 320
GAP = 4


def detail_map(rgb):
    g = rgb.mean(-1)
    return np.abs(np.diff(g, axis=0))[:, :-1] + np.abs(np.diff(g, axis=1))[:-1]


def windows(rgb, size):
    """Top-left corners of the busiest window and of a middling one."""
    d = detail_map(rgb)
    H, W = d.shape
    step = max(16, size // 2)
    cand = []
    for y in range(0, max(1, H - size), step):
        for x in range(0, max(1, W - size), step):
            cand.append((float(d[y:y + size, x:x + size].mean()), y, x))
    cand.sort()
    busy = cand[-1]
    mid = cand[len(cand) * 2 // 5]
    return (busy[1], busy[2]), (mid[1], mid[2])


def checker(h, w, n=16):
    yy, xx = np.mgrid[0:h, 0:w]
    c = (((yy // n) + (xx // n)) % 2).astype(np.float32)
    return (0.35 + 0.3 * c)[..., None] * 255


def over(rgba, bg):
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    return np.clip(rgba[..., :3] * a + bg * (1 - a), 0, 255).astype(np.uint8)


def label(img, text):
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, img.width, 16), fill=(0, 0, 0))
    d.text((4, 2), text, fill=(235, 225, 200))
    return img


def psnr(a, b):
    m = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 10 * np.log10(255.0 ** 2 / max(m, 1e-9))


def row_for(v, out_layer, png_dir, rel):
    src = v.find(rel)
    srgba = dds.read(src.read())
    sh, sw = srgba.shape[:2]
    path = os.path.join(out_layer, "gamedata", rel)
    w, h, mips, fourcc, ok = bcn.check(path)
    hrgba = np.asarray(Image.open(path).convert("RGBA")).copy()
    stats = {"rel": rel, "dds": "%dx%d %s mips=%d %s" % (w, h, fourcc, mips, "ok" if ok else "BAD HEADER")}
    raw_path = os.path.join(png_dir or "", rel[len("textures/act/"):].replace("/", "__")[:-4] + ".png")
    if png_dir and os.path.exists(raw_path):
        raw = np.asarray(Image.open(raw_path).convert("RGBA"))
        stats["psnr_rgb"] = psnr(hrgba[..., :3], raw[..., :3])
        if fourcc == "DX10":
            stats["psnr_a"] = psnr(hrgba[..., 3], raw[..., 3])
    back = hd.down(hrgba[..., :3].astype(np.float32), (sw, sh))
    stats["drift"] = tuple(float(x) for x in (back - srgba[..., :3]).reshape(-1, 3).mean(0))
    stats["drift_abs"] = float(np.abs(back - srgba[..., :3]).mean())

    orig = np.asarray(Image.fromarray(srgba).resize((w, h), Image.BILINEAR))
    (by, bx), (my, mx) = windows(hrgba[..., :3].astype(np.float32), CROP)
    crop = lambda a, y, x: a[y:y + CROP, x:x + CROP]
    thumb = Image.fromarray(srgba[..., :3]).resize((CROP, max(1, int(CROP * sh / sw))))
    d = ImageDraw.Draw(thumb)
    for (y, x), col in (((by, bx), (255, 200, 0)), ((my, mx), (0, 200, 255))):
        k = CROP / w
        d.rectangle((x * k, y * k, (x + CROP) * k, (y + CROP) * k), outline=col, width=2)
    label(thumb, "%s %dx%d" % (os.path.basename(rel), sw, sh))
    tiles = [thumb]
    for (y, x), tag in (((by, bx), "busy"), ((my, mx), "quiet")):
        tiles.append(label(Image.fromarray(crop(orig, y, x)[..., :3]), "original, game-magnified (%s)" % tag))
        tiles.append(label(Image.fromarray(crop(hrgba, y, x)[..., :3]), "UMBRA HD %s (%s)" % (fourcc, tag)))
    rows = [tiles]
    if fourcc == "DX10":
        # alpha row: where the edge is: the window with the most alpha change
        ad = np.abs(np.diff(hrgba[..., 3].astype(np.float32), axis=1))
        best, pos = -1, (0, 0)
        for y in range(0, h - CROP, CROP // 2):
            for x in range(0, w - CROP, CROP // 2):
                s = ad[y:y + CROP, x:x + CROP].mean()
                if s > best:
                    best, pos = s, (y, x)
        y, x = pos
        bg = checker(CROP, CROP)
        arow = [label(Image.new("RGB", (CROP, CROP), (20, 20, 18)), "alpha edges")]
        arow.append(label(Image.fromarray(over(crop(orig, y, x).astype(np.float32), bg)), "original over checker"))
        arow.append(label(Image.fromarray(over(crop(hrgba, y, x).astype(np.float32), bg)), "UMBRA HD over checker"))
        arow.append(label(Image.fromarray(crop(orig, y, x)[..., 3]).convert("RGB"), "original alpha"))
        arow.append(label(Image.fromarray(crop(hrgba, y, x)[..., 3]).convert("RGB"), "UMBRA HD alpha"))
        rows.append(arow)
    return rows, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_layer")
    ap.add_argument("rels", nargs="+")
    ap.add_argument("--png", help="batch.py --keep-png folder (for compression PSNR)")
    ap.add_argument("--name", default="batch-check")
    ap.add_argument("--per-sheet", type=int, default=4)
    args = ap.parse_args()
    v = vfs.VFS(prefix="textures/act/")
    os.makedirs(SHEETS, exist_ok=True)
    rels = [r.lower() for r in args.rels]
    for n in range(0, len(rels), args.per_sheet):
        rows = []
        for rel in rels[n:n + args.per_sheet]:
            r, s = row_for(v, args.out_layer, args.png, rel)
            rows += r
            print("%s  %s  psnr %s%s  drift %s (abs %.2f)" % (
                s["rel"], s["dds"], "%.1f" % s["psnr_rgb"] if "psnr_rgb" in s else "-",
                " alpha %.1f" % s["psnr_a"] if "psnr_a" in s else "",
                " ".join("%+.2f" % c for c in s["drift"]), s["drift_abs"]))
        width = max(len(r) for r in rows) * (CROP + GAP)
        height = sum(max(t.height for t in r) + GAP for r in rows)
        sheet = Image.new("RGB", (width, height), (20, 20, 18))
        y = 0
        for r in rows:
            for i, t in enumerate(r):
                sheet.paste(t, (i * (CROP + GAP), y))
            y += max(t.height for t in r) + GAP
        p = os.path.join(SHEETS, "%s-%02d.png" % (args.name, n // args.per_sheet + 1))
        sheet.save(p)
        print("->", p)


if __name__ == "__main__":
    main()
