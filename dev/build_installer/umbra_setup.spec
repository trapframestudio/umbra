# PyInstaller spec for Umbra Setup. build.sh runs it; README.md here says how and why.
# It makes one file: "Umbra Setup.exe" (Windows, a window program) or umbra-setup-linux.
#
# It builds from a staging copy of installer/ (UMBRA_STAGE) that build.sh makes. build.sh
# also writes umbra_installer/build_info.py (the built-in pack URL) into that copy only,
# never into the source tree.
import os
import re
import struct
import sys

WIN = sys.platform == "win32"
HERE = SPECPATH  # noqa: F821 (PyInstaller sets SPECPATH, workpath, Analysis, PYZ, EXE)
STAGE = os.environ.get("UMBRA_STAGE", "")
PKG = os.path.join(STAGE, "umbra_installer")
ASSETS = os.path.join(PKG, "assets")
if not os.path.isfile(os.path.join(STAGE, "umbra_setup.py")) or not os.path.isfile(os.path.join(PKG, "build_info.py")):
    raise SystemExit("UMBRA_STAGE must name build.sh's staging copy of installer/ (run dev/build_installer/build.sh)")

with open(os.path.join(PKG, "__init__.py"), encoding="utf-8") as f:
    VERSION = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M).group(1)

# Every module of the package, including the ones only imported inside functions (gui, build_info).
ours = ["umbra_installer"] + ["umbra_installer." + fn[:-3] for fn in sorted(os.listdir(PKG))
                             if fn.endswith(".py") and fn != "__init__.py"]

# The logo and icons. install.asset_path() looks in sys._MEIPASS/assets.
pngs = sorted(fn for fn in os.listdir(ASSETS) if fn.lower().endswith(".png"))
for need in ("umbra-logo-1280.png", "umbra-icon-256.png"):
    if need not in pngs:
        raise SystemExit("missing %s in %s" % (need, ASSETS))
datas = [(os.path.join(ASSETS, fn), "assets") for fn in pngs]
# Umbra's typefaces and their licences (typeface.font_dir() looks in sys._MEIPASS/assets/fonts).
FONTS = os.path.join(ASSETS, "fonts")
fonts = sorted(fn for fn in os.listdir(FONTS) if fn.lower().endswith((".ttf", ".txt")))
for need in ("TekturNarrow-SemiBold.ttf", "Play-Regular.ttf", "Play-Bold.ttf", "OFL-Tektur.txt", "OFL-Play.txt"):
    if need not in fonts:
        raise SystemExit("missing %s in %s (branding/fonts.py makes them)" % (need, FONTS))
datas += [(os.path.join(FONTS, fn), os.path.join("assets", "fonts")) for fn in fonts]

# Linux: the empty libXcursor.so.1 stand-in (nocursor.c says why). build.sh compiles it and
# passes its path. umbra_setup.no_xcursor() loads it from sys._MEIPASS before Tk starts.
binaries = []
if not WIN:
    stub = os.environ.get("UMBRA_NOCURSOR", "")
    if not os.path.isfile(stub):
        raise SystemExit("UMBRA_NOCURSOR must name the built libXcursor.so.1 stand-in (run dev/build_installer/build.sh)")
    binaries.append((stub, "."))


def make_ico(dest, names):
    """A Windows .ico holding the PNG icons as they are (Vista and later read PNG entries)."""
    imgs = []
    for n in names:
        p = os.path.join(ASSETS, n)
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as f:
            data = f.read()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise SystemExit("%s is not a PNG" % p)
        w, h = struct.unpack(">II", data[16:24])
        imgs.append((w, h, data))
    if not imgs:
        raise SystemExit("no icon PNGs in %s" % ASSETS)
    imgs.sort(key=lambda i: -i[0])
    out = struct.pack("<HHH", 0, 1, len(imgs))
    off = 6 + 16 * len(imgs)
    for w, h, data in imgs:
        out += struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(data), off)
        off += len(data)
    out += b"".join(d for _, _, d in imgs)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(out)
    return dest


icon = None
version = None
if WIN:
    from PyInstaller.utils.win32 import versioninfo as vi

    icon = make_ico(os.path.join(workpath, "umbra.ico"), ["umbra-icon-256.png", "umbra-icon-64.png"])  # noqa: F821
    nums = tuple((list(map(int, re.findall(r"\d+", VERSION)[:4])) + [0, 0, 0, 0])[:4])
    version = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            vi.StringFileInfo([vi.StringTable("040904B0", [
                vi.StringStruct("ProductName", "Umbra"),
                vi.StringStruct("FileDescription", "Umbra Setup"),
                vi.StringStruct("FileVersion", VERSION),
                vi.StringStruct("ProductVersion", VERSION),
                vi.StringStruct("InternalName", "Umbra Setup"),
                vi.StringStruct("OriginalFilename", "Umbra Setup.exe"),
                vi.StringStruct("CompanyName", "Umbra"),
                vi.StringStruct("LegalCopyright", "Umbra contributors"),
            ])]),
            vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
        ])

a = Analysis(  # noqa: F821
    [os.path.join(STAGE, "umbra_setup.py")],
    pathex=[STAGE],
    binaries=binaries,
    datas=datas,
    hiddenimports=ours,
    runtime_hooks=[os.path.join(HERE, "rthook_umbra.py")],
    # never used by the installer; keeps the program smaller
    excludes=["unittest", "pydoc", "doctest", "lib2to3", "idlelib", "turtledemo", "turtle", "test",
              "setuptools", "pkg_resources", "distutils", "pip"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Umbra Setup" if WIN else "umbra-setup-linux",
    console=not WIN,
    icon=icon,
    version=version,
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    bootloader_ignore_signals=False,
)
