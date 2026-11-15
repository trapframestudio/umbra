"""
The typefaces Umbra Setup and the launcher are set in, the ones the game's UI
uses (look/fonts.py): Tektur Narrow for headings and buttons, Play for text.

    <venv>/bin/python -I branding/fonts.py SOURCE_DIR

SOURCE_DIR holds the Google Fonts files (github.com/google/fonts, ofl/tektur
and ofl/play), checked against the sha256 below. Writes
installer/umbra_installer/assets/fonts/:

  TekturNarrow-SemiBold.ttf  Tektur's variable font fixed at the width and
                             weight the game's UI uses (wdth 80, wght 600). Tk
                             can't pick a variable font's axes, so it gets one
                             face with a family name of its own. Tektur's OFL
                             reserves no font name, so the modified face may
                             keep it.
  Play-Regular.ttf, Play-Bold.ttf
                             as published: Play's OFL reserves the name "Play",
                             so it ships unmodified.
  OFL-Tektur.txt, OFL-Play.txt
                             their licences, which go wherever they go.

Needs fontTools (pip install fonttools) in a venv; Setup itself reads the
fonts with its own small reader (installer/umbra_installer/ttf.py).
"""
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "installer", "umbra_installer", "assets", "fonts")

SOURCES = {   # file under SOURCE_DIR: sha256
    "tektur/Tektur[wdth,wght].ttf": "a4c451e5336874811267c2a514efa09bca7bc8cabf32a310f0b44e111eacb933",
    "play/Play-Regular.ttf": "eed0da79005cab35d6ed0eacab594ed67cc643be0b2632fa9e440b3bc5078dc4",
    "play/Play-Bold.ttf": "45c572eccda4cf335165b750345258e753035bf48ee2fdf37faa07c7db88bce0",
}
HEAD_FAMILY = "Tektur Narrow SemiBold"


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tektur_narrow(src, dst):
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer
    f = instancer.instantiateVariableFont(TTFont(src), {"wdth": 80, "wght": 600})
    name = f["name"]
    for nid in (16, 17, 21, 22, 25):                  # typographic and variation names: one plain family
        name.removeNames(nameID=nid)
    for nid, value in ((1, HEAD_FAMILY), (2, "Regular"), (3, "Tektur Narrow SemiBold; Umbra"),
                       (4, HEAD_FAMILY), (6, "TekturNarrow-SemiBold")):
        name.setName(value, nid, 3, 1, 0x409)         # Windows, Unicode BMP, English
        name.setName(value, nid, 1, 0, 0)             # Mac Roman (older readers)
    f["OS/2"].usWeightClass = 400                     # the one face of its family: plain, not "semibold"
    f["OS/2"].usWidthClass = 5
    f["OS/2"].fsSelection = (f["OS/2"].fsSelection & ~0b1100001) | 0b1000000   # REGULAR, not bold/italic
    f["head"].macStyle = 0
    if "STAT" in f:
        del f["STAT"]
    f.save(dst)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    for rel, want in SOURCES.items():
        got = sha256(os.path.join(src, rel))
        if got != want:
            sys.exit("%s: sha256 %s, expected %s" % (rel, got, want))
    os.makedirs(OUT, exist_ok=True)
    tektur_narrow(os.path.join(src, "tektur/Tektur[wdth,wght].ttf"), os.path.join(OUT, "TekturNarrow-SemiBold.ttf"))
    for fn in ("Play-Regular.ttf", "Play-Bold.ttf"):
        shutil.copyfile(os.path.join(src, "play", fn), os.path.join(OUT, fn))
    shutil.copyfile(os.path.join(src, "tektur", "OFL.txt"), os.path.join(OUT, "OFL-Tektur.txt"))
    shutil.copyfile(os.path.join(src, "play", "OFL.txt"), os.path.join(OUT, "OFL-Play.txt"))
    for fn in sorted(os.listdir(OUT)):
        print("%8d  %s" % (os.path.getsize(os.path.join(OUT, fn)), fn))


if __name__ == "__main__":
    main()
