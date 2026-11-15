"""
Game textures used as source material for the logo's wear.

The textures come from the base game's texture archives (Anomaly, made from
GSC's S.T.A.L.K.E.R. assets). They are extracted on first use into .cache/
(not committed) with the .db reader from Zone Gore Overhaul's baker, then
converted from DDS with ImageMagick.

Paths can be overridden:
    UMBRA_GAME_DB   folder with textures*.db*   (default: ../../anomaly/db/textures)
    UMBRA_XDB       folder containing xdb.py    (default: ZGO's tools/zgo_baker)
"""
import glob
import os
import subprocess
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache", "textures")
GAMMA = os.path.normpath(os.path.join(HERE, "..", ".."))
GAME_DB = os.environ.get("UMBRA_GAME_DB", os.path.join(GAMMA, "anomaly", "db", "textures"))
XDB = os.environ.get("UMBRA_XDB", os.path.join(GAMMA, "gamma", "mods", "Zone Gore Overhaul", "tools", "zgo_baker"))

# name in the archive (under textures\) -> what we use it for
SOURCES = {
    "mtl\\mtl_rusted_01": "rust, orange and pitted",
    "mtl\\mtl_rust_dark": "rust, dark brown",
    "mtl\\mtl_garage_gate_rjav": "rust, streaked sheet",
    "wood\\wood_white_painted_01": "pale paint flaking off wood: peel shapes",
    "crete\\crete_stena_kraska_02": "pale paint flaking off concrete: peel shapes, grime",
    "mtl\\mtl_bochka_03_r": "red paint peeling off a barrel: peel shapes",
    "mtl\\mtl_red_painted": "red painted metal: grime and stains",
    "wall\\wall_walls_paint_01": "dirty painted wall: grime",
}


def _extract(missing):
    sys.path.insert(0, XDB)
    try:
        import xdb
    except ImportError:
        sys.exit(f"can't import xdb.py from {XDB} (set UMBRA_XDB)")
    want = {("textures\\" + n + ".dds").lower(): n for n in missing}
    archives = sorted(glob.glob(os.path.join(GAME_DB, "*.db*")))
    if not archives:
        sys.exit(f"no texture archives in {GAME_DB} (set UMBRA_GAME_DB)")
    for path in archives:
        if not want:
            break
        arc = xdb.Archive(path)
        for key in [k for k in want if k in arc.files]:
            name = want.pop(key)
            dds = os.path.join(CACHE, name.replace("\\", "__") + ".dds")
            with open(dds, "wb") as fh:
                fh.write(arc.read(key))
            subprocess.run(["magick", dds + "[0]", "-alpha", "off", dds[:-4] + ".png"], check=True)
            os.remove(dds)
        del arc
    if want:
        sys.exit("not found in the game archives: " + ", ".join(want.values()))


def load(name):
    """RGB float32 array in 0..1."""
    os.makedirs(CACHE, exist_ok=True)
    png = os.path.join(CACHE, name.replace("\\", "__") + ".png")
    if not os.path.exists(png):
        _extract([n for n in SOURCES if not os.path.exists(os.path.join(CACHE, n.replace("\\", "__") + ".png"))])
    return np.asarray(Image.open(png).convert("RGB"), np.float32) / 255.0
