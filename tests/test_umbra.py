"""
Umbra installer tests: a small fake pack is built from a fake MO2 instance
with the real pack builder, then installed with the real installer (from a
folder and over HTTP), into an existing fake G.A.M.M.A. and into a new folder
through a stand-in stalker-gamma-cli.

    python3 -m unittest -v tests/test_umbra.py        (from the umbra folder)
"""
import copy
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))
sys.path.insert(0, os.path.join(ROOT, "dev"))

import pack_build  # noqa: E402
from umbra_installer import shortcuts as menus  # noqa: E402 (the module; shortcuts() below reads Steam's)
from umbra_installer import cab, fetch, gammacli, install, launcher, layers, mo2, packdef, runtime, steam, ttf, userltx  # noqa: E402,E501

REAL_DOWNLOADS = copy.deepcopy(runtime.DOWNLOADS)       # the tests use small stand-ins (setUpModule)

# A stand-in for Umbra Effects Setup (tools/zgo_setup/zgo-setup-linux in the ZGO folder): the
# command line Umbra Setup uses, and its RESULT lines. FAKE_ZGO_* in the environment steer it.
FAKE_ZGO = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
def arg(k):
    return a[a.index(k) + 1] if k in a else None
log = open(arg("--log"), "a")
def say(s):
    log.write(s + "\n")
    log.flush()
if os.environ.get("FAKE_ZGO_CALLS"):
    with open(os.environ["FAKE_ZGO_CALLS"], "a") as f:
        f.write(" ".join(x for x in a if x.startswith("--") and x not in
                         ("--mod", "--mo2", "--profile", "--game", "--log", "--yes")) + "\n")
mod = arg("--mod")
rep_p = os.path.join(mod, "zgo_bake_report.json")
def body_state():
    try:
        rep = json.load(open(rep_p))
    except (OSError, ValueError):
        return "none"
    if not all(os.path.isfile(os.path.join(mod, *f.split("/"))) for f in rep.get("files", [])):
        return "broken"
    return "stale" if os.environ.get("FAKE_ZGO_STALE") else "ok"
if "--check" in a:
    st = body_state()
    say("Your bake is up to date." if st == "ok" else "Not baked yet.")
    say("RESULT body=%s props=%s kid=ok" % (st, os.environ.get("FAKE_ZGO_PROPS", "none")))
    sys.exit(0 if st == "ok" else 1)
if "--match" in a:
    rep = json.load(open(os.path.join(arg("--match"), "zgo_bake_report.json")))
    assert rep["profile"] == arg("--profile"), rep["profile"]           # Setup put the names in
    assert rep["inputs"]["zgo_mod"] == os.path.basename(mod), rep["inputs"]["zgo_mod"]
    assert rep["inputs"]["mo2"] == arg("--mo2"), rep["inputs"]["mo2"]
    fp = rep["inputs"]["fingerprint"]
    if os.environ.get("FAKE_ZGO_MATCH") == "old":                       # before 1.8.0: no --match
        say("Traceback (most recent call last):")
        say("TypeError: check() got an unexpected keyword argument 'report'")
        sys.exit(1)
    if os.environ.get("FAKE_ZGO_MATCH", "yes") == "yes":
        say("The prebaked bake fits your mod list.")
        say("RESULT match=yes fingerprint=" + fp)
        sys.exit(0)
    say("The prebaked bake does not fit your mod list.")
    say('  - Mod "Extra Models" adds 2 models the bake does not have (e.g. actors\\x).')
    say("RESULT match=no fingerprint=" + fp)
    sys.exit(1)
if "--bake" in a:
    if os.environ.get("FAKE_ZGO_BAKE_FAILS"):
        say("Problem: not enough free space")
        sys.exit(2)
    for i in (1, 2):
        say("  [models] Baking %d/2 actors\\x" % i)
    os.makedirs(os.path.join(mod, "gamedata", "meshes"), exist_ok=True)
    open(os.path.join(mod, "gamedata", "meshes", "m.ogf"), "wb").write(b"baked here")
    json.dump({"profile": arg("--profile"), "files": ["gamedata/meshes/m.ogf"], "inputs": {"fingerprint": "f" * 32}},
              open(rep_p, "w"))
    say("Baked 2 models (0 skipped, 0 could not be baked)")
    sys.exit(0)
if "--props" in a or "--kid-particles-apply" in a:
    say("done")
    sys.exit(0)
sys.exit(2)
'''
BAKE_FP = "ab" * 16

FAKE_CLI = r'''
import json, os, sys
a = sys.argv[1:]
def out(o): print(json.dumps(o), flush=True)
cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME", "."), "stalker-gamma", "settings.json")
if a[:2] == ["config", "create"]:
    d = dict(zip(a[2::2], a[3::2]))
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    json.dump(d, open(cfg, "w"))
    out({"type": "result", "command": "config create", "success": True, "exitCode": 0}); sys.exit(0)
if a[:2] == ["config", "use"] or a[:2] == ["config", "list"]:
    sys.exit(0)
if a[0] == "full-install":
    d = json.load(open(cfg))
    assert "--mod-pack-maker-path" in a and "--json" in a, a
    assert "--skip-extract-on-hash-match" not in a, a      # it skips archives a shared cache already has
    assert d["--stalker-gamma-repo-branch"] == "abc123", d
    for i in range(3):
        out({"type": "progress", "name": "addon%d" % i, "operation": "Download", "progress": 50.0,
             "complete": i, "total": 3, "url": "", "archiveName": "", "downloadPath": "", "extractPath": ""})
    out({"type": "log", "level": "Information", "message": "installing", "timestamp": "x"})
    g, an = d["--gamma"], d["--anomaly"]
    os.makedirs(os.path.join(g, "profiles", "G.A.M.M.A"), exist_ok=True)
    os.makedirs(os.path.join(g, "mods", "GAMMA base"), exist_ok=True)
    os.makedirs(os.path.join(an, "bin"), exist_ok=True)
    open(os.path.join(an, "bin", "AnomalyDX11.exe"), "wb").write(b"vanilla")
    if not os.environ.get("FAKE_CLI_NO_ANOMALY"):        # (as when it skipped extracting Anomaly)
        os.makedirs(os.path.join(an, "db"), exist_ok=True)
        for n in ("AnomalyLauncher.exe", "fsgame.ltx", os.path.join("db", "files.db0")):
            open(os.path.join(an, n), "wb").write(b"anomaly")
    open(os.path.join(g, "ModOrganizer.exe"), "wb").write(b"")
    open(os.path.join(g, "profiles", "G.A.M.M.A", "modlist.txt"), "w").write("+GAMMA base\r\n")
    open(os.path.join(g, "profiles", "G.A.M.M.A", "settings.ini"), "w").write("[General]\r\n")
    win = "Z:" + os.path.abspath(an).replace("/", "\\\\")
    open(os.path.join(g, "ModOrganizer.ini"), "w").write(
        "[General]\r\nselected_profile=@ByteArray(G.A.M.M.A)\r\ngamePath=@ByteArray(%s)\r\n" % win)
    out({"type": "result", "command": "full-install", "success": True, "exitCode": 0}); sys.exit(0)
sys.exit(2)
'''


def write(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    if mtime:
        os.utime(path, (mtime, mtime))


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def make_ini(mo2_dir, anomaly):
    # the folder as MO2 (a Windows program) writes it: Wine's Z: drive for a Linux path, with
    # MO2's doubled backslashes
    p = os.path.abspath(anomaly)
    win = (p if os.name == "nt" else "Z:" + p).replace("/", "\\").replace("\\", "\\\\")
    write(os.path.join(mo2_dir, "ModOrganizer.ini"),
          "[General]\r\nselected_profile=@ByteArray(G.A.M.M.A)\r\ngamePath=@ByteArray(%s)\r\n[Settings]\r\nx=1\r\n"
          % win)


def make_source(base):
    """A fake tested MO2 instance + Anomaly + pack definition; returns the toml path."""
    src_mo2 = os.path.join(base, "src", "gamma")
    src_an = os.path.join(base, "src", "anomaly")
    mods = os.path.join(src_mo2, "mods")
    write(os.path.join(mods, "Visual Mod", "gamedata", "textures", "a.dds"), os.urandom(3000), 1700000000)
    write(os.path.join(mods, "Visual Mod", "gamedata", "textures", "b.dds"), b"B" * 5000, 1700000100)
    write(os.path.join(mods, "Visual Mod", "gamedata", "textures", "hidden.dds.mohidden"), b"x")
    write(os.path.join(mods, "Visual Mod", "meta.ini"),
          "[General]\r\ngameName=stalkeranomaly\r\nversion=2.0\r\ninstallationFile=Z:/home/someone/x.7z\r\n"
          "url=https://www.moddb.com/mods/x\r\n")
    # a ZGO-like layer with its bake, whose report records this machine's paths and names
    zgo = os.path.join(mods, "Gore")
    write(os.path.join(zgo, "zgo_bake_report.json"), json.dumps({
        "profile": "G.A.M.M.A", "time": "2026-11-15 12:00:00", "baked": 1,
        "files": ["gamedata/meshes/m.ogf", "gamedata/scripts/zgo_bake_inputs.script"],
        "inputs": {"profile": "G.A.M.M.A", "mo2": src_mo2, "mo2_game": "Z:" + src_mo2.replace("/", "\\"),
                   "zgo_mod": "Gore", "fingerprint": BAKE_FP, "baker": 3, "zgo": "1.8.0"}}))
    write(os.path.join(zgo, "gamedata", "scripts", "zgo_bake_inputs.script"),
          'profile = "G.A.M.M.A"\nmo2 = "%s"\nzgo_mod = "Gore"\n'
          % ("Z:" + src_mo2.replace("/", "\\")).replace("\\", "\\\\"))
    write(os.path.join(zgo, "notes.txt"), "baked from %s/mods/Visual Mod\n" % src_mo2)
    write(os.path.join(zgo, "dev", "secret.txt"), "dev only")
    write(os.path.join(zgo, "gamedata", "meshes", "m.ogf"), os.urandom(2000))
    write(os.path.join(zgo, "meta.ini"), "[General]\r\nversion=d2026.11.14\r\n")     # MO2's date stamp
    write(os.path.join(zgo, "gamedata", "scripts", "zgo_main.script"), 'local x = 1\nVERSION = "1.7.1"\n')
    # Linux programs that lost their mode bits in an MO2 install (written rw-r--r--)
    write(os.path.join(zgo, "tools", "zgo_setup", "zgo-setup-linux"), FAKE_ZGO)
    write(os.path.join(zgo, "tools", "zgo_setup", "zgo-setup.sh"), "#!/bin/sh\nexec true\n")
    for fn in ("zgo-setup-linux", "zgo-setup.sh"):
        os.chmod(os.path.join(zgo, "tools", "zgo_setup", fn), 0o644)
    write(os.path.join(mods, "HD Faces", "gamedata", "textures", "face.dds"), b"H" * 4000)
    write(os.path.join(mods, "GAMMA base", "gamedata", "x.ltx"), b"base")
    write(os.path.join(src_mo2, "profiles", "G.A.M.M.A", "modlist.txt"),
          "# This file was automatically generated by Mod Organizer.\r\n+Gore 1.6 (old)\r\n-Gore\r\n"
          "+Visual Mod\r\n-Someone Elses Disabled\r\n+GAMMA base\r\n")
    write(os.path.join(src_an, "bin", "AnomalyDX11.exe"), b"vanilla")
    # an anomaly-target layer from an archive (like xrRazom's engine)
    arc = os.path.join(base, "src", "engine.zip")
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("Anomaly/bin/AnomalyDX11.exe", b"coop engine")
        z.writestr("Anomaly/bin/steam_api64.dll", b"steam")
        z.writestr("Anomaly/gamedata/configs/x.ltx", b"not this")
        z.writestr("readme.txt", b"no")
    pk = os.path.join(base, "pack")
    write(os.path.join(pk, "gamma", "modpack_maker_list.txt"), "maker\n")
    write(os.path.join(pk, "gamma", "modlist.txt"), "+GAMMA base\n")
    write(os.path.join(pk, "credits.toml"), '''
[[credit]]
id = "visual"
mod = "Visual Mod"
authors = ["Someone"]
url = "https://example.org/visual"
role = "extra"
[[credit]]
id = "gore"
mod = "Gore"
authors = ["Umbra team"]
role = "extra"
''')
    toml = os.path.join(pk, "umbra.toml")
    write(toml, '''
[pack]
name = "Umbra"
version = "0.0.1"
channel = "stable"
[source]
mo2 = "../src/gamma"
anomaly = "../src/anomaly"
profile = "G.A.M.M.A"
[gamma]
cli_version = "9.9"
mo2_profile = "G.A.M.M.A"
modpack_maker_list = "gamma/modpack_maker_list.txt"
modlist = "gamma/modlist.txt"
[gamma.cli.linux-x64]
url = "https://example.invalid/cli"
sha256 = "%s"
[gamma.repos]
stalker-gamma = "abc123"
[profile]
name = "Umbra"
base = "G.A.M.M.A"
drop = ["Gore 1.6 (old)"]
[[option]]
id = "hd"
title = "HD faces"
description = "4K faces"
default = false
[[layer]]
id = "gore"
folder = "Gore"
zgo = true
credit = "gore"
[[layer]]
id = "hd-faces"
folder = "HD Faces"
option = "hd"
credit = "visual"
[[layer]]
id = "visual"
folder = "Visual Mod"
credit = "visual"
[[layer]]
id = "engine"
target = "anomaly"
archive = "../src/engine.zip"
archive_root = "Anomaly"
archive_include = ["bin/"]
credit = "visual"
''' % ("0" * 64))
    return toml


def build(toml, out, volume=4096):
    b = pack_build.Builder(pack_build.load_def(toml), out, volume, 2, False)
    return b.build()


def fake_pe(imports):
    """The headers of a 64-bit Windows program importing these DLLs (enough for an import-table reader)."""
    sec_raw, sec_va = 0x200, 0x1000
    names_at = 20 * (len(imports) + 1)
    names, rvas = bytearray(), []
    for n in imports:
        rvas.append(sec_va + names_at + len(names))
        names += n.encode() + b"\0"
    body = b"".join(struct.pack("<IIIII", 0, 0, 0, r, 0) for r in rvas) + b"\0" * 20 + bytes(names)
    size = max(0x200, len(body))
    hdr = bytearray(sec_raw)
    hdr[0:2] = b"MZ"
    struct.pack_into("<I", hdr, 0x3C, 0x40)
    hdr[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", hdr, 0x44, 0x8664, 1, 0, 0, 0, 240, 0x22)
    opt = 0x58
    struct.pack_into("<H", hdr, opt, 0x20B)
    struct.pack_into("<I", hdr, opt + 108, 16)
    struct.pack_into("<II", hdr, opt + 112 + 8, sec_va, len(body))
    struct.pack_into("<8sIIII", hdr, opt + 240, b".idata", size, sec_va, size, sec_raw)
    return bytes(hdr) + body + b"\0" * (size - len(body))


COOP_EXE = fake_pe(["KERNEL32.dll", "MSVCP140.dll", "VCRUNTIME140.dll", "steam_api64.dll"])
DX12_EXE = fake_pe(["KERNEL32.dll", "MSVCP140.dll", "MSVCP140_2.dll", "tbb12.dll"])
SETTINGS_V1 = "[mcm]\r\n        zgo/blood = 2\r\n        hud/minimap = false\r\n"


def make_engine_zip(path):
    """A stand-in for the Slop-Ray full pack: engine bins, a db0, MO2 mods zipped inside, scripts."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("meta.ini", "[General]\r\nversion=2.0.1\r\ninstallationFile=C:/Users/someone/x.zip\r\n")
        z.writestr("gamedata\\scripts\\", b"")                       # a folder written the Windows way
        z.writestr("gamedata\\scripts\\bodycam.script", b"-- bodycam")
        z.writestr("README.md", b"bodycam patch")
    r = "Full Pack/"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(r + "STEP 1 - Engine/bin/AnomalyDX11AVX.exe", b"slop-ray dx11, same name as the co-op exe")
        z.writestr(r + "STEP 1 - Engine/bin/msvcp140.dll", fake_pe(["KERNEL32.dll"]))
        z.writestr(r + "STEP 1 - Engine/bin/nvngx_dlss.dll", b"dlss runtime")
        z.writestr(r + "STEP 1 - Engine/db/mods/00_modded_exes_gamedata.db0", b"engine gamedata")
        z.writestr(r + "STEP 2 - MO2 mods/1 - Bodycam.zip", inner.getvalue())
        z.writestr(r + "STEP 7 - Frame gen/bin/AnomalyDX12.exe", DX12_EXE)
        z.writestr(r + "STEP 7 - Frame gen/bin/tbb12.dll", b"tbb")
        z.writestr(r + "STEP 7 - Frame gen/bin/D3D12/D3D12Core.dll", b"agility")
        z.writestr(r + "STEP 6 - OptiScaler/dxgi.dll", b"proxy")
        z.writestr(r + "INSTALL - double-click me.bat", b"@echo never run")
    return packdef.sha256_file(path)


PROFILES_TOML = '''
[user_ltx]
lines = ["r3_msaa st_opt_off", "bind jump kSPACE", "rs_v_sync off"]

[[option]]
id = "solo"
title = "Umbra Solo"
description = "single-player engine"
default = false

[[profiles]]
id = "solo"
name = "Umbra Solo"
option = "solo"
off = ["coop"]
requires = ["avx2"]
steam = { title = "Umbra Solo", run = "Anomaly (DX12 + Frame Gen)" }
[profiles.user_ltx]
file = "user_solo.ltx"
seed = "user.ltx"
lines = ["r_upscaler fsr3", "r_frame_generation fsr", "r3_msaa st_opt_off", "rs_v_sync off"]
[[profiles.executable]]
title = "Anomaly (DX12 + Frame Gen)"
binary = "bin/AnomalyDX12.exe"
cwd = "bin"
arguments = "-ltx user_solo.ltx"

[[dependency]]
id = "model-drop"
profile = "solo"
title = "Model Drop 15"
url = "https://example.invalid/model-drop"
required = true
fetched = false

[sources.slop]
file = "../cache/engine-full.zip"
url = "https://example.invalid/engine-full.zip"
sha256 = "%(sha)s"
version = "0.6-dev"
root = "Full Pack"

[[layer]]
id = "settings"
folder = "Umbra Settings"
preserve = true
credit = "visual"
[layer.files]
"gamedata/configs/axr_options.ltx" = "../src/gamma/mods/GAMMA MCM values/gamedata/configs/axr_options.ltx"

[[layer]]
id = "solo-bodycam"
profile = "solo"
folder = "1 - Bodycam"
source = "slop"
archive_root = "STEP 2 - MO2 mods"
archive_member = "1 - Bodycam.zip"
credit = "visual"

[[layer]]
id = "solo-data"
profile = "solo"
folder = "Slop-Ray Engine gamedata"
source = "slop"
archive_root = "STEP 1 - Engine"
archive_include = ["db/"]
credit = "visual"

[[layer]]
id = "atmos"
folder = "Atmos"
credit = "visual"

[[layer]]
id = "coop"
folder = "Coop Mod"
credit = "visual"

[[layer]]
id = "solo-engine"
profile = "solo"
target = "anomaly"
source = "slop"
archive_root = "STEP 7 - Frame gen"
archive_include = ["bin/"]
credit = "visual"

[[layer]]
id = "solo-dlss"
profile = "solo"
target = "anomaly"
source = "slop"
archive_root = "STEP 1 - Engine"
archive_include = ["bin/nvngx_dlss.dll"]
credit = "visual"
'''


def make_profiles_source(base):
    """make_source plus settings, machine-specific files, a co-op mod and a Solo profile."""
    toml = make_source(base)
    mods = os.path.join(base, "src", "gamma", "mods")
    write(os.path.join(mods, "GAMMA MCM values", "gamedata", "configs", "axr_options.ltx"), SETTINGS_V1)
    atmos = os.path.join(mods, "Atmos")
    write(os.path.join(atmos, "gamedata", "shaders", "r3", "deffer_impl_flat.ps"), b"shader")
    write(os.path.join(atmos, "gamedata", "textures", "logs", "keep.dds"), b"a texture in a folder named logs")
    write(os.path.join(atmos, "appdata", "Atmos_Cold.ltx"), b"preset")            # a mod's preset: shipped
    for junk in ("appdata/user.ltx", "appdata/xrrazom_lan_id.bin", "appdata/savedgames/quick.scop",
                 "appdata/shaders_cache/r3/x.bin", "appdata/logs/xray.log", "logs/build.log",
                 "xrrazom_lan_id.bin", "appdata/presets/deep.ltx", "umbra.layer.json"):
        write(os.path.join(atmos, *junk.split("/")), b"this machine's")
    write(os.path.join(mods, "Coop Mod", "gamedata", "scripts", "xrr_core.script"), "XrrNet()")
    write(os.path.join(base, "src", "gamma", "profiles", "G.A.M.M.A", "modlist.txt"),
          "# This file was automatically generated by Mod Organizer.\r\n+Gore 1.6 (old)\r\n-Gore\r\n"
          "+Visual Mod\r\n+Coop Mod\r\n+Atmos\r\n+GAMMA MCM values\r\n-Someone Elses Disabled\r\n+GAMMA base\r\n")
    write(os.path.join(base, "src", "anomaly", "bin", "AnomalyDX11AVX.exe"), COOP_EXE)   # the co-op exe
    sha = make_engine_zip(os.path.join(base, "cache", "engine-full.zip"))
    with open(toml, "a", encoding="utf-8") as f:
        f.write(PROFILES_TOML % {"sha": sha})
    return toml


REALISTIC_INI = r'''[General]
gameName=STALKER Anomaly
selected_profile=@ByteArray(G.A.M.M.A)
gamePath=@ByteArray(%(an_ini)s)
version=2.5.2
first_start=false

[PluginPersistance]
Python%%20Proxy\tryInit=false

[customExecutables]
size=3
1\arguments=
1\binary=%(an)s/AnomalyLauncher.exe
1\hide=false
1\ownicon=true
1\steamAppID=
1\title=Anomaly Launcher
1\toolbar=false
1\workingDirectory=%(an)s
2\arguments=
2\binary=%(an)s/bin/AnomalyDX11AVX.exe
2\hide=false
2\ownicon=true
2\steamAppID=
2\title=Anomaly (DX11-AVX)
2\toolbar=false
2\workingDirectory=%(an)s/bin
3\arguments=\"%(an_esc)s\"
3\binary=%(mo2)s/explorer++/Explorer++.exe
3\hide=false
3\ownicon=true
3\steamAppID=
3\title=Explore Virtual Folder
3\toolbar=false
3\workingDirectory=%(mo2)s/explorer++

[recentDirectories]
size=1
1\name=installMod
1\directory=Z:/home/player/Desktop

[Widgets]
MainWindow_executablesListBox_index=1
MainWindow_modList_index="@Variant(\0\0\0\t\0\0\0\x1\0\0\0\n\0\0\0,\0G\0.\0\x41)"
'''


def realistic_ini(mo2_dir, anomaly):
    """ModOrganizer.ini as MO2 2.5.2 writes it for G.A.M.M.A. under Wine (CRLF line ends)."""
    an = mo2.mo2_path(anomaly, True)
    text = REALISTIC_INI % {"an": an, "mo2": mo2.mo2_path(mo2_dir, True),
                            "an_ini": an.replace("/", "\\\\"), "an_esc": an.replace("/", "\\\\")}
    write(os.path.join(mo2_dir, "ModOrganizer.ini"), text.replace("\n", "\r\n"))


def make_target(base, with_conflict=False):
    """An existing fake G.A.M.M.A. install to add Umbra to."""
    t_mo2 = os.path.join(base, "player", "gamma")
    t_an = os.path.join(base, "player", "anomaly")
    write(os.path.join(t_mo2, "ModOrganizer.exe"), b"")
    write(os.path.join(t_mo2, "profiles", "G.A.M.M.A", "modlist.txt"), "+GAMMA base\r\n")
    write(os.path.join(t_mo2, "profiles", "G.A.M.M.A", "settings.ini"), "[General]\r\nLocalSettings=true\r\n")
    write(os.path.join(t_mo2, "mods", "GAMMA base", "gamedata", "x.ltx"), b"base")
    write(os.path.join(t_an, "bin", "AnomalyDX11.exe"), b"vanilla")
    make_ini(t_mo2, t_an)
    if with_conflict:
        write(os.path.join(t_mo2, "mods", "Visual Mod", "mine.txt"), b"the player's own")
    return t_mo2, t_an


class Server:
    """A local HTTP server for the built pack (Range requests supported)."""

    def __init__(self, root):
        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(s, *a, **k):
                super().__init__(*a, directory=root, **k)

            def log_message(s, *a):
                pass

            def send_head(s):
                rng = s.headers.get("Range")
                path = s.translate_path(s.path)
                if not rng or not os.path.isfile(path):
                    return super().send_head()
                start = int(rng.split("=")[1].split("-")[0])
                size = os.path.getsize(path)
                f = open(path, "rb")
                f.seek(start)
                s.send_response(206)
                s.send_header("Content-Length", str(size - start))
                s.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
                s.end_headers()
                return f

        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_install(opts):
    logs = []
    inst = install.Installer(opts, log=lambda m, l="info": logs.append((l, m)))
    plan = inst.plan()
    if not plan.ok():
        raise AssertionError(plan.problems)
    st = inst.run(plan)
    return st, logs


class AuthorSite:
    """A stand-in for ModDB: a download page with a mirror link on it, the mirror's redirect,
    then the file (Range supported). mode: "ok", "blocked" (403, as Cloudflare does) or
    "nolink" (a page without a download link)."""

    def __init__(self, data):
        self.data, self.mode, self.hits = data, "ok", []
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(s, *a):
                pass

            def _send(s, code, body=b"", ctype="text/html; charset=utf-8", extra=()):
                s.send_response(code)
                s.send_header("Content-Type", ctype)
                s.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    s.send_header(k, v)
                s.end_headers()
                s.wfile.write(body)

            def do_GET(s):
                site.hits.append(s.path)
                if s.path == "/addons/start/1":
                    if site.mode == "blocked":
                        return s._send(403, b"<html>Just a moment...</html>")
                    if site.mode == "nolink":
                        return s._send(200, b"<html>no link here</html>")
                    return s._send(200, b'<html><a href="/downloads/mirror/1/2/abc&amp;x=1">download xr.zip</a>'
                                        b'</html>')
                if s.path == "/downloads/mirror/1/2/abc&x=1":
                    return s._send(302, extra=[("Location", "/cdn/xr.zip")])
                if s.path == "/cdn/xr.zip":
                    rng = s.headers.get("Range")
                    start = int(rng.split("=")[1].split("-")[0]) if rng else 0
                    extra = [("Content-Range", "bytes %d-%d/%d" % (start, len(site.data) - 1, len(site.data)))] \
                        if rng else []
                    return s._send(206 if rng else 200, site.data[start:], "application/zip", extra)
                return s._send(404)

        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def downloads(self):
        return sum(1 for h in self.hits if h == "/cdn/xr.zip")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_origin_source(base):
    """make_source, with xrRazom-like layers from an archive players download from its author's
    site (the AuthorSite); returns (toml, site, the archive's bytes)."""
    toml = make_source(base)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Anomaly/bin/AnomalyDX11.exe", b"xr engine")
        z.writestr("Anomaly/gamedata/configs/xr_engine.ltx", b"xr config")
        z.writestr("gamedata/", b"")
        z.writestr("gamedata/scripts/xr.script", b"xr script")
        z.writestr("gamedata/configs/shared.ltx", b"plain")
        z.writestr("gamma/gamedata/configs/shared.ltx", b"for gamma")
        z.writestr("fomod/ModuleConfig.xml", b"<config/>")
        z.writestr("readme.txt", b"read me")
    data = buf.getvalue()
    write(os.path.join(base, "src", "xr.zip"), data)
    site = AuthorSite(data)
    with open(toml, encoding="utf-8") as f:
        text = f.read()
    engine = text[text.index('[[layer]]\nid = "engine"'):]
    text = text.replace(engine, """[[layer]]
id = "xr"
folder = "XR Co-op"
source = "xr"
take = [["gamedata/", "gamedata/"], ["gamma/gamedata/", "gamedata/"]]
credit = "visual"
[[layer]]
id = "engine"
target = "anomaly"
source = "xr"
take = [["Anomaly/", ""]]
credit = "visual"
[sources.xr]
file = "../src/xr.zip"
page = "%(u)s/mods/xr"
download = "%(u)s/addons/start/1"
sha256 = "%(sha)s"
version = "1.5"
site = "TestDB"
players_download = true
""" % {"u": site.url, "sha": hashlib.sha256(data).hexdigest()})
    write(toml, text)
    return toml, site, data


class Origins(unittest.TestCase):
    """A mod whose author asks that it be downloaded from their own page (xrRazom, ModDB):
    never in Umbra's own files; players' Setup gets it there."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="umbra_test_")
        cls.toml, cls.site, cls.data = make_origin_source(cls.d)
        cls.dist = os.path.join(cls.d, "dist")
        cls.pack = build(cls.toml, cls.dist)
        cls.sha = hashlib.sha256(cls.data).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.site.close()
        shutil.rmtree(cls.d, ignore_errors=True)

    def setUp(self):
        for target in ("umbra_installer.mo2.running", "umbra_installer.steam.running"):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.downloads = tempfile.mkdtemp(prefix="umbra_dl_", dir=self.d)
        p = mock.patch("umbra_installer.fetch.downloads_dirs", return_value=[self.downloads])
        p.start()
        self.addCleanup(p.stop)
        self.site.mode, self.site.hits[:] = "ok", []

    def target(self, name):
        return make_target(os.path.join(self.d, name))

    def opts(self, t_mo2):
        return install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)

    def test_the_pack_names_the_authors_file_and_never_carries_it(self):
        by_id = {L["id"]: L for L in self.pack["layers"]}
        for lid, take in (("xr", [["gamedata/", "gamedata/"], ["gamma/gamedata/", "gamedata/"]]),
                          ("engine", [["Anomaly/", ""]])):
            (v,) = by_id[lid]["volumes"]
            self.assertEqual(v["path"], "origins/%s.zip" % self.sha)
            self.assertEqual((v["sha256"], v["size"], v["take"]), (self.sha, len(self.data), take))
            self.assertEqual(v["origin"], {"name": "xr.zip", "site": "TestDB", "page": self.site.url + "/mods/xr",
                                           "download": self.site.url + "/addons/start/1"})
        self.assertEqual((by_id["xr"]["files"], by_id["engine"]["files"]), (2, 2))
        self.assertIn("url=%s/mods/xr" % self.site.url, by_id["xr"]["meta_ini"])
        self.assertIn("version=1.5", by_id["xr"]["meta_ini"])
        self.assertNotIn("meta_ini", by_id["engine"])
        # not built, not uploaded, counted once in the download size
        for dp, _, fns in os.walk(self.dist):
            for fn in fns:
                with open(os.path.join(dp, fn), "rb") as f:
                    self.assertNotEqual(f.read(), self.data, fn)
        import b2_upload
        self.assertFalse([o for o in b2_upload.object_refs(self.pack) if o["sha256"] == self.sha])
        every = [v for L in packdef.chosen_layers(self.pack) for v in L["volumes"]]
        self.assertEqual(packdef.total_download(self.pack), sum(v["size"] for v in every) - len(self.data))
        self.assertEqual(len(packdef.origins(self.pack)), 1)
        self.assertEqual(self.site.hits, [])          # building it downloads nothing

    def test_setup_downloads_it_from_the_authors_site(self):
        t_mo2, t_an = self.target("from-site")
        inst = install.Installer(self.opts(t_mo2))
        plan = inst.plan()
        self.assertTrue(any('"xr.zip"' in w and "comes from TestDB" in w for w in plan.warnings), plan.warnings)
        _, logs = run_install(self.opts(t_mo2))
        self.assertEqual(self.site.hits[:3], ["/addons/start/1", "/downloads/mirror/1/2/abc&x=1", "/cdn/xr.zip"])
        self.assertTrue(any("from TestDB" in m and "as its author asks" in m for _, m in logs), logs)
        mod = os.path.join(t_mo2, "mods", "XR Co-op")
        self.assertEqual(read_bytes(os.path.join(mod, "gamedata", "configs", "shared.ltx")), b"for gamma")
        self.assertEqual(read_bytes(os.path.join(mod, "gamedata", "scripts", "xr.script")), b"xr script")
        self.assertFalse(os.path.exists(os.path.join(mod, "fomod")))
        self.assertFalse(os.path.exists(os.path.join(mod, "gamma")))
        with open(os.path.join(mod, "meta.ini"), encoding="utf-8") as f:
            self.assertIn("url=%s/mods/xr" % self.site.url, f.read())
        self.assertEqual(read_bytes(os.path.join(t_an, "bin", "AnomalyDX11.exe")), b"xr engine")
        self.assertEqual(read_bytes(os.path.join(t_an, "gamedata", "configs", "xr_engine.ltx")), b"xr config")
        self.assertFalse(os.path.exists(os.path.join(t_an, "readme.txt")))
        self.assertEqual(self.site.downloads(), 1)    # one download for both layers
        # a second run: everything in place, nothing downloaded again
        _, logs = run_install(self.opts(t_mo2))
        self.assertEqual(self.site.downloads(), 1)
        self.assertIn(("info", "Already in place: engine"), logs)
        self.assertIn(("info", "Already in place: XR Co-op"), logs)
        # the launcher puts the engine back from its cache, as for Umbra's own layers
        write(os.path.join(t_an, "gamedata", "configs", "xr_engine.ltx"), b"gamma's")
        linst = launcher.Install(os.path.join(t_mo2, ".umbra"))
        self.assertEqual(linst.anomaly_changed(), {"engine": ["gamedata/configs/xr_engine.ltx"]})
        self.assertEqual(linst.anomaly_restore(), ["engine"])
        self.assertEqual(read_bytes(os.path.join(t_an, "gamedata", "configs", "xr_engine.ltx")), b"xr config")
        self.assertEqual(self.site.downloads(), 1)

    def test_the_copy_in_downloads_is_used(self):
        t_mo2, _ = self.target("from-downloads")
        write(os.path.join(self.downloads, "xr (1).zip"), self.data)
        write(os.path.join(self.downloads, "xr.zip"), b"another file with the same name")
        _, logs = run_install(self.opts(t_mo2))
        self.assertEqual(self.site.hits, [])
        self.assertTrue(any('Using the "xr.zip" you downloaded' in m and "xr (1).zip" in m for _, m in logs), logs)
        self.assertTrue(os.path.exists(os.path.join(self.downloads, "xr (1).zip")))     # left where it was

    def test_a_site_that_says_no_sends_the_player_to_its_page(self):
        for mode, why in (("blocked", "403"), ("nolink", "no download link")):
            self.site.mode = mode
            t_mo2, _ = self.target("blocked-" + mode)
            with self.assertRaises(fetch.OriginNeeded) as cm:
                run_install(self.opts(t_mo2))
            e = cm.exception
            self.assertEqual((e.name, e.page, e.site, e.folder), ("xr.zip", self.site.url + "/mods/xr", "TestDB",
                                                                   self.downloads))
            self.assertIn(why, str(e))
            self.assertIn("press Download there", str(e))
            # the player downloads it in the browser; Try again finds it
            write(os.path.join(self.downloads, "xr.zip"), self.data)
            run_install(self.opts(t_mo2))
            self.assertEqual(read_bytes(os.path.join(t_mo2, "mods", "XR Co-op", "gamedata", "scripts", "xr.script")),
                             b"xr script")
            os.remove(os.path.join(self.downloads, "xr.zip"))

    def test_a_site_that_says_no_hands_it_to_the_browser_and_setup_carries_on(self):
        self.site.mode = "blocked"
        t_mo2, _ = self.target("browser")
        seen, steps = [], []

        def wait_for_download(e):              # the window: the browser downloads it, a moment later
            seen.append((e.name, e.download))
            threading.Timer(0.3, lambda: write(os.path.join(self.downloads, "xr.zip"), self.data)).start()
            fetch.wait_for(e.obj, lambda: False, every=0.1)
        inst = install.Installer(self.opts(t_mo2), progress=lambda st, frac, msg="": steps.append(st),
                                 wait_for_download=wait_for_download)
        plan = inst.plan()
        self.assertEqual([x[0] for x in plan.steps][:2], ["pack", "origins"])
        inst.run(plan)
        self.assertEqual(seen, [("xr.zip", self.site.url + "/addons/start/1")])
        self.assertEqual(read_bytes(os.path.join(t_mo2, "mods", "XR Co-op", "gamedata", "scripts", "xr.script")),
                         b"xr script")
        first = [x for i, x in enumerate(steps) if x not in steps[:i]]
        self.assertEqual(first[:3], ["pack", "origins", "download"])       # before everything else

    def test_waiting_for_the_browser(self):
        obj = packdef.origins(self.pack)[0]
        write(os.path.join(self.downloads, "xr.zip"), b"x" * len(self.data))      # the right size, not the file
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        with self.assertRaises(fetch.Cancelled):
            fetch.wait_for(obj, stop.is_set, every=0.1)
        later = os.path.join(self.d, "elsewhere")
        threading.Timer(0.2, lambda: write(os.path.join(later, "xr (2).zip"), self.data)).start()
        got = fetch.wait_for(obj, lambda: False, lambda: [self.downloads, later], every=0.1)
        self.assertEqual(got, os.path.join(later, "xr (2).zip"))

    def test_without_authors_downloads_there_is_no_such_step(self):
        t_mo2, _ = make_target(os.path.join(self.d, "plain"))
        d = tempfile.mkdtemp(prefix="umbra_plain_", dir=self.d)
        dist = os.path.join(d, "dist")
        build(make_source(d), dist)
        plan = install.Installer(install.Options(mode="existing", mo2_dir=t_mo2, source=dist, steam=False)).plan()
        self.assertNotIn("origins", [x[0] for x in plan.steps])

    def test_a_broken_download_resumes(self):
        t_mo2, _ = self.target("resume")
        part = os.path.join(t_mo2, ".umbra", "cache", "origins", "%s.zip.part" % self.sha)
        write(part, self.data[:100])
        run_install(self.opts(t_mo2))
        cdn = [i for i, h in enumerate(self.site.hits) if h == "/cdn/xr.zip"]
        self.assertEqual(len(cdn), 1)
        self.assertEqual(read_bytes(os.path.join(t_mo2, ".umbra", "cache", "origins", "%s.zip" % self.sha)),
                         self.data)

    def test_take(self):
        with zipfile.ZipFile(io.BytesIO(self.data)) as z:
            got = [(rel, i.filename) for rel, i in layers.take_entries(z.infolist(), [["gamedata/", "gamedata/"],
                                                                                       ["gamma/gamedata/", "gamedata/"]])]
        self.assertEqual(got, [("gamedata/scripts/xr.script", "gamedata/scripts/xr.script"),
                               ("gamedata/configs/shared.ltx", "gamma/gamedata/configs/shared.ltx")])
        bad = zipfile.ZipInfo("gamedata/../../evil.txt")
        with self.assertRaises(layers.LayerError):
            layers.take_entries([bad], [["gamedata/", "gamedata/"]])

    def test_the_manifest_checks_an_authors_download(self):
        good = copy.deepcopy(self.pack)
        packdef.check_pack(good)
        for change in (lambda v: v["origin"].update(download="file:///etc/passwd"),
                       lambda v: v["origin"].update(name="../x.zip"),
                       lambda v: v.update(path="objects/%s.zip" % v["sha256"]),
                       lambda v: v.update(take=[["../", ""]]),
                       lambda v: v.update(take=[["gamedata", "gamedata/"]]),
                       lambda v: v.update(take="all")):
            p = copy.deepcopy(self.pack)
            change(p["layers"][[L["id"] for L in p["layers"]].index("xr")]["volumes"][0])
            with self.assertRaises(packdef.PackError):
                packdef.check_pack(p)
        p = copy.deepcopy(self.pack)
        del p["layers"][0]["volumes"][0]["path"]
        with self.assertRaises(packdef.PackError):
            packdef.check_pack(p)

    def test_the_definition_says_what_a_layer_takes(self):
        with open(self.toml, encoding="utf-8") as f:
            text = f.read()
        for old, new, err in (
                ('take = [["Anomaly/", ""]]\n', "", "say what the layer takes"),
                ('download = "%s/addons/start/1"\n' % self.site.url, "", "its page and its download link"),
                ('take = [["Anomaly/", ""]]\n', 'take = [["Anomaly/", ""]]\narchive_include = ["bin/"]\n',
                 "archive_include"),
                ('folder = "XR Co-op"\n', 'folder = "XR Co-op"\nfirst = true\n', "must come first")):
            bad = os.path.join(self.d, "bad.toml")
            write(bad, text.replace(old, new, 1))
            shutil.copy(os.path.join(os.path.dirname(self.toml), "credits.toml"), os.path.join(self.d, "credits.toml"))
            with self.assertRaisesRegex(SystemExit, re.escape(err)):
                pack_build.load_def(bad)


class PackBuild(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="umbra_test_")
        self.toml = make_source(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_pack(self):
        out = os.path.join(self.d, "dist")
        pack = build(self.toml, out)
        packdef.check_pack(pack)
        ids = [L["id"] for L in pack["layers"]]
        self.assertEqual(ids, ["gore", "hd-faces", "visual", "engine"])
        gore = pack["layers"][0]
        # ZGO's release rules: no dev/, and the paths file was tokenised
        names = set()
        for v in gore["volumes"]:
            with zipfile.ZipFile(os.path.join(out, v["path"])) as z:
                names |= set(z.namelist())
                for n in z.namelist():
                    data = z.read(n)
                    self.assertNotIn(os.path.join(self.d, "src").encode(), data, n)
        self.assertNotIn("dev/secret.txt", names)
        self.assertIn("meta.ini", names)
        self.assertEqual(gore["version"], "1.7.1")              # ZGO's own, not MO2's date stamp
        modes = {}
        for v in gore["volumes"]:
            with zipfile.ZipFile(os.path.join(out, v["path"])) as z:
                modes.update((i.filename, i.external_attr >> 16) for i in z.infolist())
        self.assertEqual(modes["tools/zgo_setup/zgo-setup-linux"], 0o755)   # programs packed runnable
        self.assertEqual(modes["tools/zgo_setup/zgo-setup.sh"], 0o755)
        self.assertEqual(gore["relocate"], ["notes.txt"])
        # the bake is not in the layer: it is its own download, in its own folder
        self.assertNotIn("gamedata/meshes/m.ogf", names)
        self.assertNotIn("zgo_bake_report.json", names)
        ref = gore["bake"]
        self.assertTrue(ref["path"].startswith("umbra-effects/%s/" % BAKE_FP), ref)
        with open(os.path.join(out, ref["path"]), "rb") as f:
            blob = f.read()
        self.assertEqual(packdef.sha256_bytes(blob), ref["sha256"])
        b = packdef.check_bake(json.loads(blob), ref)
        self.assertEqual((b["fingerprint"], b["models"], b["zgo"], b["files"]), (BAKE_FP, 1, "1.8.0", 3))
        self.assertEqual(b["relocate"], ["gamedata/scripts/zgo_bake_inputs.script", "zgo_bake_report.json"])
        with zipfile.ZipFile(os.path.join(out, b["report"]["path"])) as z:
            self.assertEqual(z.namelist(), ["zgo_bake_report.json"])         # the report alone, fetched first
            rep = json.loads(z.read("zgo_bake_report.json"))
        self.assertEqual((rep["profile"], rep["inputs"]["profile"], rep["inputs"]["zgo_mod"], rep["inputs"]["mo2"]),
                         ("@@UMBRA_PROFILE@@", "@@UMBRA_PROFILE@@", "@@UMBRA_ZGO_FOLDER@@", "@@UMBRA_MO2@@"))
        baked = set()
        for v in b["volumes"]:
            with zipfile.ZipFile(os.path.join(out, v["path"])) as z:
                baked |= set(z.namelist())
                if "gamedata/scripts/zgo_bake_inputs.script" in z.namelist():
                    script = z.read("gamedata/scripts/zgo_bake_inputs.script").decode()
                    self.assertIn('profile = "@@UMBRA_PROFILE@@"', script)
                    self.assertIn('zgo_mod = "@@UMBRA_ZGO_FOLDER@@"', script)
        self.assertEqual(baked, {"gamedata/meshes/m.ogf", "gamedata/scripts/zgo_bake_inputs.script"})
        visual = pack["layers"][2]
        vn, meta = [], None
        for v in visual["volumes"]:
            with zipfile.ZipFile(os.path.join(out, v["path"])) as z:
                vn += z.namelist()
                if "meta.ini" in z.namelist():
                    meta = z.read("meta.ini").decode()
        self.assertNotIn("gamedata/textures/hidden.dds.mohidden", vn)
        self.assertNotIn("someone", meta)
        self.assertIn("url=https://www.moddb.com/mods/x", meta)
        eng = pack["layers"][3]
        with zipfile.ZipFile(os.path.join(out, eng["volumes"][0]["path"])) as z:
            self.assertEqual(sorted(z.namelist()), ["bin/AnomalyDX11.exe", "bin/steam_api64.dll"])
        # the load order: layers on top in pack order, the old ZGO dropped
        with open(os.path.join(out, pack["profile"]["modlist"]["path"]), encoding="utf-8") as f:
            ml = f.read().splitlines()
        self.assertEqual(ml[1:5], ["+Gore", "+HD Faces", "+Visual Mod", "-Someone Elses Disabled"])
        self.assertNotIn("+Gore 1.6 (old)", ml)
        with open(os.path.join(out, "CREDITS.md")) as f:
            self.assertIn("[Visual Mod](https://example.org/visual) by Someone", f.read())

    def test_the_bake_alone(self):
        out = os.path.join(self.d, "bakeonly")
        b = pack_build.Builder(pack_build.load_def(self.toml), out, 4096, 2, False)
        ref, = b.bake_only()
        self.assertTrue(ref["path"].startswith("umbra-effects/%s/" % BAKE_FP))
        self.assertEqual([x for x in os.listdir(out) if not x.startswith(".")], ["umbra-effects"])     # no pack, no channel
        files = os.listdir(os.path.join(out, "umbra-effects", BAKE_FP))
        self.assertEqual(len(files), 3)                         # the manifest, the report, one volume
        # the pack built after it names the same bake and packs nothing again for it
        pack = pack_build.Builder(pack_build.load_def(self.toml), out, 4096, 2, False).build()
        self.assertEqual(pack["layers"][0]["bake"], ref)
        self.assertEqual(len(os.listdir(os.path.join(out, "umbra-effects", BAKE_FP))), 3)

    def test_a_bake_made_before_the_mod_was_renamed(self):
        with open(self.toml, encoding="utf-8") as f:
            toml = f.read()
        write(self.toml, toml.replace('folder = "Gore"\n', 'folder = "Gore"\nwas = ["Zone Gore"]\n', 1))
        gore = os.path.join(self.d, "src", "gamma", "mods", "Gore")
        rp = os.path.join(gore, "zgo_bake_report.json")
        with open(rp, encoding="utf-8") as f:
            rep = json.load(f)
        rep["inputs"]["zgo_mod"] = "Zone Gore"
        write(rp, json.dumps(rep))
        sp = os.path.join(gore, "gamedata", "scripts", "zgo_bake_inputs.script")
        with open(sp, encoding="utf-8") as f:
            script = f.read()
        write(sp, script.replace('zgo_mod = "Gore"', 'zgo_mod = "Zone Gore"'))
        out = os.path.join(self.d, "renamed")
        ref, = pack_build.Builder(pack_build.load_def(self.toml), out, 4096, 2, False).bake_only()
        with open(os.path.join(out, ref["path"]), encoding="utf-8") as f:
            b = json.load(f)
        with zipfile.ZipFile(os.path.join(out, b["report"]["path"])) as z:
            self.assertEqual(json.loads(z.read("zgo_bake_report.json"))["inputs"]["zgo_mod"], "@@UMBRA_ZGO_FOLDER@@")
        for v in b["volumes"]:
            with zipfile.ZipFile(os.path.join(out, v["path"])) as z:
                if "gamedata/scripts/zgo_bake_inputs.script" in z.namelist():
                    self.assertIn('zgo_mod = "@@UMBRA_ZGO_FOLDER@@"',
                                  z.read("gamedata/scripts/zgo_bake_inputs.script").decode())

    def test_a_zgo_folder_without_its_bake(self):
        os.remove(os.path.join(self.d, "src", "gamma", "mods", "Gore", "zgo_bake_report.json"))
        b = pack_build.Builder(pack_build.load_def(self.toml), os.path.join(self.d, "nobake"), 4096, 2, False)
        pack = b.build()
        self.assertNotIn("bake", pack["layers"][0])
        self.assertTrue(any("every player bakes on their own machine" in w for w in b.warnings), b.warnings)
        with self.assertRaisesRegex(pack_build.DefError, "has no bake"):
            pack_build.Builder(pack_build.load_def(self.toml), os.path.join(self.d, "nobake2"), 4096, 2,
                               False).bake_only()

    def test_deterministic_and_incremental(self):
        a = build(self.toml, os.path.join(self.d, "a"), volume=3000)
        b = build(self.toml, os.path.join(self.d, "b"), volume=3000)
        va = [v["sha256"] for L in a["layers"] for v in L["volumes"]]
        self.assertEqual(va, [v["sha256"] for L in b["layers"] for v in L["volumes"]])
        self.assertGreater(len(a["layers"][2]["volumes"]), 1)      # split by --volume-mb
        # rebuild into the same folder: nothing to pack again
        c = pack_build.Builder(pack_build.load_def(self.toml), os.path.join(self.d, "a"), 3000, 2, False)
        self.assertEqual(va, [v["sha256"] for L in c.build()["layers"] for v in L["volumes"]])


class Install(unittest.TestCase):
    def setUp(self):
        # this machine's own MO2 and Steam may be open; the fake installs don't care
        for target in ("umbra_installer.mo2.running", "umbra_installer.steam.running"):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(install, "GAMMA_BYTES", 0)        # the fake G.A.M.M.A. is tiny
        p.start()
        self.addCleanup(p.stop)

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="umbra_test_")
        cls.toml = make_source(cls.d)
        cls.dist = os.path.join(cls.d, "dist")
        cls.pack = build(cls.toml, cls.dist)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def target(self, name, **kw):
        base = os.path.join(self.d, name)
        return make_target(base, **kw)

    def check_installed(self, t_mo2, t_an, visual_folder="Visual Mod"):
        mods = os.path.join(t_mo2, "mods")
        self.assertTrue(os.path.exists(os.path.join(mods, "Gore", "gamedata", "meshes", "m.ogf")))
        if os.name != "nt":                                     # ZGO Setup's Linux program runs as installed
            self.assertTrue(os.access(os.path.join(mods, "Gore", "tools", "zgo_setup", "zgo-setup-linux"), os.X_OK))
            self.assertFalse(os.access(os.path.join(mods, "Gore", "gamedata", "meshes", "m.ogf"), os.X_OK))
        self.assertTrue(os.path.exists(os.path.join(mods, visual_folder, "gamedata", "textures", "b.dds")))
        st = os.stat(os.path.join(mods, visual_folder, "gamedata", "textures", "b.dds"))
        self.assertEqual(int(st.st_mtime), 1700000100)          # exact times kept
        # relocation: the player's paths, escaped where the format needs it
        with open(os.path.join(mods, "Gore", "zgo_bake_report.json")) as f:
            rep = json.load(f)
        self.assertEqual(rep["profile"], "Umbra")
        self.assertEqual(rep["inputs"]["zgo_mod"], "Gore")
        self.assertEqual(rep["inputs"]["mo2"], t_mo2)
        self.assertEqual(rep["inputs"]["mo2_game"], "Z:" + t_mo2.replace("/", "\\"))
        with open(os.path.join(mods, "Gore", "gamedata", "scripts", "zgo_bake_inputs.script")) as f:
            script = f.read()
        self.assertIn('profile = "Umbra"', script)
        self.assertIn('zgo_mod = "Gore"', script)
        self.assertIn('mo2 = "%s"' % ("Z:" + t_mo2.replace("/", "\\")).replace("\\", "\\\\"), script)
        with open(os.path.join(mods, "Gore", "notes.txt")) as f:
            self.assertEqual(f.read(), "baked from %s/mods/Visual Mod\n" % t_mo2)
        # the engine went into Anomaly, the original backed up
        with open(os.path.join(t_an, "bin", "AnomalyDX11.exe"), "rb") as f:
            self.assertEqual(f.read(), b"coop engine")
        self.assertTrue(os.path.exists(os.path.join(t_an, "bin", "steam_api64.dll")))
        # the profile
        with open(os.path.join(t_mo2, "profiles", "Umbra", "modlist.txt"), encoding="utf-8") as f:
            ml = f.read().splitlines()
        self.assertEqual(ml[1], "+Gore")
        self.assertIn("+" + visual_folder, ml)
        self.assertNotIn("+HD Faces", ml)                       # option not picked
        self.assertTrue(os.path.exists(os.path.join(t_mo2, "profiles", "Umbra", "settings.ini")))
        self.assertEqual(mo2.selected_profile(t_mo2), "Umbra")
        with open(os.path.join(t_mo2, "profiles", "G.A.M.M.A", "modlist.txt"), "rb") as f:
            self.assertEqual(f.read(), b"+GAMMA base\r\n")       # G.A.M.M.A.'s profile untouched
        self.assertTrue(os.path.exists(os.path.join(t_mo2, "UMBRA-CREDITS.txt")))

    def test_add_to_existing_from_folder(self):
        t_mo2, t_an = self.target("existing")
        st, logs = run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False))
        self.check_installed(t_mo2, t_an)
        # run again: nothing re-unpacked, still correct
        st2, logs2 = run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False))
        self.assertTrue(any("Already in place" in m for _, m in logs2))
        self.assertFalse(any(m.startswith("Installed ") for _, m in logs2))
        self.check_installed(t_mo2, t_an)
        # the engine can be undone
        self.assertTrue(layers.restore_anomaly_layer(t_an, "engine"))
        with open(os.path.join(t_an, "bin", "AnomalyDX11.exe"), "rb") as f:
            self.assertEqual(f.read(), b"vanilla")
        self.assertFalse(os.path.exists(os.path.join(t_an, "bin", "steam_api64.dll")))

    def test_over_http_and_options(self):
        srv = Server(self.dist)
        try:
            t_mo2, t_an = self.target("http")
            st, _ = run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=srv.url, steam=False,
                                                choices={"hd"}))
            self.assertTrue(os.path.exists(os.path.join(t_mo2, "mods", "HD Faces", "gamedata", "textures", "face.dds")))
            with open(os.path.join(t_mo2, "profiles", "Umbra", "modlist.txt")) as f:
                self.assertIn("+HD Faces", f.read().splitlines())
            # the player switches 4K off again: kept on disk, switched off
            run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=srv.url, steam=False, choices=set()))
            with open(os.path.join(t_mo2, "profiles", "Umbra", "modlist.txt")) as f:
                self.assertIn("-HD Faces", f.read().splitlines())
        finally:
            srv.close()

    def test_someone_elses_folder_is_kept(self):
        t_mo2, t_an = self.target("conflict", with_conflict=True)
        st, logs = run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False))
        with open(os.path.join(t_mo2, "mods", "Visual Mod", "mine.txt"), "rb") as f:
            self.assertEqual(f.read(), b"the player's own")
        self.assertFalse(os.path.exists(os.path.join(t_mo2, "mods", "Visual Mod", "meta.ini")))
        self.check_installed(t_mo2, t_an, visual_folder="Visual Mod (Umbra)")

    def test_resume_damaged_download(self):
        srv = Server(self.dist)
        try:
            cache = os.path.join(self.d, "resume-cache")
            v = self.pack["layers"][2]["volumes"][0]
            f = fetch.Fetcher(srv.url, cache)
            part = f.path(v) + ".part"
            os.makedirs(os.path.dirname(part), exist_ok=True)
            with open(os.path.join(self.dist, v["path"]), "rb") as src:
                head = src.read(v["size"] // 2)
            with open(part, "wb") as out:
                out.write(head)                         # half a download, as after a lost connection
            f.fetch_all([v])
            self.assertEqual(packdef.sha256_file(f.path(v)), v["sha256"])
            # a damaged part that can't be resumed is fetched again from scratch
            os.unlink(f.path(v))
            os.unlink(f.path(v) + ".ok")
            with open(part, "wb") as out:
                out.write(b"\0" * (v["size"] // 2))
            f.fetch_all([v])
            self.assertEqual(packdef.sha256_file(f.path(v)), v["sha256"])
        finally:
            srv.close()

    def test_new_install_through_the_cli(self):
        cli = os.path.join(self.d, "fake_cli.py")
        write(cli, FAKE_CLI)
        root = os.path.join(self.d, "newinstall", "Umbra")
        os.environ["UMBRA_GAMMA_CLI"] = cli
        try:
            st, logs = run_install(install.Options(mode="new", root=root, source=self.dist, steam=False))
        finally:
            del os.environ["UMBRA_GAMMA_CLI"]
        self.check_installed(os.path.join(root, "gamma"), os.path.join(root, "anomaly"))
        self.assertTrue(os.path.exists(os.path.join(root, ".umbra", "gamma.done")))
        self.assertTrue(os.path.exists(os.path.join(root, ".umbra", "cli-config", "stalker-gamma", "settings.json")))
        self.assertTrue(any(m == "installing" for _, m in logs))
        # the launcher changes this install the way Setup made it
        inst = launcher.Install(os.path.join(root, ".umbra"))
        o = inst.options(choices=set())
        self.assertEqual((o.mode, o.root, o.steam, o.choices), ("new", root, False, set()))
        self.assertEqual(o.gamma_cache, os.path.join(root, "cache"))
        logf = os.listdir(os.path.join(root, ".umbra", "logs"))
        self.assertEqual(len(logf), 1)
        with open(os.path.join(root, ".umbra", "logs", logf[0]), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("installing", text)
        self.assertIn("Done in", text)

    def test_incomplete_gamma_is_caught_and_installed_again(self):
        cli = os.path.join(self.d, "fake_cli2.py")
        write(cli, FAKE_CLI)
        root = os.path.join(self.d, "incomplete", "Umbra")
        opts = dict(mode="new", root=root, source=self.dist, steam=False)
        with mock.patch.dict(os.environ, {"UMBRA_GAMMA_CLI": cli, "FAKE_CLI_NO_ANOMALY": "1"}):
            with self.assertRaises(gammacli.GammaError) as e:
                run_install(install.Options(**opts))
        self.assertIn("Anomaly itself is missing", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(root, ".umbra", "gamma.done")))
        logs = os.path.join(root, ".umbra", "logs")
        with open(os.path.join(logs, os.listdir(logs)[0]), encoding="utf-8") as f:
            self.assertIn("GammaError: stalker-gamma-cli finished, but", f.read())     # the failure is logged
        with mock.patch.dict(os.environ, {"UMBRA_GAMMA_CLI": cli}):
            run_install(install.Options(**opts))                    # the same folder again: it completes
            self.check_installed(os.path.join(root, "gamma"), os.path.join(root, "anomaly"))
            # done once, then Anomaly loses its launcher: the next run installs G.A.M.M.A. again
            os.remove(os.path.join(root, "anomaly", "AnomalyLauncher.exe"))
            _, logs = run_install(install.Options(**opts))
        self.assertTrue(any("incomplete" in m and "AnomalyLauncher.exe" in m for _, m in logs), logs)
        self.assertTrue(os.path.isfile(os.path.join(root, "anomaly", "AnomalyLauncher.exe")))
        self.assertEqual(install.gamma_problems(os.path.join(root, "gamma"), os.path.join(root, "anomaly"),
                                                "G.A.M.M.A"), [])

    def test_gamma_list_typos_are_tolerated_but_not_a_failed_install(self):
        g, an = os.path.join(self.d, "typos", "gamma"), os.path.join(self.d, "typos", "anomaly")
        for n in ("AnomalyLauncher.exe", "fsgame.ltx", "db/files.db0"):
            write(os.path.join(an, n), b"x")
        names = ["mod %d" % i for i in range(200)]
        for n in names:
            os.makedirs(os.path.join(g, "mods", n))
        modlist = os.path.join(g, "profiles", "G.A.M.M.A", "modlist.txt")
        write(modlist, "".join("+%s\r\n" % n for n in names + ["DarkasleQif's typo"]) + "-off and missing\r\n")
        logs = []
        self.assertEqual(install.gamma_problems(g, an, "G.A.M.M.A", lambda m, l="info": logs.append(m)), [])
        self.assertTrue(any("DarkasleQif's typo" in m and "skips it" in m for m in logs), logs)
        for n in names[:6]:                                    # more than a few: a failed install
            shutil.rmtree(os.path.join(g, "mods", n))
        self.assertEqual(len(install.gamma_problems(g, an, "G.A.M.M.A")), 1)
        self.assertIn("7 of the 201 mods", install.gamma_problems(g, an, "G.A.M.M.A")[0])

    def test_menu_and_desktop_shortcuts(self):
        """Setup puts the launcher in the menu and on the desktop; the launcher's own updates leave a
        deleted one deleted, and give an install from before this its shortcuts once."""
        t_mo2, _ = self.target("menus")
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)
        st, logs = run_install(install.Options(**opts))
        work = os.path.join(t_mo2, ".umbra")
        root = os.environ["UMBRA_SHORTCUTS_ROOT"]
        self.assertEqual(len(st["shortcuts"]), 2)
        self.assertTrue(all(p.startswith(root) and os.path.exists(p) for p in st["shortcuts"]))
        self.assertTrue(any("starts from there without Steam" in m for _, m in logs), logs)
        desktop = [p for p in st["shortcuts"] if os.sep + "desktop" + os.sep in p][0]
        if os.name != "nt":
            with open(desktop) as f:
                self.assertIn('Exec="%s"' % os.path.join(work, install.LAUNCHER), f.read())
        os.remove(desktop)                                          # the player doesn't want it
        st2, _ = run_install(install.Options(shortcuts=None, **opts))      # what the launcher's Update runs
        self.assertFalse(os.path.exists(desktop))
        self.assertEqual(st2["shortcuts"], [p for p in st["shortcuts"] if p != desktop])
        # an install from before shortcuts: the launcher's next update adds them
        state_file = os.path.join(work, "state.json")
        with open(state_file) as f:
            old = json.load(f)
        old.pop("shortcuts")
        write(state_file, json.dumps(old).encode())
        st3, _ = run_install(install.Options(shortcuts=None, **opts))
        self.assertEqual(sorted(st3["shortcuts"]), sorted(st["shortcuts"]))
        # --no-shortcuts: none made, the step not even planned
        t2, _ = self.target("no-menus")
        o2 = dict(opts, mo2_dir=t2)
        self.assertNotIn("shortcuts", [s[0] for s in install.Installer(install.Options(shortcuts=False, **o2)).plan().steps])
        self.assertEqual(run_install(install.Options(shortcuts=False, **o2))[0]["shortcuts"], [])
        for p in st3["shortcuts"]:
            os.remove(p)

    def test_launcher_play_repair_and_a_second_plan(self):
        t_mo2, t_an = self.target("launcher")
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)
        first = install.Installer(install.Options(**opts)).plan()
        st, _ = run_install(install.Options(**opts))
        work = os.path.join(t_mo2, ".umbra")
        exe = os.path.join(work, install.LAUNCHER)
        self.assertEqual(st["launcher"], exe)
        self.assertTrue(os.access(exe, os.X_OK))
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120)
        self.assertIn("Umbra Setup", out.stdout, out.stderr)
        inst = launcher.Install(work)
        self.assertTrue(inst.found)
        self.assertEqual((inst.pack["version"], inst.version, inst.profile), (st["pack"], st["pack"], "Umbra"))
        self.assertEqual((inst.mo2, inst.anomaly), (t_mo2, t_an))
        # Play: Steam's Proton command, with MO2's own arguments added
        cmd = ["/s/SteamLinuxRuntime_sniper/_v2-entry-point", "--verb=waitforexitandrun", "--",
               "/s/Proton - Experimental/proton", "waitforexitandrun", os.path.join(t_mo2, "ModOrganizer.exe")]
        self.assertEqual(inst.command(cmd, "Anomaly (DX11)"), cmd + ["-p", "Umbra", "run", "-e", "Anomaly (DX11)"])
        self.assertEqual(inst.command(cmd), cmd + ["-p", "Umbra"])
        self.assertIsNone(inst.command(None))                    # Linux, not started from Steam
        self.assertIsNone(inst.command(["/s/proton", "waitforexitandrun", "/x/other.exe"]))
        # a second plan: nothing to download, and only room for what isn't in place
        again = install.Installer(inst.options()).plan()
        self.assertTrue(again.ok(), again.problems)
        self.assertEqual(again.download_bytes, 0)
        self.assertLess(again.need_bytes, first.need_bytes)
        # repair: a mod folder that lost a file gets it back (a plain re-run leaves it)
        folder = os.path.join(t_mo2, "mods", "Visual Mod")
        lost = next(os.path.join(d, f) for d, _, fs in os.walk(folder) for f in fs
                    if f not in (layers.MARKER, "meta.ini"))
        os.remove(lost)
        run_install(inst.options())
        self.assertFalse(os.path.exists(lost))
        _, logs = run_install(inst.options(repair=True))
        self.assertTrue(os.path.exists(lost))
        self.assertIn(("info", "Installed Visual Mod"), logs)

    def test_an_open_launcher_is_not_replaced(self):
        """A running built program reads its own file, so Setup waits for the launcher to close."""
        t_mo2, _ = self.target("busy-launcher")
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)
        run_install(install.Options(**opts))
        work = os.path.join(t_mo2, ".umbra")
        dst = os.path.join(work, install.LAUNCHER)
        me = os.path.join(t_mo2, "busy-setup")                 # "this program", built
        shutil.copy2(sys.executable, me)
        shutil.copy2(sys.executable, dst)                       # the installed launcher, another build
        with open(me, "ab") as f:
            f.write(b"\0newer")
        old = read_bytes(dst)
        proc = subprocess.Popen([dst, "-I", "-S", "-c", "import time; time.sleep(60)"])
        self.addCleanup(proc.kill)
        logs = []
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", me):
            for _ in range(50):                                 # until it runs
                if install.program_busy(dst):
                    break
                time.sleep(0.1)
            self.assertTrue(install.launcher_busy(work))
            p = install.Installer(install.Options(**opts)).plan()
            self.assertIn("The Umbra launcher is open. Close it first (Setup updates it).", p.problems)
            self.assertEqual(install.install_launcher(work, lambda m, level="info": logs.append(level)), dst)
            self.assertEqual((logs, read_bytes(dst)), (["warn"], old))
            proc.kill()
            proc.wait()
            self.assertFalse(install.launcher_busy(work))
            self.assertTrue(install.Installer(install.Options(**opts)).plan().ok())
            install.install_launcher(work)
            self.assertEqual(read_bytes(dst), read_bytes(me))

    def test_an_engine_put_back_by_gamma_is_replaced_again(self):
        """A G.A.M.M.A. install or update puts its own engine over xrRazom's: Setup (and the
        launcher, before Play) see it and put Umbra's back; removing Umbra restores the newer one."""
        t_mo2, t_an = self.target("regamma")
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)
        run_install(install.Options(**opts))
        exe = os.path.join(t_an, "bin", "AnomalyDX11.exe")
        inst = launcher.Install(os.path.join(t_mo2, ".umbra"))
        self.assertEqual(inst.anomaly_changed(), {})
        write(exe, b"gamma's engine, updated")
        self.assertEqual(inst.anomaly_changed(), {"engine": ["bin/AnomalyDX11.exe"]})
        # the launcher puts it back before Play, from the download cache only
        logs = []
        self.assertEqual(inst.anomaly_restore(log=lambda m, level="info": logs.append((level, m))), ["engine"])
        self.assertEqual(read_bytes(exe), b"coop engine")
        self.assertEqual(inst.anomaly_changed(), {})
        self.assertEqual(inst.anomaly_restore(), [])
        write(exe, b"gamma's engine, updated")
        with self.assertRaises(fetch.Cancelled):                # Stop in the launcher
            inst.anomaly_restore(cancel=lambda: True)
        self.assertEqual(inst.anomaly_restore(), ["engine"])
        # and Setup does it too
        write(exe, b"gamma's engine, updated")
        _, logs = run_install(install.Options(**opts))
        self.assertTrue(any(level == "warn" and "not Umbra's any more" in m for level, m in logs), logs)
        with open(exe, "rb") as f:
            self.assertEqual(f.read(), b"coop engine")
        self.assertEqual(inst.anomaly_changed(), {})
        _, logs = run_install(install.Options(**opts))
        self.assertIn(("info", "Already in place: engine"), logs)
        self.assertTrue(layers.restore_anomaly_layer(t_an, "engine"))
        with open(exe, "rb") as f:
            self.assertEqual(f.read(), b"gamma's engine, updated")     # not the one from before

    def test_the_launcher_sends_you_to_repair_without_the_download(self):
        t_mo2, t_an = self.target("regamma-nocache")
        run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False))
        inst = launcher.Install(os.path.join(t_mo2, ".umbra"))
        write(os.path.join(t_an, "bin", "AnomalyDX11.exe"), b"gamma's engine")
        shutil.rmtree(inst.cache)
        with self.assertRaisesRegex(layers.LayerError, "Repair"):
            inst.anomaly_restore()
        self.assertEqual(read_bytes(os.path.join(t_an, "bin", "AnomalyDX11.exe")), b"gamma's engine")

    def test_a_marker_from_before_hashes_is_checked_by_reinstalling(self):
        t_mo2, t_an = self.target("oldmarker")
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)
        run_install(install.Options(**opts))
        mpath = os.path.join(t_an, ".umbra-backup", "engine", layers.MARKER)
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        for k in ("sha256", "sizes"):
            m.pop(k)
        write(mpath, json.dumps(m).encode())
        write(os.path.join(t_an, "bin", "AnomalyDX11.exe"), b"gamma's engine")
        _, logs = run_install(install.Options(**opts))
        self.assertIn(("info", "Installed engine"), logs)
        self.assertEqual(read_bytes(os.path.join(t_an, "bin", "AnomalyDX11.exe")), b"coop engine")
        self.assertTrue(layers.restore_anomaly_layer(t_an, "engine"))
        self.assertEqual(read_bytes(os.path.join(t_an, "bin", "AnomalyDX11.exe")), b"gamma's engine")

    def test_a_file_umbra_made_that_gamma_then_ships_is_kept_on_removal(self):
        """The first install found no Anomaly yet (the engine's files were Umbra's own); G.A.M.M.A.
        then put its engine there. Removing Umbra must leave G.A.M.M.A.'s, not delete it."""
        t_mo2, t_an = self.target("created")
        os.remove(os.path.join(t_an, "bin", "AnomalyDX11.exe"))
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)
        run_install(install.Options(**opts))
        m = layers.anomaly_marker(t_an, "engine")
        self.assertIn("bin/AnomalyDX11.exe", m["created"])
        write(os.path.join(t_an, "bin", "AnomalyDX11.exe"), b"gamma's engine")
        run_install(install.Options(**opts))
        self.assertEqual(read_bytes(os.path.join(t_an, "bin", "AnomalyDX11.exe")), b"coop engine")
        self.assertTrue(layers.restore_anomaly_layer(t_an, "engine"))
        self.assertEqual(read_bytes(os.path.join(t_an, "bin", "AnomalyDX11.exe")), b"gamma's engine")

    def test_launcher_engines(self):
        t_mo2, t_an = self.target("engines")
        for n in ("AnomalyDX11AVX.exe", "AnomalyDX9.exe"):
            write(os.path.join(t_an, "bin", n), b"x")
        exes = [("Anomaly (DX11-AVX)", "AnomalyDX11AVX.exe"), ("Anomaly (DX11)", "AnomalyDX11.exe"),
                ("Anomaly (DX10)", "AnomalyDX10.exe"), ("Anomaly (DX9)", "AnomalyDX9.exe"),
                ("Explore Virtual Folder", "../gamma/explorer++/Explorer++.exe")]
        mo2.set_executables(t_mo2, [{"title": t, "binary": mo2.mo2_path(os.path.join(t_an, "bin", b), True),
                                     "workingDirectory": "", "arguments": ""} for t, b in exes])
        work = os.path.join(t_mo2, ".umbra")
        os.makedirs(work)
        with open(os.path.join(work, "state.json"), "w") as f:
            json.dump({"mo2": t_mo2, "anomaly": t_an, "profiles": ["Umbra"]}, f)
        inst = launcher.Install(work)
        titles = lambda avx: [t for t, _ in inst.engines(avx)]  # noqa: E731
        self.assertEqual(titles(True), ["Anomaly (DX11-AVX)", "Anomaly (DX11)", "Anomaly (DX9)"])
        self.assertEqual(titles(False), ["Anomaly (DX11)", "Anomaly (DX9)"])     # no AVX: not offered
        self.assertEqual(titles(None), ["Anomaly (DX11)", "Anomaly (DX11-AVX)", "Anomaly (DX9)"])
        eng = inst.engines(True)
        self.assertEqual(inst.engine(eng), "Anomaly (DX11-AVX)")
        inst.save_settings(engine="Anomaly (DX9)")
        self.assertEqual(inst.engine(eng), "Anomaly (DX9)")                       # remembered
        self.assertEqual(inst.engine(inst.engines(False)), "Anomaly (DX9)")
        inst.save_settings(engine="Anomaly (DX10)")
        self.assertEqual(inst.engine(eng), "Anomaly (DX11-AVX)")                  # gone: the best again

    @unittest.skipIf(os.name == "nt", "Linux only")
    def test_linux_needs_steam(self):
        t_mo2, _ = self.target("nosteam")
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist)
        base = os.path.join(t_mo2, "steamroots")
        with mock.patch("umbra_installer.steam.steam_roots", return_value=[]):
            p = install.Installer(install.Options(steam=True, **opts)).plan()
            self.assertTrue(any("Steam isn't installed" in x for x in p.problems), p.problems)
            p = install.Installer(install.Options(steam=False, **opts)).plan()
            self.assertTrue(p.ok(), p.problems)
            self.assertTrue(any("Proton, which comes with Steam" in w for w in p.warnings), p.warnings)
        root = make_steam(base)
        with mock.patch("umbra_installer.steam.steam_roots", return_value=[root]):
            self.assertTrue(install.Installer(install.Options(steam=True, **opts)).plan().ok())
            shutil.rmtree(os.path.join(root, "userdata", "123"))
            p = install.Installer(install.Options(steam=True, **opts)).plan()
            self.assertTrue(any("nobody has signed in" in x for x in p.problems), p.problems)

    def test_flatpak_steam_is_let_in(self):
        home = tempfile.mkdtemp(prefix="umbra_flatpak_")
        self.addCleanup(shutil.rmtree, home, True)
        P = {"work": os.path.join(home, "Games", "Umbra", ".umbra"), "mo2": os.path.join(home, "Games", "Umbra", "gamma"),
             "anomaly": os.path.join(home, "Games", "Umbra", "anomaly")}
        logs = []
        inst = install.Installer(install.Options(mode="new", root=os.path.join(home, "Games", "Umbra"), source=self.dist),
                                 log=lambda m, level="info": logs.append((level, m)))
        done = subprocess.CompletedProcess([], 0, "")
        with mock.patch.object(install.subprocess, "run", return_value=done) as run:
            inst._flatpak_access(os.path.join(home, ".local", "share", "Steam"), P)
            run.assert_not_called()                                 # Steam from the distribution: nothing to do
            inst._flatpak_access(os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share",
                                              "Steam"), P)
            self.assertEqual(run.call_args[0][0], ["flatpak", "override", "--user", "--filesystem=" +
                                                   os.path.realpath(os.path.join(home, "Games", "Umbra")),
                                                   "com.valvesoftware.Steam"])
        with mock.patch.object(install.subprocess, "run", side_effect=FileNotFoundError("flatpak")):
            inst._flatpak_access(os.path.join(home, ".var", "app", "com.valvesoftware.Steam", "data", "Steam"), P)
        self.assertEqual(logs[-1][0], "warn")
        self.assertIn("flatpak override --user", logs[-1][1])

    def test_the_cli_does_without_icu(self):
        cli = gammacli.Cli.__new__(gammacli.Cli)
        cli.work = tempfile.gettempdir()
        with mock.patch.object(gammacli, "has_icu", return_value=False):
            self.assertEqual(cli.env().get("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"),
                             None if os.name == "nt" else "1")
        with mock.patch.object(gammacli, "has_icu", return_value=True), mock.patch.dict(os.environ):
            os.environ.pop("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", None)
            self.assertNotIn("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", cli.env())

    def test_plan_refuses_a_full_folder_and_no_mo2(self):
        busy = os.path.join(self.d, "busy")
        write(os.path.join(busy, "something.txt"), "x")
        p = install.Installer(install.Options(mode="new", root=busy, source=self.dist, steam=False)).plan()
        self.assertTrue(any("not empty" in x for x in p.problems))
        p = install.Installer(install.Options(mode="existing", mo2_dir=busy, source=self.dist, steam=False)).plan()
        self.assertTrue(any("not a Mod Organizer 2 folder" in x for x in p.problems))


class Prebake(unittest.TestCase):
    """Umbra Gore & Effects' body models: the prebaked bake when Umbra Effects Setup says it
    fits the player's mod list, else a bake on the player's machine."""
    def setUp(self):
        for target in ("umbra_installer.mo2.running", "umbra_installer.steam.running"):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(install, "GAMMA_BYTES", 0)
        p.start()
        self.addCleanup(p.stop)
        self.calls = os.path.join(self.d, "calls-%s.txt" % self._testMethodName)
        p = mock.patch.dict(os.environ, {"FAKE_ZGO_CALLS": self.calls})
        p.start()
        self.addCleanup(p.stop)

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="umbra_test_")
        cls.toml = make_source(cls.d)
        cls.dist = os.path.join(cls.d, "dist")
        cls.pack = build(cls.toml, cls.dist)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def install(self, name, source=None, **kw):
        t_mo2, _ = make_target(os.path.join(self.d, name))
        st, logs = run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=source or self.dist,
                                               steam=False, **kw))
        return t_mo2, st, logs

    def asked(self):
        """What Umbra Setup asked Umbra Effects Setup since the last asked()."""
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            out = [x.strip() for x in f]
        os.remove(self.calls)
        return out

    def mesh(self, t_mo2):
        return read_bytes(os.path.join(t_mo2, "mods", "Gore", "gamedata", "meshes", "m.ogf"))

    def test_it_fits_so_it_is_downloaded(self):
        t_new, _ = make_target(os.path.join(self.d, "fits-plan"))
        plan = install.Installer(install.Options(mode="existing", mo2_dir=t_new, source=self.dist, steam=False)).plan()
        self.assertTrue(any("downloads the prebaked ones" in w for w in plan.warnings), plan.warnings)
        nobake = install.Installer(install.Options(mode="existing", mo2_dir=t_new, source=self.dist, steam=False,
                                                   bake=False)).plan()
        ref = self.pack["layers"][0]["bake"]
        self.assertEqual(plan.need_bytes - nobake.need_bytes, ref["download"] + ref["bytes"])
        t_mo2, st, logs = self.install("fits")
        self.assertEqual(self.asked(), ["--check", "--match", "--check"])
        self.assertEqual(st["bake"], {"how": "prebaked", "fingerprint": BAKE_FP})
        ref = self.pack["layers"][0]["bake"]
        self.assertGreater(ref["download"], 0)
        self.assertGreater(ref["bytes"], 0)
        self.assertNotEqual(self.mesh(t_mo2), b"baked here")
        self.assertTrue(any("fit your mod list: downloading" in m for _, m in logs), logs)
        cache = os.path.join(t_mo2, ".umbra", "cache", "umbra-effects")      # only the report stays (a few KB)
        self.assertEqual(len([f for f in os.listdir(os.path.join(cache, BAKE_FP)) if f.endswith(".zip")]), 1)
        # installed again: it is in place, nothing to fetch or bake
        again = install.Installer(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False))
        plan = again.plan()
        self.assertFalse(any("body models" in w for w in plan.warnings), plan.warnings)   # it has them
        st2, _ = run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False))
        self.assertEqual(self.asked(), ["--check"])
        self.assertEqual(st2["bake"]["how"], "up to date")

    def test_another_mod_list_is_baked_here(self):
        with mock.patch.dict(os.environ, {"FAKE_ZGO_MATCH": "no"}):
            t_mo2, st, logs = self.install("other-list")
        self.assertEqual(self.asked(), ["--check", "--match", "--bake"])
        self.assertEqual(st["bake"]["how"], "baked here")
        self.assertEqual(self.mesh(t_mo2), b"baked here")
        said = " ".join(m for _, m in logs)
        self.assertIn("Extra Models", said)                     # why, in Umbra Effects Setup's words
        self.assertNotIn("downloading them", said)
        self.assertIn("Baked 2 models", said)

    def test_an_umbra_effects_setup_that_cannot_compare(self):
        with mock.patch.dict(os.environ, {"FAKE_ZGO_MATCH": "old"}):
            t_mo2, st, logs = self.install("old-zgo")
        self.assertEqual(self.asked(), ["--check", "--match", "--bake"])
        self.assertEqual(st["bake"]["how"], "baked here")
        said = " ".join(m for _, m in logs)
        self.assertIn("could not compare", said)
        self.assertIn("unexpected keyword argument", said)
        self.assertNotIn("don't fit this install", said)

    def test_a_bake_that_fails_is_a_warning_not_a_failed_install(self):
        with mock.patch.dict(os.environ, {"FAKE_ZGO_MATCH": "no", "FAKE_ZGO_BAKE_FAILS": "1"}):
            t_mo2, st, logs = self.install("bake-fails")
        self.assertEqual(st["bake"], {"how": "failed"})
        self.assertTrue(any(lv == "warn" and "not enough free space" in m for lv, m in logs), logs)
        self.assertTrue(os.path.isdir(os.path.join(t_mo2, "profiles", "Umbra")))   # the rest went on

    def test_stale_props_are_cut_again(self):
        with mock.patch.dict(os.environ, {"FAKE_ZGO_PROPS": "stale"}):
            self.install("props")
        self.assertEqual(self.asked(), ["--check", "--match", "--check", "--props"])

    def test_without_the_bake(self):
        t_mo2, _ = make_target(os.path.join(self.d, "nobake"))
        opts = install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False, bake=False)
        self.assertNotIn("bake", [s[0] for s in install.Installer(opts).plan().steps])
        st, _ = run_install(opts)
        self.assertEqual(self.asked(), [])
        self.assertFalse(os.path.exists(os.path.join(t_mo2, "mods", "Gore", "zgo_bake_report.json")))
        self.assertTrue(os.path.exists(os.path.join(t_mo2, "mods", "Gore", "notes.txt")))
        self.assertIsNone(st.get("bake"))

    def test_an_update_of_the_mod_keeps_its_bake(self):
        d = os.path.join(self.d, "update")
        toml = make_source(d)
        dist = os.path.join(d, "dist")
        build(toml, dist)
        with mock.patch.dict(os.environ, {"FAKE_ZGO_MATCH": "no"}):
            t_mo2, _, _ = self.install("update", source=dist)
        self.asked()
        write(os.path.join(d, "src", "gamma", "mods", "Gore", "gamedata", "scripts", "zgo_main.script"),
              'local x = 2\nVERSION = "1.8.1"\n')
        build(toml, dist)
        st, logs = run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=dist, steam=False))
        self.assertTrue(any("Kept the 2 baked files" in m for _, m in logs), logs)
        self.assertEqual(self.asked(), ["--check"])               # still fits: not fetched, not baked
        self.assertEqual(self.mesh(t_mo2), b"baked here")
        gore = os.path.join(t_mo2, "mods", "Gore")
        with open(os.path.join(gore, "gamedata", "scripts", "zgo_main.script")) as f:
            self.assertIn("1.8.1", f.read())

    def test_the_bits(self):
        from umbra_installer import prebake
        self.assertEqual(prebake.parse_result(["x", "RESULT body=ok props=stale kid=off", "RESULT match=no fingerprint=ab",
                                               "done"]), {"match": "no", "fingerprint": "ab"})
        self.assertEqual(prebake.parse_result(["no result"]), {})
        self.assertAlmostEqual(prebake.progress_of("  [models] Baking 120/480 actors\\x"), 0.25)
        self.assertIsNone(prebake.progress_of("Baked 4790/4790 models"))
        self.assertIsNone(prebake.progress_of("  [models] 3/0"))
        self.assertAlmostEqual(prebake.progress_of("  [models] Baking models 2395/4790 actors\\x"), 0.55)
        self.assertAlmostEqual(prebake.progress_of("  [models] Reading your mod list 10/10 "), 0.2)
        old, new, tmp = (os.path.join(self.d, "bits", x) for x in ("old", "new", "tmp"))
        write(os.path.join(old, "zgo_bake_report.json"), json.dumps(
            {"files": ["a/x.ogf", "a/y.ogf", "../escape.txt", "C:/no.txt", "shipped.script"]}))
        write(os.path.join(old, "zgo_fracture_report.json"), json.dumps({"files": ["p/q.ogf"]}))
        for rel in ("a/x.ogf", "a/y.ogf", "p/q.ogf", "shipped.script", "mine.txt"):
            write(os.path.join(old, rel), b"old " + rel.encode())
        write(os.path.join(self.d, "bits", "escape.txt"), b"outside")
        self.assertEqual(prebake.generated(old), {"zgo_bake_report.json", "zgo_fracture_report.json", "a/x.ogf",
                                                  "a/y.ogf", "p/q.ogf", "shipped.script"})
        write(os.path.join(new, "shipped.script"), b"the layer's own")
        self.assertEqual(prebake.carry_generated(old, new, {"shipped.script"}), 5)
        self.assertEqual(read_bytes(os.path.join(new, "shipped.script")), b"the layer's own")
        self.assertEqual(read_bytes(os.path.join(new, "p", "q.ogf")), b"old p/q.ogf")
        self.assertFalse(os.path.exists(os.path.join(new, "mine.txt")))      # not the bake's: the layer's business
        # a new bake in place of the old: the old one's files go, the reports go in last
        write(os.path.join(tmp, "zgo_bake_report.json"), json.dumps({"files": ["a/x.ogf", "b/z.ogf"]}))
        write(os.path.join(tmp, "a", "x.ogf"), b"new x")
        write(os.path.join(tmp, "b", "z.ogf"), b"new z")
        names = ["zgo_bake_report.json", "a/x.ogf", "b/z.ogf"]
        prebake.remove_old(new, names)
        self.assertFalse(os.path.exists(os.path.join(new, "a", "y.ogf")))
        self.assertFalse(os.path.exists(os.path.join(new, "zgo_fracture_report.json")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "bits", "escape.txt")))
        prebake.move_in(tmp, new, names)
        self.assertEqual(read_bytes(os.path.join(new, "a", "x.ogf")), b"new x")
        self.assertEqual(prebake.generated(new), {"zgo_bake_report.json", "a/x.ogf", "b/z.ogf"})


class Safety(unittest.TestCase):
    def test_unsafe_zip_paths(self):
        d = tempfile.mkdtemp(prefix="umbra_test_")
        try:
            for bad in ("../evil.txt", "/abs.txt", "a/../../evil.txt", "C:/x.txt"):
                z = os.path.join(d, "bad.zip")
                with zipfile.ZipFile(z, "w") as zf:
                    zf.writestr(bad, b"x")
                with self.assertRaises(layers.LayerError):
                    layers.unpack([z], os.path.join(d, "out"))
            self.assertFalse(os.path.exists(os.path.join(d, "evil.txt")))
        finally:
            shutil.rmtree(d)

    def test_pack_validation(self):
        with self.assertRaises(packdef.PackError):
            packdef.check_pack({"format": 99})
        with self.assertRaises(packdef.PackError):
            packdef.check_channel({"format": 1, "pack": "../x", "sha256": "0" * 64})


class Steam(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="umbra_test_")
        self.root = os.path.join(self.d, "Steam")
        cfg = os.path.join(self.root, "userdata", "123", "config")
        os.makedirs(cfg)
        other = {"shortcuts": {"0": {"appid": -787916836, "appname": "STALKER GAMMA", "Exe": '"/x/ModOrganizer.exe"',
                                     "StartDir": "/x/", "icon": "", "LaunchOptions": "A=1 %command%", "IsHidden": 0,
                                     "tags": {}}}}
        self.orig = steam.bvdf_dump(other)
        with open(os.path.join(cfg, "shortcuts.vdf"), "wb") as f:
            f.write(self.orig)
        os.makedirs(os.path.join(self.root, "config"))
        with open(os.path.join(self.root, "config", "config.vdf"), "w") as f:
            f.write('"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
                    '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"550"\n\t\t\t\t\t{\n'
                    '\t\t\t\t\t\t"name"\t\t"proton_9"\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')
        self.png = os.path.join(self.d, "logo.png")
        write(self.png, b"\x89PNG fake")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_add_update_remove(self):
        appid = steam.add_shortcut(self.root, "123", "Umbra", "/g/gamma/ModOrganizer.exe", "/g/gamma",
                                   icon="/g/icon.png", compat_tool="GE-Proton11-7", artwork={"logo": self.png})
        cfg = os.path.join(self.root, "userdata", "123", "config")
        with open(os.path.join(cfg, "shortcuts.vdf"), "rb") as f:
            d = steam.bvdf_load(f.read())
        names = [e.get("AppName") or e.get("appname") for e in d["shortcuts"].values()]
        self.assertEqual(names, ["STALKER GAMMA", "Umbra"])
        self.assertEqual(d["shortcuts"]["0"]["LaunchOptions"], "A=1 %command%")   # others kept
        self.assertEqual(d["shortcuts"]["1"]["appid"] & 0xFFFFFFFF, appid)
        self.assertTrue(os.path.exists(os.path.join(cfg, "grid", "%d_logo.png" % appid)))
        self.assertTrue(os.path.exists(os.path.join(cfg, "shortcuts.vdf.umbra-bak")))
        if os.name != "nt":
            with open(os.path.join(self.root, "config", "config.vdf")) as f:
                c = steam.tvdf_load(f.read())
            m = c["InstallConfigStore"]["Software"]["Valve"]["Steam"]["CompatToolMapping"]
            self.assertEqual(m[str(appid)]["name"], "GE-Proton11-7")
            self.assertEqual(m["550"]["name"], "proton_9")
        # again: updated in place, same id
        again = steam.add_shortcut(self.root, "123", "Umbra", "/g/gamma/ModOrganizer.exe", "/g/gamma")
        self.assertEqual(again, appid)
        with open(os.path.join(cfg, "shortcuts.vdf"), "rb") as f:
            self.assertEqual(len(steam.bvdf_load(f.read())["shortcuts"]), 2)
        self.assertTrue(steam.remove_shortcut(self.root, "123", "Umbra"))
        with open(os.path.join(cfg, "shortcuts.vdf"), "rb") as f:
            self.assertEqual(f.read(), self.orig)

    def test_real_files_round_trip(self):
        """This machine's own Steam files, read only: parsing and writing gives the same bytes."""
        roots = _RT["real_steam_roots"]()
        if not roots:
            self.skipTest("no Steam here")
        seen = 0
        for u in steam.users(roots[0]):
            p = os.path.join(roots[0], "userdata", u, "config", "shortcuts.vdf")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    data = f.read()
                self.assertEqual(steam.bvdf_dump(steam.bvdf_load(data)), data)
                seen += 1
        p = os.path.join(roots[0], "config", "config.vdf")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read()
            self.assertEqual(steam.tvdf_load(steam.tvdf_dump(steam.tvdf_load(t))), steam.tvdf_load(t))
        if not seen:
            self.skipTest("no shortcuts.vdf")


class LauncherBits(unittest.TestCase):
    def test_how_long_setup_took(self):
        self.assertEqual([install.took(s) for s in (0.2, 8, 59.4, 59.6, 90, 720)],
                         ["1 second", "8 seconds", "59 seconds", "1 minute", "2 minutes", "12 minutes"])

    def test_split_argv(self):
        self.assertEqual(launcher.split_argv(["--launcher"]), (["--launcher"], None))
        self.assertEqual(launcher.split_argv(["--", "/p/proton", "run", "--", "x"]), ([], ["/p/proton", "run", "--", "x"]))
        self.assertEqual(launcher.split_argv(["--work", "w", "--"]), (["--work", "w"], None))
        self.assertEqual(launcher.mo2_index(["a", "Z:\\g\\ModOrganizer.exe", "b"]), 1)

    def test_child_env(self):
        env = {"LD_LIBRARY_PATH": "/t/_MEIx", "LD_LIBRARY_PATH_ORIG": "/usr/lib/x", "LD_PRELOAD": "overlay.so",
               "SYSTEM_LD_LIBRARY_PATH": "", "SYSTEM_PATH": "/usr/bin", "PATH": "/steam/bin:/usr/bin"}
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", "/t/_MEIx", create=True), mock.patch.dict(os.environ, env):
            e = launcher.child_env()
            self.assertEqual((e["LD_LIBRARY_PATH"], e["LD_PRELOAD"]), ("/usr/lib/x", "overlay.so"))
            self.assertNotIn("LD_LIBRARY_PATH_ORIG", e)
            os.environ["_PYI_ARCHIVE_FILE"] = "/x"
            self.assertNotIn("_PYI_ARCHIVE_FILE", launcher.child_env())
            h = launcher.child_env(host=True)                   # a desktop program: none of Steam's
            self.assertNotIn("LD_PRELOAD", h)
            self.assertNotIn("LD_LIBRARY_PATH", h)
            self.assertEqual(h["PATH"], "/usr/bin")
            del os.environ["LD_LIBRARY_PATH_ORIG"]
            os.environ["LD_LIBRARY_PATH"] = "/t/_MEIx:/opt/lib"
            self.assertEqual(launcher.child_env()["LD_LIBRARY_PATH"], "/opt/lib")
            os.environ.update(APPIMAGE="/d/Umbra-Setup-x86_64.AppImage", APPDIR="/tmp/.mount_x", OWD="/d", ARGV0="u")
            e = launcher.child_env()                            # what Setup's AppImage told it isn't for others
            self.assertFalse(set(launcher.APPIMAGE_VARS) & set(e))

    def test_own_file_in_an_appimage(self):
        import umbra_setup
        d = tempfile.mkdtemp(prefix="umbra_test_")
        os.makedirs(os.path.join(d, "pack", "channels"))
        os.makedirs(os.path.join(d, "pack", "installer"))
        with open(os.path.join(d, "pack", "channels", "stable.json"), "w") as f:
            f.write("{}")
        ai = os.path.join(d, "pack", "installer", "Umbra-Setup-x86_64.AppImage")
        inner = "/tmp/.mount_ab12/usr/bin/umbra-setup-linux"
        with mock.patch.object(sys, "executable", inner), mock.patch.object(umbra_setup, "FROZEN", True), \
                mock.patch.dict(os.environ, {"APPDIR": "/tmp/.mount_ab12", "APPIMAGE": ai}):
            self.assertEqual(umbra_setup.own_file(), ai)
            self.assertEqual(umbra_setup.beside_program(), os.path.join(d, "pack"))
            os.environ["APPDIR"] = "/elsewhere"                 # another AppImage's, inherited: not ours
            self.assertEqual(umbra_setup.own_file(), inner)
            self.assertIsNone(umbra_setup.beside_program())
        with mock.patch.dict(os.environ, {"APPIMAGE": "/a.AppImage", "APPDIR": "/tmp/.mount_ab12"}):
            os.environ.pop("APPDIR")
            with mock.patch.object(sys, "executable", inner):
                self.assertEqual(umbra_setup.own_file(), inner)

    def test_no_install(self):
        inst = launcher.Install(tempfile.mkdtemp(prefix="umbra_test_"))
        self.assertFalse(inst.found)
        self.assertEqual(inst.picks(), set())
        shutil.rmtree(inst.work)


class Mo2Ini(unittest.TestCase):
    def test_selected_profile_keeps_the_rest(self):
        d = tempfile.mkdtemp(prefix="umbra_test_")
        try:
            make_ini(d, "/games/anomaly")
            with open(os.path.join(d, "ModOrganizer.ini"), "rb") as f:
                before = f.read()
            mo2.set_selected_profile(d, "Umbra")
            with open(os.path.join(d, "ModOrganizer.ini"), "rb") as f:
                after = f.read()
            self.assertEqual(after, before.replace(b"@ByteArray(G.A.M.M.A)", b"@ByteArray(Umbra)"))
            self.assertEqual(mo2.native_path("Z:\\\\games\\\\anomaly"), "/games/anomaly")
        finally:
            shutil.rmtree(d)


def volume_names(dist, layer):
    out = {}
    for v in layer["volumes"]:
        with zipfile.ZipFile(os.path.join(dist, v["path"])) as z:
            for n in z.namelist():
                out[n] = z.read(n)
    return out


def layer_by_id(pack, lid):
    return [L for L in pack["layers"] if L["id"] == lid][0]


def make_steam(base):
    root = os.path.join(base, "Steam")
    cfg = os.path.join(root, "userdata", "123", "config")
    os.makedirs(cfg)
    with open(os.path.join(cfg, "shortcuts.vdf"), "wb") as f:
        f.write(steam.bvdf_dump({"shortcuts": {"0": {"appid": -5, "AppName": "Other game", "Exe": '"/x.exe"',
                                                     "tags": {}}}}))
    write(os.path.join(root, "config", "config.vdf"),
          '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n}\n')
    return root


def shortcuts(root):
    with open(os.path.join(root, "userdata", "123", "config", "shortcuts.vdf"), "rb") as f:
        return {e.get("AppName"): e for e in steam.bvdf_load(f.read())["shortcuts"].values()}


PLAYER_LTX = "bind jump kSPACE\r\nbind crouch kLCONTROL\r\nr3_msaa st_opt_2x\r\nvid_mode 2560x1440\r\n"


class Profiles(unittest.TestCase):
    """Settings, user.ltx, two MO2 profiles and the Solo engine, built and installed for real."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="umbra_test_")
        cls.toml = make_profiles_source(cls.d)
        cls.dist = os.path.join(cls.d, "dist")
        cls.pack = build(cls.toml, cls.dist)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def setUp(self):
        for target in ("umbra_installer.mo2.running", "umbra_installer.steam.running"):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

    def target(self, name):
        t_mo2, t_an = make_target(os.path.join(self.d, name))
        realistic_ini(t_mo2, t_an)
        write(os.path.join(t_an, "bin", "AnomalyDX11AVX.exe"), COOP_EXE)
        write(os.path.join(t_an, "appdata", "user.ltx"), PLAYER_LTX)
        return t_mo2, t_an

    def install(self, t_mo2, choices, steam_root=None, source=None):
        p = mock.patch("umbra_installer.steam.steam_roots", return_value=[steam_root] if steam_root else [])
        p.start()
        try:
            return run_install(install.Options(mode="existing", mo2_dir=t_mo2, source=source or self.dist,
                                               steam=bool(steam_root), compat_tool="proton_9", choices=choices))
        finally:
            p.stop()

    # ---------------------------------------------------------------- the pack
    def test_two_profiles_with_their_own_load_orders(self):
        pack = self.pack
        self.assertEqual(pack["format"], packdef.FORMAT)
        self.assertEqual([p["name"] for p in packdef.all_profiles(pack)], ["Umbra", "Umbra Solo"])
        solo = pack["profiles"][0]
        self.assertEqual(solo["option"], "solo")
        self.assertNotEqual(solo["modlist"]["sha256"], pack["profile"]["modlist"]["sha256"])

        def ml(pr):
            with open(os.path.join(self.dist, pr["modlist"]["path"]), encoding="utf-8") as f:
                return f.read().splitlines()
        main, solo_ml = ml(pack["profile"]), ml(solo)
        # every layer is listed in both, in the definition's order; Solo's are on only in Solo
        for line in ("-1 - Bodycam", "-Slop-Ray Engine gamedata", "+Coop Mod", "+Umbra Settings", "+Atmos"):
            self.assertIn(line, main)
        for line in ("+1 - Bodycam", "+Slop-Ray Engine gamedata", "-Coop Mod", "+Umbra Settings", "+Atmos"):
            self.assertIn(line, solo_ml)
        self.assertLess(solo_ml.index("+Umbra Settings"), solo_ml.index("+1 - Bodycam"))   # settings on top
        self.assertEqual(main.count("+Coop Mod") + main.count("-Coop Mod"), 1)            # not listed twice
        self.assertNotIn("+Gore 1.6 (old)", solo_ml)                                     # [profile] drop applies
        for L in pack["layers"]:
            if L.get("profile") == "solo":
                self.assertEqual(L["option"], "solo")
        self.assertEqual(solo["executables"][0]["arguments"], "-ltx user_solo.ltx")
        self.assertEqual(solo["user_ltx"]["file"], "user_solo.ltx")
        self.assertEqual(pack["user_ltx"]["lines"][0], "r3_msaa st_opt_off")
        self.assertEqual(pack["dependencies"][0]["id"], "model-drop")
        self.assertIs(layer_by_id(pack, "settings")["preserve"], True)
        # the MO2 mod zipped inside the engine zip: Windows folder names, a clean meta.ini
        bc = volume_names(self.dist, layer_by_id(pack, "solo-bodycam"))
        self.assertIn("gamedata/scripts/bodycam.script", bc)
        self.assertFalse(any("\\" in n for n in bc))
        self.assertIn(b"version=2.0.1", bc["meta.ini"])
        self.assertNotIn(b"someone", bc["meta.ini"])
        data = volume_names(self.dist, layer_by_id(pack, "solo-data"))
        self.assertEqual(sorted(data), ["db/mods/00_modded_exes_gamedata.db0", "meta.ini"])

    def test_machine_specific_files_are_never_packed(self):
        names = set(volume_names(self.dist, layer_by_id(self.pack, "atmos")))
        self.assertEqual(sorted(names), ["appdata/Atmos_Cold.ltx", "gamedata/shaders/r3/deffer_impl_flat.ps",
                                         "gamedata/textures/logs/keep.dds", "meta.ini"])
        for rel, target in (("appdata/user.ltx", "mods"), ("appdata/user_solo.ltx", "mods"),
                            ("appdata/xrrazom_lan_id.bin", "mods"), ("bin/xrrazom_lan_id.bin", "anomaly"),
                            ("appdata/savedgames/a.scop", "mods"), ("appdata/shaders_cache/r3/x", "mods"),
                            ("appdata/logs/xray_player.log", "mods"), ("appdata/Atmos_Cold.ltx", "anomaly"),
                            ("appdata/console_history.txt", "mods"), ("savedgames/x.scop", "anomaly")):
            self.assertTrue(pack_build.machine_specific(rel, target), rel)
        # whatever the definition says: a layer that lists them by hand gets nothing of it
        d = os.path.join(self.d, "excl")
        toml = make_source(d)
        write(os.path.join(d, "src", "anomaly", "appdata", "user.ltx"), "vid_mode 1x1\r\n")
        write(os.path.join(d, "src", "anomaly", "appdata", "xrrazom_lan_id.bin"), b"id")
        write(os.path.join(d, "src", "anomaly", "appdata", "savedgames", "a.scop"), b"save")
        write(os.path.join(d, "src", "anomaly", "fsgame.ltx"), b"fs")
        with open(toml, "a") as f:
            f.write('\n[[layer]]\nid = "whole-anomaly"\ntarget = "anomaly"\nfrom = "../src/anomaly"\ncredit = "visual"\n'
                    '\n[[layer]]\nid = "by-hand"\nfolder = "By Hand"\ncredit = "visual"\n[layer.files]\n'
                    '"appdata/user.ltx" = "../src/anomaly/appdata/user.ltx"\n'
                    '"xrrazom_lan_id.bin" = "../src/anomaly/appdata/xrrazom_lan_id.bin"\n'
                    '"gamedata/configs/ok.ltx" = "../src/anomaly/fsgame.ltx"\n')
        pack = build(toml, os.path.join(d, "dist"))
        self.assertEqual(sorted(volume_names(os.path.join(d, "dist"), layer_by_id(pack, "whole-anomaly"))),
                         ["bin/AnomalyDX11.exe", "fsgame.ltx"])
        self.assertEqual(sorted(volume_names(os.path.join(d, "dist"), layer_by_id(pack, "by-hand"))),
                         ["gamedata/configs/ok.ltx", "meta.ini"])

    def test_solo_engine_never_touches_coop_files(self):
        coop = {"bin/anomalydx11avx.exe", "bin/anomalydx11.exe", "bin/steam_api64.dll"}
        coop |= {n.lower() for n in volume_names(self.dist, layer_by_id(self.pack, "engine"))}
        solo_files = set()
        for lid in ("solo-engine", "solo-dlss"):
            solo_files |= {n.lower() for n in volume_names(self.dist, layer_by_id(self.pack, lid))}
        self.assertEqual(solo_files, {"bin/anomalydx12.exe", "bin/tbb12.dll", "bin/d3d12/d3d12core.dll",
                                      "bin/nvngx_dlss.dll"})
        self.assertFalse(solo_files & coop)
        self.assertEqual(pack_build.pe_imports(DX12_EXE), {"kernel32.dll", "msvcp140.dll", "msvcp140_2.dll",
                                                           "tbb12.dll"})

        def refused(extra):
            d = tempfile.mkdtemp(prefix="umbra_test_", dir=self.d)
            toml = make_profiles_source(d)
            with open(toml, "a") as f:
                f.write(extra)
            with self.assertRaises(SystemExit) as e:
                pack_build.Builder(pack_build.load_def(toml), os.path.join(d, "dist"), 4096, 1, True).build()
            return str(e.exception)
        layer = ('\n[[layer]]\nid = "bad"\nprofile = "solo"\ntarget = "anomaly"\nsource = "slop"\n'
                 'archive_root = "%s"\narchive_include = ["%s"]\ncredit = "visual"\n%s')
        # the Slop-Ray DX11 exe has the co-op exe's name; the MSVC runtime would load into the co-op exe;
        # OptiScaler's dxgi.dll into every exe
        self.assertIn("replaces a file", refused(layer % ("STEP 1 - Engine", "bin/AnomalyDX11AVX.exe", "")))
        self.assertIn("co-op exes there would load it", refused(layer % ("STEP 1 - Engine", "bin/msvcp140.dll", "")))
        self.assertIn("every program", refused(layer % ("STEP 6 - OptiScaler", "dxgi.dll", "")))
        # unless the definition says so on purpose
        d = tempfile.mkdtemp(prefix="umbra_test_", dir=self.d)
        toml = make_profiles_source(d)
        with open(toml, "a") as f:
            f.write(layer % ("STEP 1 - Engine", "bin/msvcp140.dll", 'coop_shared_dlls = ["msvcp140.dll"]'))
        pack_build.Builder(pack_build.load_def(toml), os.path.join(d, "dist"), 4096, 1, True).build()
        # a source that doesn't match its pinned sha256 is refused
        with open(toml) as f:
            text = f.read()
        with open(toml, "w") as f:
            f.write(re.sub(r'sha256 = "[0-9a-f]{64}"\nversion = "0.6-dev"', 'sha256 = "%s"\nversion = "0.6-dev"'
                           % ("1" * 64), text))
        with self.assertRaises(SystemExit):
            pack_build.Builder(pack_build.load_def(toml), os.path.join(d, "dist"), 4096, 1, True).build()

    # ---------------------------------------------------------------- installing
    def test_install_with_and_without_solo(self):
        t_mo2, t_an = self.target("solo")
        root = make_steam(os.path.join(self.d, "solo-steam"))
        ini_before = mo2.read_executables(t_mo2)
        st, logs = self.install(t_mo2, {"solo"}, steam_root=root)
        # two profiles, each with its own load order
        prof = os.path.join(t_mo2, "profiles")
        with open(os.path.join(prof, "Umbra", "modlist.txt")) as f:
            main = f.read().splitlines()
        with open(os.path.join(prof, "Umbra Solo", "modlist.txt")) as f:
            solo = f.read().splitlines()
        self.assertIn("-1 - Bodycam", main)
        self.assertIn("+Coop Mod", main)
        self.assertIn("+1 - Bodycam", solo)
        self.assertIn("-Coop Mod", solo)
        self.assertEqual(mo2.selected_profile(t_mo2), "Umbra")
        # the engine: new files only; the co-op exe and Anomaly's db untouched
        with open(os.path.join(t_an, "bin", "AnomalyDX11AVX.exe"), "rb") as f:
            self.assertEqual(f.read(), COOP_EXE)
        self.assertTrue(os.path.exists(os.path.join(t_an, "bin", "AnomalyDX12.exe")))
        self.assertTrue(os.path.exists(os.path.join(t_an, "bin", "D3D12", "D3D12Core.dll")))
        self.assertFalse(os.path.exists(os.path.join(t_an, "bin", "msvcp140.dll")))
        self.assertFalse(os.path.exists(os.path.join(t_an, "db")))
        self.assertTrue(os.path.exists(os.path.join(t_mo2, "mods", "Slop-Ray Engine gamedata", "db", "mods",
                                                    "00_modded_exes_gamedata.db0")))
        # user.ltx: only missing keys added; the player's own never changed
        with open(os.path.join(t_an, "appdata", "user.ltx"), newline="") as f:
            ltx = f.read()
        self.assertIn("r3_msaa st_opt_2x\r\n", ltx)
        self.assertNotIn("st_opt_off", ltx)
        self.assertEqual(ltx.count("bind jump"), 1)
        self.assertTrue(ltx.endswith("rs_v_sync off\r\n"))
        self.assertNotIn("r_upscaler", ltx)
        with open(os.path.join(t_an, "appdata", "user.ltx.umbra-bak"), newline="") as f:
            self.assertEqual(f.read(), PLAYER_LTX)
        # Solo's own settings: the player's file plus the engine's settings
        with open(os.path.join(t_an, "appdata", "user_solo.ltx"), newline="") as f:
            solo_ltx = f.read()
        for line in ("vid_mode 2560x1440", "bind crouch kLCONTROL", "r3_msaa st_opt_off", "r_upscaler fsr3",
                     "r_frame_generation fsr", "rs_v_sync off"):
            self.assertIn(line + "\r\n", solo_ltx)
        self.assertNotIn("st_opt_2x", solo_ltx)
        # MO2's Run list: the new entry added, the others as they were
        exes = mo2.read_executables(t_mo2)
        self.assertEqual(exes[:len(ini_before)], ini_before)
        dx12 = exes[-1]
        self.assertEqual(dx12["title"], "Anomaly (DX12 + Frame Gen)")
        self.assertEqual(dx12["binary"], mo2.mo2_path(os.path.join(t_an, "bin", "AnomalyDX12.exe"), True))
        self.assertEqual(dx12["arguments"], "-ltx user_solo.ltx")
        # Steam: a shortcut per profile, each starting its own
        sc = shortcuts(root)
        self.assertEqual(sorted(sc), ["Other game", "Umbra", "Umbra Solo"])
        self.assertEqual(sc["Umbra"]["LaunchOptions"],
                         '"%s" -- %%command%%' % os.path.join(t_mo2, ".umbra", "umbra-launcher"))
        self.assertEqual(sc["Umbra"]["Exe"], '"%s"' % os.path.join(t_mo2, "ModOrganizer.exe"))
        self.assertEqual(sc["Umbra Solo"]["LaunchOptions"], '-p "Umbra Solo" run -e "Anomaly (DX12 + Frame Gen)"')
        self.assertNotEqual(sc["Umbra"]["appid"], sc["Umbra Solo"]["appid"])
        # the player edits Solo's settings; a re-run keeps them
        with open(os.path.join(t_an, "appdata", "user_solo.ltx"), "w", newline="") as f:
            f.write(solo_ltx.replace("r_upscaler fsr3", "r_upscaler dlss"))
        self.install(t_mo2, {"solo"}, steam_root=root)
        with open(os.path.join(t_an, "appdata", "user_solo.ltx")) as f:
            self.assertIn("r_upscaler dlss", f.read())

        # Solo switched off again: its engine, Run entry and shortcut go; co-op stays as it was
        self.install(t_mo2, set(), steam_root=root)
        self.assertFalse(os.path.exists(os.path.join(t_an, "bin", "AnomalyDX12.exe")))
        self.assertFalse(os.path.exists(os.path.join(t_an, "bin", "nvngx_dlss.dll")))
        with open(os.path.join(t_an, "bin", "AnomalyDX11AVX.exe"), "rb") as f:
            self.assertEqual(f.read(), COOP_EXE)
        self.assertEqual(mo2.read_executables(t_mo2), ini_before)
        self.assertEqual(sorted(shortcuts(root)), ["Other game", "Umbra"])
        with open(os.path.join(prof, "Umbra", "modlist.txt")) as f:
            self.assertIn("-1 - Bodycam", f.read().splitlines())

    def test_plan_warns_about_solo_needs(self):
        t_mo2, _ = self.target("plan")
        with mock.patch.object(install, "cpu_has_avx2", return_value=False):
            p = install.Installer(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False,
                                                  choices={"solo"})).plan()
        self.assertTrue(any("AVX2" in w for w in p.warnings))
        self.assertTrue(any("Model Drop 15" in w and "example.invalid/model-drop" in w for w in p.warnings))
        p = install.Installer(install.Options(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False,
                                              choices=set())).plan()
        self.assertFalse(any("Model Drop" in w for w in p.warnings))

    def test_solo_engine_refuses_a_file_it_did_not_add(self):
        t_mo2, t_an = self.target("manual-slopray")
        write(os.path.join(t_an, "bin", "anomalydx12.exe"), b"the player's own copy")      # any case
        with self.assertRaises(layers.LayerError):
            self.install(t_mo2, {"solo"})
        with open(os.path.join(t_an, "bin", "anomalydx12.exe"), "rb") as f:
            self.assertEqual(f.read(), b"the player's own copy")

    def test_settings_survive_an_update(self):
        d = tempfile.mkdtemp(prefix="umbra_test_", dir=self.d)
        toml = make_profiles_source(d)
        build(toml, os.path.join(d, "dist1"))
        edited_t, _ = self.target("pres-edited")
        clean_t, _ = self.target("pres-clean")
        for t in (edited_t, clean_t):
            self.install(t, set(), source=os.path.join(d, "dist1"))
        rel = os.path.join("mods", "Umbra Settings", "gamedata", "configs", "axr_options.ltx")
        marker = layers.read_marker(os.path.join(edited_t, "mods", "Umbra Settings"))
        self.assertEqual(marker["shipped"]["gamedata/configs/axr_options.ltx"],
                         packdef.sha256_bytes(SETTINGS_V1.encode()))
        # the player changes a setting in MCM (MO2 writes it into the mod)
        mine = SETTINGS_V1.replace("blood = 2", "blood = 0")
        write(os.path.join(edited_t, rel), mine)
        write(os.path.join(edited_t, "mods", "Umbra Settings", "gamedata", "configs", "my_own.ltx"), "mine")
        # Umbra 0.0.2 ships new values
        v2 = SETTINGS_V1 + "        new/option = true\r\n"
        write(os.path.join(d, "src", "gamma", "mods", "GAMMA MCM values", "gamedata", "configs", "axr_options.ltx"),
              v2)
        with open(toml) as f:
            text = f.read().replace('version = "0.0.1"', 'version = "0.0.2"')
        with open(toml, "w") as f:
            f.write(text)
        build(toml, os.path.join(d, "dist2"))
        _, logs = self.install(edited_t, set(), source=os.path.join(d, "dist2"))
        with open(os.path.join(edited_t, rel), newline="") as f:
            self.assertEqual(f.read(), mine)                                 # the player's file kept
        with open(os.path.join(edited_t, rel + ".umbra-new"), newline="") as f:
            self.assertEqual(f.read(), v2)                                   # Umbra's new one beside it
        with open(os.path.join(edited_t, "mods", "Umbra Settings", "gamedata", "configs", "my_own.ltx")) as f:
            self.assertEqual(f.read(), "mine")
        self.assertTrue(any("axr_options.ltx.umbra-new" in m and lvl == "warn" for lvl, m in logs))
        # untouched by the player: simply updated
        self.install(clean_t, set(), source=os.path.join(d, "dist2"))
        with open(os.path.join(clean_t, rel), newline="") as f:
            self.assertEqual(f.read(), v2)
        self.assertFalse(os.path.exists(os.path.join(clean_t, rel + ".umbra-new")))
        # once the player takes Umbra's version, later updates apply again
        shutil.copy2(os.path.join(edited_t, rel + ".umbra-new"), os.path.join(edited_t, rel))
        m = layers.read_marker(os.path.join(edited_t, "mods", "Umbra Settings"))
        self.assertEqual(m["shipped"]["gamedata/configs/axr_options.ltx"], packdef.sha256_bytes(v2.encode()))


    def test_same_version_with_changed_mods_is_an_update(self):
        """A pack rebuilt under the same version with a mod changed (a rebake, a hotfix): the
        plan names that mod, so the launcher offers Update for it, not only Repair."""
        d = tempfile.mkdtemp(prefix="umbra_test_", dir=self.d)
        toml = make_profiles_source(d)
        build(toml, os.path.join(d, "dist1"))
        t, _ = self.target("same-version")

        def plan(dist):
            return install.Installer(install.Options(mode="existing", mo2_dir=t, source=dist, steam=False,
                                                     choices=set())).plan()
        self.assertEqual(plan(os.path.join(d, "dist1")).changed, [])         # nothing installed yet
        self.install(t, set(), source=os.path.join(d, "dist1"))
        self.assertEqual(plan(os.path.join(d, "dist1")).changed, [])         # installed, the same
        write(os.path.join(d, "src", "gamma", "mods", "GAMMA MCM values", "gamedata", "configs", "axr_options.ltx"),
              SETTINGS_V1 + "        new/option = true\r\n")
        build(toml, os.path.join(d, "dist2"))                                 # still 0.0.1
        p2 = plan(os.path.join(d, "dist2"))
        self.assertEqual((p2.pack["version"], p2.changed), ("0.0.1", ["Umbra Settings"]))
        self.install(t, set(), source=os.path.join(d, "dist2"))
        self.assertEqual(plan(os.path.join(d, "dist2")).changed, [])
        self.assertEqual([launcher.names(x) for x in (["A"], ["A", "B"], ["A", "B", "C"], list("ABCDE"))],
                         ["A", "A and B", "A, B and C", "A, B, C and 2 more"])

    def test_renamed_mods_are_renamed_in_place(self):
        """A pack that renames mod folders (layer "was"): an install of the old pack gets them
        renamed, in place and in every profile, instead of a second copy. A same-content one
        isn't unpacked again; one whose id changed too (the settings mod becoming the Umbra mod,
        a folder plus single files) is updated in place, the player's MCM edits kept."""
        d = tempfile.mkdtemp(prefix="umbra_test_", dir=self.d)
        toml = make_profiles_source(d)
        pack1 = build(toml, os.path.join(d, "dist1"))
        t, _ = self.target("renamed")
        self.install(t, set(), source=os.path.join(d, "dist1"))
        mods = os.path.join(t, "mods")
        mine = SETTINGS_V1.replace("blood = 2", "blood = 0")                  # the player's MCM edit
        write(os.path.join(mods, "Umbra Settings", "gamedata", "configs", "axr_options.ltx"), mine)
        gamma_ml = os.path.join(t, "profiles", "G.A.M.M.A", "modlist.txt")
        write(gamma_ml, b"+GAMMA base\r\n-Umbra Settings\r\n")              # MO2 added it there, off
        solo_ml = os.path.join(t, "profiles", "Umbra Solo", "modlist.txt")    # an Umbra profile whose
        write(solo_ml, b"+Visual Mod\r\n+Umbra Settings\r\n")              # option is off now
        write(os.path.join(t, "profiles", "Umbra Solo", mo2.PROFILE_MARKER), "")
        umbra_ml = os.path.join(t, "profiles", "Umbra", "modlist.txt")
        with open(umbra_ml, encoding="utf-8") as f:
            before = f.read().splitlines()
        # pack 0.0.2: "Visual Mod" -> "Visual Mod 2.0" (same files); the settings mod -> "Umbra",
        # with a folder of its own (the look) and the settings file on top of it
        write(os.path.join(d, "look", "gamedata", "textures", "umbra", "ui.dds"), b"umbra ui")
        write(os.path.join(d, "look", "gamedata", "configs", "axr_options.ltx"), b"not this one")
        with open(toml, encoding="utf-8") as f:
            text = f.read()
        for old, new in (
                ('version = "0.0.1"', 'version = "0.0.2"'),
                ('id = "visual"\nfolder = "Visual Mod"\n',
                 'id = "visual"\nfolder = "Visual Mod 2.0"\nfrom = "../src/gamma/mods/Visual Mod"\n'
                 'was = ["Visual Mod"]\n'),
                ('id = "settings"\nfolder = "Umbra Settings"\npreserve = true\n',
                 'id = "umbra"\nfolder = "Umbra"\nwas = ["Umbra Settings"]\nfrom = "../look"\n'
                 'preserve = ["gamedata/configs/axr_options.ltx"]\n')):
            self.assertIn(old, text)
            text = text.replace(old, new)
        with open(toml, "w", encoding="utf-8") as f:
            f.write(text)
        pack2 = build(toml, os.path.join(d, "dist2"))
        self.assertEqual(layer_by_id(pack2, "visual")["was"], ["Visual Mod"])
        self.assertEqual(layer_by_id(pack2, "visual")["volumes"], layer_by_id(pack1, "visual")["volumes"])
        names = volume_names(os.path.join(d, "dist2"), layer_by_id(pack2, "umbra"))
        self.assertIn("gamedata/textures/umbra/ui.dds", names)
        self.assertEqual(names["gamedata/configs/axr_options.ltx"], SETTINGS_V1.encode())    # files win
        with open(os.path.join(d, "dist2", *pack2["profile"]["modlist"]["path"].split("/")), encoding="utf-8") as f:
            ml = f.read().splitlines()
        self.assertIn("+Visual Mod 2.0", ml)
        self.assertNotIn("+Visual Mod", ml)                                  # the source's own line is replaced
        # the plan: the Umbra mod changes, the renamed one is in place already
        p = install.Installer(install.Options(mode="existing", mo2_dir=t, source=os.path.join(d, "dist2"),
                                              steam=False, choices=set())).plan()
        self.assertEqual(p.changed, ["Umbra"])
        _, logs = self.install(t, set(), source=os.path.join(d, "dist2"))
        msgs = [m for _, m in logs]
        self.assertIn('Renamed "Visual Mod" to "Visual Mod 2.0"', msgs)
        self.assertIn('Renamed "Umbra Settings" to "Umbra"', msgs)
        self.assertIn("Already in place: Visual Mod 2.0", msgs)
        self.assertIn("Installed Umbra", msgs)
        self.assertFalse(os.path.exists(os.path.join(mods, "Visual Mod")))
        self.assertFalse(os.path.exists(os.path.join(mods, "Umbra Settings")))
        self.assertEqual(layers.read_marker(os.path.join(mods, "Umbra"))["id"], "umbra")
        self.assertTrue(os.path.exists(os.path.join(mods, "Visual Mod 2.0", "gamedata", "textures", "b.dds")))
        with open(os.path.join(mods, "Umbra", "gamedata", "textures", "umbra", "ui.dds"), "rb") as f:
            self.assertEqual(f.read(), b"umbra ui")
        rel = os.path.join(mods, "Umbra", "gamedata", "configs", "axr_options.ltx")
        with open(rel, newline="") as f:
            self.assertEqual(f.read(), mine)                                 # the player's settings kept
        self.assertFalse(os.path.exists(rel + ".umbra-new"))                # Umbra's are unchanged:
        self.assertFalse(any("Kept your changed" in m for m in msgs))        # nothing beside it, no warning
        # Umbra's profiles: the new names, where the old ones were, on or off as they were
        with open(umbra_ml, encoding="utf-8") as f:
            after = f.read().splitlines()
        swap = {"+Umbra Settings": "+Umbra", "+Visual Mod": "+Visual Mod 2.0"}
        self.assertEqual(after, [swap.get(x, x) for x in before])
        with open(solo_ml, "rb") as f:
            self.assertEqual(f.read(), b"+Visual Mod 2.0\r\n+Umbra\r\n")
        with open(gamma_ml, "rb") as f:
            self.assertEqual(f.read(), b"+GAMMA base\r\n-Umbra Settings\r\n")    # G.A.M.M.A.'s: never
        # once more: nothing left to rename or unpack
        _, logs = self.install(t, set(), source=os.path.join(d, "dist2"))
        self.assertFalse(any(m.startswith(("Renamed", "Installed ")) for _, m in logs))

    def test_was_must_not_name_another_layers_folder(self):
        pack = copy.deepcopy(self.pack)
        layer_by_id(pack, "atmos")["was"] = ["Coop Mod"]
        with self.assertRaises(packdef.PackError):
            packdef.check_pack(pack)

class Preserve(unittest.TestCase):
    """The preserve rule is per file pattern and works for Anomaly-folder layers too."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="umbra_test_")

    def tearDown(self):
        shutil.rmtree(self.d)

    def vol(self, name, files):
        p = os.path.join(self.d, name + ".zip")
        with zipfile.ZipFile(p, "w") as z:
            for k, v in files.items():
                z.writestr(k, v)
        return {"sha256": packdef.sha256_file(p), "path": p}

    def test_patterns(self):
        L = {"id": "x", "preserve": ["gamedata/configs/*.ltx", "bin/*.ini"]}
        self.assertTrue(packdef.preserved(L, "gamedata/configs/axr_options.ltx"))
        self.assertTrue(packdef.preserved(L, "BIN/Atmos.ini"))
        self.assertFalse(packdef.preserved(L, "gamedata/scripts/a.script"))
        self.assertTrue(packdef.preserved({"preserve": True}, "anything"))
        self.assertFalse(packdef.preserved({}, "anything"))

    def test_anomaly_layer(self):
        an = os.path.join(self.d, "anomaly")
        write(os.path.join(an, "bin", "reshade.ini"), "original")
        L = {"id": "cfg", "target": "anomaly", "preserve": ["bin/*.ini"]}
        v1 = self.vol("v1", {"bin/reshade.ini": "umbra 1", "bin/tool.dll": "dll 1"})
        L["volumes"] = [v1]
        layers.install_anomaly_layer(an, L, [v1["path"]], {})
        write(os.path.join(an, "bin", "reshade.ini"), "the player's")      # edited after Umbra put it there
        v2 = self.vol("v2", {"bin/reshade.ini": "umbra 2", "bin/tool.dll": "dll 2"})
        L["volumes"] = [v2]
        logs = []
        layers.install_anomaly_layer(an, L, [v2["path"]], {}, log=lambda m, l="info": logs.append(m))
        with open(os.path.join(an, "bin", "reshade.ini")) as f:
            self.assertEqual(f.read(), "the player's")
        with open(os.path.join(an, "bin", "reshade.ini.umbra-new")) as f:
            self.assertEqual(f.read(), "umbra 2")
        with open(os.path.join(an, "bin", "tool.dll")) as f:
            self.assertEqual(f.read(), "dll 2")                            # not preserved: replaced
        self.assertTrue(logs)
        layers.restore_anomaly_layer(an, "cfg")
        with open(os.path.join(an, "bin", "reshade.ini")) as f:
            self.assertEqual(f.read(), "original")


class UserLtx(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="umbra_test_")
        self.p = os.path.join(self.d, "appdata", "user.ltx")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, p=None):
        with open(p or self.p, newline="") as f:
            return f.read()

    def test_keys(self):
        self.assertEqual(userltx.key("r3_msaa st_opt_off"), "r3_msaa")
        self.assertEqual(userltx.key("bind jump kSPACE"), "bind jump")
        self.assertEqual(userltx.key("BIND Jump kX"), "bind jump")
        self.assertIsNone(userltx.key("   "))

    def test_merge_adds_only_missing_keys(self):
        write(self.p, PLAYER_LTX)
        added = userltx.merge(self.p, ["r3_msaa st_opt_off", "bind jump kX", "bind sprint_toggle kLSHIFT",
                                       "rs_v_sync off", "rs_v_sync on"])
        self.assertEqual(added, ["bind sprint_toggle kLSHIFT", "rs_v_sync off"])
        self.assertEqual(self.read(), PLAYER_LTX + "bind sprint_toggle kLSHIFT\r\nrs_v_sync off\r\n")
        self.assertEqual(self.read(self.p + ".umbra-bak"), PLAYER_LTX)
        # again: nothing to do, nothing written
        os.unlink(self.p + ".umbra-bak")
        self.assertEqual(userltx.merge(self.p, ["r3_msaa st_opt_off", "rs_v_sync off"]), [])
        self.assertFalse(os.path.exists(self.p + ".umbra-bak"))

    def test_merge_creates_a_missing_file_and_keeps_line_ends(self):
        self.assertEqual(userltx.merge(self.p, ["r3_msaa st_opt_off"]), ["r3_msaa st_opt_off"])
        self.assertEqual(self.read(), "r3_msaa st_opt_off\r\n")
        lf = os.path.join(self.d, "lf.ltx")
        write(lf, "a 1\nb 2")                                   # LF, no newline at the end
        userltx.merge(lf, ["c 3"])
        self.assertEqual(self.read(lf), "a 1\nb 2\nc 3\n")

    def test_seed_once_then_only_missing_keys(self):
        write(self.p, PLAYER_LTX)
        solo = os.path.join(self.d, "appdata", "user_solo.ltx")
        lines = ["r_upscaler fsr3", "r3_msaa st_opt_off"]
        userltx.seed(solo, self.p, lines)
        self.assertEqual(self.read(solo), PLAYER_LTX.replace("r3_msaa st_opt_2x", "r3_msaa st_opt_off")
                         + "r_upscaler fsr3\r\n")
        self.assertEqual(self.read(), PLAYER_LTX)                                    # the seed is untouched
        write(solo, self.read(solo).replace("st_opt_off", "st_opt_4x"))              # the player's choice
        userltx.seed(solo, self.p, lines + ["r_frame_generation fsr"])
        text = self.read(solo)
        self.assertIn("r3_msaa st_opt_4x\r\n", text)
        self.assertTrue(text.endswith("r_frame_generation fsr\r\n"))
        # no seed file (game never started): just the lines
        other = os.path.join(self.d, "appdata", "other.ltx")
        userltx.seed(other, os.path.join(self.d, "missing.ltx"), lines)
        self.assertEqual(self.read(other), "r_upscaler fsr3\r\nr3_msaa st_opt_off\r\n")


class Mo2Executables(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="umbra_test_")
        self.mo2 = os.path.join(self.d, "gamma")
        self.an = os.path.join(self.d, "anomaly")
        realistic_ini(self.mo2, self.an)
        self.ini = os.path.join(self.mo2, "ModOrganizer.ini")
        with open(self.ini, "rb") as f:
            self.orig = f.read()

    def tearDown(self):
        shutil.rmtree(self.d)

    def sections(self, raw):
        """{section: lines} of an ini."""
        out, cur = {}, None
        for line in raw.split(b"\r\n"):
            if line.startswith(b"[") and line.rstrip().endswith(b"]"):
                cur = line
            out.setdefault(cur, []).append(line)
        return out

    def test_qsettings_escaping(self):
        for s in ("", "plain", '-ltx "a b"', "C:\\x\\y", "a,b", "a;b", " lead", "trail ", "k=v", "tab\there",
                  "Jürgen"):
            self.assertEqual(mo2.qs_unescape(mo2.qs_escape(s)), s, s)
        self.assertEqual(mo2.qs_escape('"Z:\\a"'), '\\"Z:\\\\a\\"')        # as in the real file
        self.assertEqual(mo2.qs_escape("a,b"), '"a,b"')

    def test_read(self):
        exes = mo2.read_executables(self.mo2)
        self.assertEqual([e["title"] for e in exes], ["Anomaly Launcher", "Anomaly (DX11-AVX)",
                                                       "Explore Virtual Folder"])
        self.assertEqual(exes[2]["arguments"], '"%s"' % mo2.mo2_path(self.an, True).replace("/", "\\"))

    def test_add_update_remove_round_trip(self):
        dx12 = {"title": "Anomaly (DX12 + Frame Gen)", "arguments": "-ltx user_solo.ltx",
                "binary": mo2.mo2_path(os.path.join(self.an, "bin", "AnomalyDX12.exe"), True),
                "workingDirectory": mo2.mo2_path(os.path.join(self.an, "bin"), True)}
        self.assertTrue(mo2.set_executables(self.mo2, [dx12]))
        with open(self.ini, "rb") as f:
            after = f.read()
        before_s, after_s = self.sections(self.orig), self.sections(after)
        for sec in before_s:                                       # every other section byte for byte
            if sec != b"[customExecutables]":
                self.assertEqual(after_s[sec], before_s[sec], sec)
        old, new = before_s[b"[customExecutables]"], after_s[b"[customExecutables]"]
        self.assertEqual(new[0], old[0])
        self.assertEqual(new[1], b"size=4")
        self.assertEqual(new[2:2 + 24], old[2:2 + 24])            # the three entries as they were
        self.assertIn(b"4\\arguments=-ltx user_solo.ltx", new)
        self.assertIn(b"4\\title=Anomaly (DX12 + Frame Gen)", new)
        self.assertIn(b"4\\binary=" + dx12["binary"].encode(), new)
        self.assertNotIn(b"\\", dx12["binary"].encode())
        self.assertTrue(after.endswith(b"\r\n"))
        self.assertEqual(mo2.read_executables(self.mo2)[3]["arguments"], "-ltx user_solo.ltx")
        self.assertTrue(os.path.exists(self.ini + ".umbra-bak"))
        # the same again: nothing to write
        self.assertFalse(mo2.set_executables(self.mo2, [dx12]))
        # an update changes only that entry
        dx12b = dict(dx12, arguments='-ltx "user solo.ltx"')
        self.assertTrue(mo2.set_executables(self.mo2, [dx12b]))
        self.assertEqual(mo2.read_executables(self.mo2)[3]["arguments"], '-ltx "user solo.ltx"')
        self.assertEqual(len(mo2.read_executables(self.mo2)), 4)
        # removed: the file is exactly what it was
        self.assertTrue(mo2.set_executables(self.mo2, [], remove=["Anomaly (DX12 + Frame Gen)"]))
        with open(self.ini, "rb") as f:
            self.assertEqual(f.read(), self.orig)

    def test_no_section_yet(self):
        make_ini(self.mo2, self.an)
        mo2.set_executables(self.mo2, [{"title": "X", "binary": "C:/x.exe"}])
        self.assertEqual([e["title"] for e in mo2.read_executables(self.mo2)], ["X"])
        self.assertEqual(mo2.selected_profile(self.mo2), "G.A.M.M.A")
        with open(self.ini, "rb") as f:
            self.assertIn(b"[Settings]\r\nx=1\r\n\r\n[customExecutables]\r\nsize=1\r\n", f.read())

    def test_the_real_file_reads_and_round_trips(self):
        """This machine's own ModOrganizer.ini, read only (MO2 may be open): parsing it and
        writing the same entries back gives the same bytes."""
        real = os.path.join(os.path.expanduser("~"), "gamma", "gamma")
        if not os.path.isfile(os.path.join(real, "ModOrganizer.ini")):
            self.skipTest("no G.A.M.M.A. MO2 here")
        shutil.copy2(os.path.join(real, "ModOrganizer.ini"), os.path.join(self.mo2, "ModOrganizer.ini"))
        with open(self.ini, "rb") as f:
            raw = f.read()
        exes = mo2.read_executables(self.mo2)
        self.assertTrue(exes)
        self.assertFalse(mo2.set_executables(self.mo2, [{k: e.get(k, "") for k in
                                                          ("title", "binary", "arguments", "workingDirectory")}
                                                         for e in exes]))
        with open(self.ini, "rb") as f:
            self.assertEqual(f.read(), raw)


SEASONS_TOML = """
[[option]]
id = "grass-season"
title = "Season"
description = "Which season the grass and trees show."
default = "autumn"
choices = [{ id = "spring", title = "Spring" }, { id = "autumn", title = "Autumn" }, { id = "winter", title = "Winter" }]

[sources.grass]
file = "../src/grass.7z"
sha256 = "%s"
page = "https://example.invalid/grass"
root = "Mod"

[[layer]]
id = "grass-spring"
option = "grass-season=spring"
folder = "Grass Season - Spring"
source = "grass"
archive_root = "Seasons/Spring"
credit = "visual"

[[layer]]
id = "grass-autumn"
option = "grass-season=autumn"
folder = "Grass Season - Autumn"
source = "grass"
archive_root = "Seasons/Autumn"
credit = "visual"

[[layer]]
id = "grass-winter"
option = "grass-season=winter"
folder = "Grass Season - Winter"
source = "grass"
archive_root = "Seasons/Dead"
credit = "visual"

# the installed mod (its autumn files in it), minus every season's files
[[layer]]
id = "grass"
folder = "Grass"
without_files_of = ["grass-autumn"]
credit = "visual"
"""


def make_seasons_source(base):
    """make_source plus a grass mod whose seasons come from a 7z (like C Consciousness)."""
    toml = make_source(base)
    mods = os.path.join(base, "src", "gamma", "mods")
    write(os.path.join(mods, "Grass", "gamedata", "textures", "lfo", "bark.dds"), b"shared bark", 1700000000)
    write(os.path.join(mods, "Grass", "gamedata", "textures", "lfo", "tree.dds"), b"autumn tree", 1700000000)
    write(os.path.join(mods, "Grass", "gamedata", "levels", "l10", "level.details"), b"extra", 1700000000)
    stage = os.path.join(base, "stage")
    for season, files in (("Spring", {"tree.dds": b"spring tree"}), ("Autumn", {"tree.dds": b"autumn tree"}),
                          ("Dead", {"tree.dds": b"dead tree", "bark.dds": b"dead bark"})):
        for n, data in files.items():
            write(os.path.join(stage, "Mod", "Seasons", season, "gamedata", "textures", "lfo", n), data, 1700000000)
    write(os.path.join(stage, "Mod", "Seasons", "Spring", "gamedata", "textures", "ccon", "desktop.ini"), b"junk")
    arc = os.path.join(base, "src", "grass.7z")
    subprocess.run([SEVEN, "a", "-t7z", "-bso0", "-bsp0", arc, "Mod"], cwd=stage, check=True,
                   stdin=subprocess.DEVNULL)
    with open(toml, "a", encoding="utf-8") as f:
        f.write(SEASONS_TOML % packdef.sha256_file(arc))
    return toml


SEVEN = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")


@unittest.skipUnless(SEVEN, "needs 7-Zip")
class Seasons(unittest.TestCase):
    """A pick-one option (C Consciousness's seasons): packed from a 7z, switched by running Setup again."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="umbra_test_")
        cls.unpacked = mock.patch.object(pack_build, "UNPACKED", os.path.join(cls.d, "unpacked"))
        cls.unpacked.start()
        cls.toml = make_seasons_source(cls.d)
        cls.dist = os.path.join(cls.d, "dist")
        cls.pack = build(cls.toml, cls.dist)

    @classmethod
    def tearDownClass(cls):
        cls.unpacked.stop()
        shutil.rmtree(cls.d, ignore_errors=True)

    def setUp(self):
        for target in ("umbra_installer.mo2.running", "umbra_installer.steam.running"):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(install, "GAMMA_BYTES", 0)
        p.start()
        self.addCleanup(p.stop)

    def names(self, lid):
        out = set()
        for v in layer_by_id(self.pack, lid)["volumes"]:
            with zipfile.ZipFile(os.path.join(self.dist, v["path"])) as z:
                out |= set(z.namelist())
        return out

    def test_pack(self):
        pack = packdef.check_pack(self.pack)
        o = [x for x in pack["options"] if x["id"] == "grass-season"][0]
        self.assertEqual(o["default"], "autumn")
        self.assertEqual([c["id"] for c in o["choices"]], ["spring", "autumn", "winter"])
        self.assertEqual(self.names("grass"), {"meta.ini", "gamedata/textures/lfo/bark.dds",
                                               "gamedata/levels/l10/level.details"})   # no season's tree
        self.assertEqual(self.names("grass-spring"), {"meta.ini", "gamedata/textures/lfo/tree.dds"})  # no desktop.ini
        self.assertEqual(self.names("grass-winter"), {"meta.ini", "gamedata/textures/lfo/tree.dds",
                                                      "gamedata/textures/lfo/bark.dds"})
        self.assertEqual(packdef.default_choices(pack), {"grass-season=autumn"})
        # the seasons sit above the shared files in the load order
        with open(os.path.join(self.dist, pack["profile"]["modlist"]["path"]), encoding="utf-8") as f:
            ml = f.read().splitlines()
        order = [l[1:] for l in ml if l[1:].startswith("Grass")]
        self.assertEqual(order, ["Grass Season - Spring", "Grass Season - Autumn", "Grass Season - Winter", "Grass"])
        # built again: the same volumes
        again = build(self.toml, os.path.join(self.d, "dist2"))
        self.assertEqual([v["sha256"] for L in again["layers"] for v in L["volumes"]],
                         [v["sha256"] for L in self.pack["layers"] for v in L["volumes"]])

    def test_choices(self):
        p = self.pack
        self.assertEqual(packdef.normalize_choices(p, None), {"grass-season=autumn"})
        self.assertEqual(packdef.normalize_choices(p, {"hd", "grass-season=winter", "nonsense"}),
                         {"hd", "grass-season=winter"})
        self.assertEqual(packdef.normalize_choices(p, {"grass-season=spring", "grass-season=winter"}),
                         {"grass-season=autumn"})                       # two picked: the default
        self.assertEqual(packdef.picked(p, {"grass-season=spring"}, "grass-season"), "spring")
        ids = [L["id"] for L in packdef.chosen_layers(p, {"grass-season=winter"})]
        self.assertIn("grass-winter", ids)
        self.assertNotIn("grass-autumn", ids)
        bad = json.loads(json.dumps(p))
        bad["layers"][0]["option"] = "grass-season=summer"
        with self.assertRaises(packdef.PackError):
            packdef.check_pack(bad)
        bad = json.loads(json.dumps(p))
        [x for x in bad["options"] if x["id"] == "grass-season"][0]["default"] = "summer"
        with self.assertRaises(packdef.PackError):
            packdef.check_pack(bad)
        # the command line
        sys.path.insert(0, os.path.join(ROOT, "installer"))
        import umbra_setup
        start = {"grass-season=winter"}
        self.assertEqual(umbra_setup.apply_picks(p, start, ["grass-season=spring", "hd"], []),
                         {"grass-season=spring", "hd"})
        for w, wo in ((["grass-season=summer"], []), (["grass-season"], []), (["hd=x"], []),
                      ([], ["grass-season"]), (["nope"], [])):
            with self.assertRaises(ValueError):
                umbra_setup.apply_picks(p, start, w, wo)

    def test_switch_seasons_by_running_setup_again(self):
        t_mo2, t_an = make_target(os.path.join(self.d, "switch"))
        mods = os.path.join(t_mo2, "mods")

        def modlist():
            with open(os.path.join(t_mo2, "profiles", "Umbra", "modlist.txt"), encoding="utf-8") as f:
                return [l for l in f.read().splitlines() if l[1:].startswith("Grass")]

        def read(*p):
            with open(os.path.join(mods, *p), "rb") as f:
                return f.read()
        opts = dict(mode="existing", mo2_dir=t_mo2, source=self.dist, steam=False)
        # first install: the default season
        st, _ = run_install(install.Options(**opts))
        self.assertEqual(st["choices"], ["grass-season=autumn"])
        self.assertEqual(modlist(), ["+Grass Season - Autumn", "+Grass"])
        self.assertEqual(read("Grass Season - Autumn", "gamedata", "textures", "lfo", "tree.dds"), b"autumn tree")
        self.assertFalse(os.path.exists(os.path.join(mods, "Grass", "gamedata", "textures", "lfo", "tree.dds")))
        # winter: its mod goes in and on, autumn's stays on disk, switched off
        st, _ = run_install(install.Options(choices={"grass-season=winter"}, **opts))
        self.assertEqual(modlist(), ["-Grass Season - Autumn", "+Grass Season - Winter", "+Grass"])
        self.assertEqual(read("Grass Season - Winter", "gamedata", "textures", "lfo", "bark.dds"), b"dead bark")
        self.assertTrue(os.path.isdir(os.path.join(mods, "Grass Season - Autumn")))
        # Setup again with nothing picked (an update): it keeps winter
        inst = install.Installer(install.Options(**opts))
        plan = inst.plan()
        self.assertEqual(plan.saved, ["grass-season=winter"])
        self.assertEqual(plan.choices, {"grass-season=winter"})
        st, logs = run_install(install.Options(**opts))
        self.assertEqual(st["choices"], ["grass-season=winter"])
        self.assertEqual(modlist(), ["-Grass Season - Autumn", "+Grass Season - Winter", "+Grass"])
        self.assertFalse(any(m.startswith("Installed ") for _, m in logs))
        # back to autumn: already on disk, so nothing to unpack
        st, logs = run_install(install.Options(choices={"grass-season=autumn"}, **opts))
        self.assertEqual(modlist(), ["+Grass Season - Autumn", "-Grass Season - Winter", "+Grass"])
        self.assertTrue(any("Already in place: Grass Season - Autumn" in m for _, m in logs))


# ---------------------------------------------------------------- the game's runtime (runtime.py, cab.py)
def pe_stub(arch, tag=b""):
    """A DLL-shaped file: MZ, a DOS stub, the PE header at 0x80 with arch's machine type."""
    b = bytearray(0x100)
    b[0:2] = b"MZ"
    struct.pack_into("<I", b, 0x3C, 0x80)
    b[0x40:0x4C] = b"This program"
    b[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", b, 0x84, {"x64": 0x8664, "x86": 0x14C}[arch])
    return bytes(b) + tag


make_cab = cab.build


def make_runtime_downloads(base):
    """Small stand-ins for Microsoft's downloads, shaped like the real ones, as
    runtime.DOWNLOADS entries with file:// URLs."""
    os.makedirs(base, exist_ok=True)
    blobs = {}
    for arch, sfx in (("x64", "_amd64"), ("x86", "_x86")):
        names = runtime.VC_X64 if arch == "x64" else runtime.VC_X86
        inner = make_cab(dict({n + ".dll" + sfx: pe_stub(arch, n.encode()) for n in names},
                              **{n + ".dll_arm64": b"arm64" for n in names[:2]}))
        outer = make_cab({"u0": b"the bundle's engine", "a12": inner}, method=0)
        blobs["vc-" + arch] = b"MZ a vc_redist bundle" + b"\0" * 64 + outer + b"its signature"
    for arch in ("x64", "x86"):
        member = runtime.DOWNLOADS["d3dcompiler-" + arch]["member"]
        blobs["d3dcompiler-" + arch] = make_cab({member: pe_stub(arch, b"d3dcompiler_47"), "fil0ther": b"x" * 9})
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        for arch in ("x64", "x86"):
            for n in ("D3DX9_43", "d3dx10_43", "d3dx11_43", "D3DCompiler_43"):
                zf.writestr("build/native/release/bin/%s/%s.dll" % (arch, n), pe_stub(arch, n.encode()))
    blobs["d3dx"] = z.getvalue()
    out = {}
    for k, b in blobs.items():
        d = dict(runtime.DOWNLOADS[k])
        p = os.path.join(base, d["name"])
        with open(p, "wb") as f:
            f.write(b)
        d.update(url="file:" + urllib.request.pathname2url(p), sha256=hashlib.sha256(b).hexdigest(), size=len(b))
        out[k] = d
    return out


FAKE_PROTON = r"""
import os, sys
# Steam's Proton command, as far as the launcher's prefix setup sees it
pfx = os.path.join(os.environ["STEAM_COMPAT_DATA_PATH"], "pfx")
win = os.path.join(pfx, "drive_c", "windows")
assert sys.argv[1] == "waitforexitandrun", sys.argv
a = sys.argv[2:]
with open(os.path.join(os.environ["STEAM_COMPAT_DATA_PATH"], "runs.txt"), "a") as f:
    f.write(" ".join(a) + "\n")
if not os.path.exists(os.path.join(pfx, "system.reg")):          # Proton makes the prefix
    stub = bytearray(0x100); stub[0:2] = b"MZ"; stub[0x40:0x50] = b"Wine builtin DLL"
    for d in ("system32", "syswow64"):
        os.makedirs(os.path.join(win, d), exist_ok=True)
        for n in ("msvcp140", "d3dx9_43", "d3dcompiler_47"):
            open(os.path.join(win, d, n + ".dll"), "wb").write(bytes(stub))
    open(os.path.join(pfx, "system.reg"), "w").write("WINE REGISTRY Version 2\n")
    open(os.path.join(pfx, "user.reg"), "w").write(
        'WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\DllOverrides] 1\n#time=1\n"atiadlxx"="disabled"\n\n'
        '[Software\\\\Wine\\\\Fonts] 1\n"x"="y"\n')
if a[0].lower().endswith("\\reg.exe") and a[1] == "import":
    text = open(os.path.join(pfx, "drive_c", a[2].split("\\", 1)[1]), "rb").read().decode("utf-16")
    assert "[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]" in text
    lines = "".join(l + "\n" for l in text.splitlines() if l.startswith('"'))
    head = "[Software\\\\Wine\\\\DllOverrides] 1\n#time=1\n"
    reg = open(os.path.join(pfx, "user.reg")).read().replace(head, head + lines)
    open(os.path.join(pfx, "user.reg"), "w").write(reg)
    sys.exit(0)
sys.exit(0 if a[0].lower().endswith("\\cmd.exe") else 1)
"""

_RT = {}


def setUpModule():
    # no test reads this machine's Steam (the tests that need one make a fake Steam and patch this)
    _RT["shortcuts"] = tempfile.mkdtemp(prefix="umbra_menus_")
    _RT["env"] = mock.patch.dict(os.environ, {"UMBRA_SHORTCUTS_ROOT": _RT["shortcuts"]})
    _RT["env"].start()
    _RT["real_steam_roots"] = steam.steam_roots
    _RT["no_steam"] = mock.patch("umbra_installer.steam.steam_roots", return_value=[])
    _RT["no_steam"].start()
    _RT["dir"] = tempfile.mkdtemp(prefix="umbra_rt_")
    _RT["patch"] = mock.patch.dict(runtime.DOWNLOADS, make_runtime_downloads(os.path.join(_RT["dir"], "ms")))
    _RT["patch"].start()


def tearDownModule():
    _RT["patch"].stop()
    _RT["no_steam"].stop()
    _RT["env"].stop()
    shutil.rmtree(_RT["shortcuts"], ignore_errors=True)
    shutil.rmtree(_RT["dir"], ignore_errors=True)


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="umbra_rt_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "cache")

    def test_cabinets(self):
        big = os.urandom(20000) * 5                      # back-references across the 32 KB blocks
        files = {"a.dll": big, "empty": b"", "b.txt": b"hello" * 7000}
        for method in (0, 1):
            data = b"MZ an exe" + b"\0" * 40 + make_cab(files, method) + b"tail"
            offs = cab.find(data)
            self.assertEqual(len(offs), 1)
            c = cab.Cab(data, offs[0])
            self.assertEqual(c.names(), list(files))
            for n, b in files.items():
                self.assertEqual(c.read(n), b)
        lzx = bytearray(make_cab({"x": b"abc"}, method=0))
        struct.pack_into("<H", lzx, 36 + 6, 0x1503)       # LZX:21
        with self.assertRaisesRegex(cab.CabError, "LZX"):
            cab.Cab(bytes(lzx)).read("x")
        nested = make_cab({"outer.txt": b"o", "inner": make_cab({"deep.dll": b"d"})}, method=0)
        self.assertEqual([n for n, _ in cab.walk(nested)], ["outer.txt", "inner", "deep.dll"])

    def test_prepare(self):
        self.assertIsNone(runtime.prepared(self.cache, "prefix"))
        total = sum(runtime.DOWNLOADS[k]["size"] for k in runtime.NEEDS["prefix"])
        self.assertEqual(runtime.download_bytes(self.cache, "prefix"), total)
        seen = []
        m = runtime.prepare(self.cache, "prefix", progress=lambda d, t: seen.append((d, t)))
        want = {"%s/%s.dll" % (a, n) for a, names in runtime.PREFIX.items() for n in names}
        self.assertEqual(set(m["sha256"]), want)            # no arm64, no bundle engine, no stray cabinet files
        self.assertEqual(seen[-1], (total, total))
        self.assertEqual(runtime.download_bytes(self.cache, "prefix"), 0)
        self.assertEqual(os.listdir(os.path.join(runtime.folder(self.cache), "downloads")), [])
        with open(runtime.file_path(self.cache, "prefix", "x86/msvcp140.dll"), "rb") as f:
            b = f.read()
        self.assertEqual((runtime.machine(b), b[-8:]), ("x86", b"msvcp140"))
        with open(runtime.file_path(self.cache, "prefix", "x64/d3dx9_43.dll"), "ab") as f:
            f.write(b"!")                                   # damaged: unpacked again
        self.assertIsNone(runtime.prepared(self.cache, "prefix"))
        w = runtime.prepare(self.cache, "windows")
        self.assertEqual(set(w["sha256"]), {"x64/%s.dll" % n for n in runtime.APP_LOCAL + ["d3dcompiler_47"]})

    def test_a_download_that_is_not_microsofts(self):
        bad = dict(runtime.DOWNLOADS["d3dx"], sha256="0" * 64)
        with mock.patch.dict(runtime.DOWNLOADS, {"d3dx": bad}):
            with self.assertRaisesRegex(runtime.RuntimeProblem, "not the file Umbra expects"):
                runtime.prepare(self.cache, "prefix")
        dl = os.path.join(runtime.folder(self.cache), "downloads")
        self.assertFalse([f for f in os.listdir(dl) if f.endswith(".part") or f.startswith("microsoft.dxsdk")])

    def test_dll_overrides_as_wine_saves_them(self):
        pfx = os.path.join(self.tmp, "pfx")
        write(os.path.join(pfx, "user.reg"),
              b'WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\AppDefaults\\\\x.exe\\\\DllOverrides] 1\n'
              b'"msvcp140"="builtin"\n\n[Software\\\\Wine\\\\DllOverrides] 17\n#time=1dd\n"*D3DX9_43"="native"\n'
              b'"*msvcp140"="native,builtin"\n"atiadlxx"="disabled"\n\n[Software\\\\Wine\\\\Fonts] 1\n"a"="b"\n')
        self.assertEqual(runtime.dll_overrides(pfx),
                         {"d3dx9_43": "native", "msvcp140": "native,builtin", "atiadlxx": "disabled"})

    def test_steams_prefix_through_the_launcher(self):
        """Linux: the first Play makes Steam's prefix and puts Microsoft's DLLs in it, through
        Steam's own Proton command; after that Play goes straight to the game."""
        compat = os.path.join(self.tmp, "compatdata", "1234567890")
        os.makedirs(compat)
        proton = os.path.join(self.tmp, "proton")
        write(proton, FAKE_PROTON.encode())
        work = os.path.join(self.tmp, "Umbra", ".umbra")
        t_mo2 = os.path.join(self.tmp, "Umbra", "gamma")
        os.makedirs(t_mo2)
        write(os.path.join(work, "state.json"), json.dumps({"mo2": t_mo2, "mode": "new"}).encode())
        steam_cmd = [sys.executable, proton, "waitforexitandrun", os.path.join(t_mo2, "ModOrganizer.exe")]
        inst = launcher.Install(work)
        with mock.patch.dict(os.environ, {"STEAM_COMPAT_DATA_PATH": compat}):
            self.assertEqual(inst.runtime_missing(steam_cmd), ["Proton has not made the prefix yet"])
            logs = []
            inst.runtime_fix(steam_cmd, log=lambda m, level="info": logs.append(m))
            self.assertEqual(inst.runtime_missing(steam_cmd), [])
            with open(os.path.join(compat, "runs.txt")) as f:
                self.assertEqual(f.read().splitlines(),
                                 ["C:\\windows\\system32\\cmd.exe /c exit",
                                  "C:\\windows\\system32\\reg.exe import C:\\umbra-runtime.reg"])
            self.assertIn("Put 59 of Microsoft's DLLs into Proton's prefix", logs)
            self.assertFalse(os.path.exists(os.path.join(compat, "pfx", "drive_c", runtime.REG_FILE)))
            self.assertTrue(os.path.isfile(os.path.join(work, "logs", "proton-setup.log")))
            # a Proton update put one of Wine's stand-ins back: only that is redone
            _, dirs = runtime.prefix_paths(compat)
            stub = bytearray(0x100)
            stub[0x40:0x50] = b"Wine builtin DLL"
            write(os.path.join(dirs["x86"], "vcruntime140.dll"), bytes(stub))
            self.assertIn("vcruntime140", inst.runtime_missing(steam_cmd)[0])
            logs.clear()
            inst.runtime_fix(steam_cmd, log=lambda m, level="info": logs.append(m))
            self.assertIn("Put 1 of Microsoft's DLLs into Proton's prefix", logs)
            self.assertEqual(inst.runtime_missing(steam_cmd), [])
            # not from Steam, and no Proton anywhere (no Steam here, nothing remembered)
            self.assertIsNone(inst.command(None))
            with self.assertRaisesRegex(runtime.RuntimeProblem, "can't find Proton"):
                inst.runtime_fix(None)
            self.assertEqual(inst.runtime_missing(None), [])

    def test_a_prefix_winetricks_set_up_is_ready(self):
        compat = os.path.join(self.tmp, "compat")
        pfx, dirs = runtime.prefix_paths(compat)
        write(os.path.join(pfx, "system.reg"), b"WINE REGISTRY Version 2\n")
        for arch, names in runtime.PREFIX.items():
            for n in names:
                write(os.path.join(dirs[arch], n + ".dll"), pe_stub(arch))
        lines = "".join('"*%s"="native"\n' % n for n in runtime.OVERRIDES_NEEDED)   # no MFC, as winetricks
        write(os.path.join(pfx, "user.reg"), ("[Software\\\\Wine\\\\DllOverrides] 1\n" + lines).encode())
        self.assertEqual(runtime.prefix_missing(compat), [])

    def test_windows(self):
        anomaly = os.path.join(self.tmp, "anomaly")
        bin_dir = os.path.join(anomaly, "bin")
        os.makedirs(bin_dir)
        sysdir = os.path.join(self.tmp, "System32")
        os.makedirs(sysdir)
        calls = []

        def elevate(exe, params, cwd):
            calls.append((os.path.basename(exe), params))
            return 3010                                     # done; Windows would like a restart
        old = (14, 38, 33135)
        with mock.patch.object(runtime, "system_dir", return_value=sysdir):
            missing = runtime.windows_missing(anomaly, vc=old)
            self.assertEqual(len(missing), 2)
            self.assertIn("installed: 14.38.33135", missing[0])
            runtime.windows_install(self.cache, anomaly, vc=old, elevate=elevate)
            self.assertEqual(calls, [("vc_redist.x64.exe", "/install /quiet /norestart")])
            self.assertEqual(sorted(os.listdir(bin_dir)),
                             sorted(n + ".dll" for n in runtime.APP_LOCAL + ["d3dcompiler_47"]))
            self.assertEqual(runtime.windows_missing(anomaly, vc=runtime.VC_VERSION), [])
            write(os.path.join(sysdir, "d3dcompiler_47.dll"), b"Windows' own")
            self.assertNotIn("d3dcompiler_47.dll", runtime.app_local(anomaly))
            calls.clear()                                   # new enough: no installer, no question
            runtime.windows_install(self.cache, anomaly, vc=(14, 50, 1), elevate=elevate)
            self.assertEqual(calls, [])

            def no(exe, params, cwd):
                raise PermissionError("declined")
            with self.assertRaisesRegex(runtime.RuntimeProblem, "permission"):
                runtime.windows_install(self.cache, anomaly, vc=old, elevate=no)
            with self.assertRaisesRegex(runtime.RuntimeProblem, "exit code 1603"):
                runtime.windows_install(self.cache, anomaly, vc=old, elevate=lambda *a: 1603)

    WINETRICKS = os.environ.get("UMBRA_TEST_WINETRICKS_CACHE",
                                os.path.expanduser("~/.var/app/com.github.Matoking.protontricks/cache/winetricks"))

    @unittest.skipUnless(os.path.isfile(os.path.join(WINETRICKS, "vcrun2022", "vc_redist.x64.exe")),
                         "needs winetricks' copies of Microsoft's downloads (UMBRA_TEST_WINETRICKS_CACHE)")
    def test_microsofts_real_downloads(self):
        """The pinned files as Microsoft serves them (winetricks downloads the same ones)."""
        files = {"vc-x64": "vcrun2022/vc_redist.x64.exe", "vc-x86": "vcrun2022/vc_redist.x86.exe",
                 "d3dcompiler-x86": "d3dcompiler_47/2630bae9681db6a9f6722366f47d055c.cab",
                 "d3dcompiler-x64": "d3dcompiler_47/61d57a7a82309cd161a854a6f4619e52.cab"}
        for k, rel in files.items():
            with open(os.path.join(self.WINETRICKS, rel), "rb") as f:
                data = f.read()
            self.assertEqual((hashlib.sha256(data).hexdigest(), len(data)),
                             (REAL_DOWNLOADS[k]["sha256"], REAL_DOWNLOADS[k]["size"]), k)
            arch = k.rsplit("-", 1)[1]
            if k.startswith("vc-"):
                dlls = runtime.vc_dlls(data, arch)
                self.assertEqual(sorted(dlls), sorted(n + ".dll" for n in (runtime.VC_X64 if arch == "x64"
                                                                          else runtime.VC_X86)))
                self.assertEqual(runtime.file_version(dlls["msvcp140.dll"])[:3], runtime.VC_VERSION)
            else:
                b = cab.Cab(data).read(REAL_DOWNLOADS[k]["member"])
                self.assertEqual((runtime.machine(b), runtime.file_version(b)[:2]), (arch, (6, 3)))  # Windows 8.1 SDK


FAKE_ENTRY_POINT = """#!/bin/sh
# the Steam Linux Runtime's _v2-entry-point, as far as the launcher sees it: note the call, run what follows --
printf '%s\\n' "$*" >> "$STEAM_COMPAT_DATA_PATH/entry.txt"    # not echo: dash's echo turns backslashes in Z: paths into control characters
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""


def make_fake_steam(base, appid=None, tool="proton_9"):
    """A Steam with a second library holding Valve's Protons and the Steam Linux Runtime, a custom
    Proton in compatibilitytools.d, and the shortcut's Proton in config.vdf. Returns (root, lib2)."""
    root, lib2 = os.path.join(base, "Steam"), os.path.join(base, "SteamLibrary")
    os.makedirs(os.path.join(root, "userdata", "1", "config"))
    os.makedirs(os.path.join(lib2, "steamapps", "common"))
    write(os.path.join(root, "steamapps", "libraryfolders.vdf"), steam.tvdf_dump(
        {"libraryfolders": {"0": {"path": root}, "1": {"path": lib2}}}))
    mapping = {str(appid): {"name": tool, "config": "", "priority": "250"}} if appid else {}
    write(os.path.join(root, "config", "config.vdf"), steam.tvdf_dump(
        {"InstallConfigStore": {"Software": {"Valve": {"Steam": {"CompatToolMapping": mapping}}}}}))

    def proton(folder, runtime_id):
        p = os.path.join(folder, "proton")
        write(p, ("#!%s\n" % sys.executable + FAKE_PROTON).encode())
        os.chmod(p, 0o755)
        write(os.path.join(folder, "toolmanifest.vdf"), steam.tvdf_dump({"manifest": {
            "version": "2", "commandline": "/proton %verb%", "require_tool_appid": runtime_id}}))
    common = os.path.join(lib2, "steamapps", "common")
    proton(os.path.join(common, "Proton 9.0 (Beta)"), "1628350")
    proton(os.path.join(common, "Proton - Experimental"), "4183110")
    proton(os.path.join(common, "Proton 5.13"), "1391110")
    proton(os.path.join(root, "compatibilitytools.d", "GE-Proton11-7"), "4183110")
    write(os.path.join(root, "compatibilitytools.d", "GE-Proton11-7", "compatibilitytool.vdf"), steam.tvdf_dump(
        {"compatibilitytools": {"compat_tools": {"GE-Proton11-7-x86_64": {
            "install_path": ".", "display_name": "GE-Proton11-7"}}}}))
    for rid, name in (("1628350", "SteamLinuxRuntime_sniper"), ("4183110", "SteamLinuxRuntime_4")):
        write(os.path.join(lib2, "steamapps", "appmanifest_%s.acf" % rid), steam.tvdf_dump(
            {"AppState": {"appid": rid, "installdir": name}}))
        ep = os.path.join(common, name, "_v2-entry-point")
        write(ep, FAKE_ENTRY_POINT.encode())
        os.chmod(ep, 0o755)
        write(os.path.join(common, name, "toolmanifest.vdf"), steam.tvdf_dump({"manifest": {
            "version": "2", "commandline": "/_v2-entry-point --verb=%verb% --"}}))
    return root, lib2


@unittest.skipIf(os.name == "nt", "Linux: Proton")
class WithoutSteam(unittest.TestCase):
    """The launcher started from the applications menu: no Steam command, so it runs Proton itself."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="umbra_nosteam_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = os.path.join(self.tmp, "Umbra", ".umbra")
        self.mo2 = os.path.join(self.tmp, "Umbra", "gamma")
        os.makedirs(self.mo2)
        self.exe = os.path.join(self.mo2, "ModOrganizer.exe")

    def install(self, **state):
        st = {"mo2": self.mo2, "anomaly": os.path.join(self.tmp, "Umbra", "anomaly"), "mode": "new",
              "profiles": ["Umbra"]}
        st.update(state)
        write(os.path.join(self.work, "state.json"), json.dumps(st).encode())
        return launcher.Install(self.work)

    def test_tools_by_steams_names(self):
        root, lib2 = make_fake_steam(self.tmp)
        common = os.path.join(lib2, "steamapps", "common")
        self.assertEqual(steam.libraries(root), [root, lib2])
        for name, folder in (("proton_9", os.path.join(common, "Proton 9.0 (Beta)")),
                             ("proton_513", os.path.join(common, "Proton 5.13")),
                             ("proton_experimental", os.path.join(common, "Proton - Experimental")),
                             ("GE-Proton11-7-x86_64", os.path.join(root, "compatibilitytools.d", "GE-Proton11-7")),
                             ("proton_42", os.path.join(common, "Proton - Experimental")),   # gone: the best one
                             (None, os.path.join(common, "Proton - Experimental"))):
            self.assertEqual(steam.find_tool(root, name), folder, name)
        self.assertEqual(steam._version_keys("Proton 9.0 (Beta)"), {"90", "9"})
        self.assertEqual(steam._version_keys("Proton 5.13"), {"513"})
        cmd, folders = steam.tool_command(root, os.path.join(common, "Proton 5.13"))
        self.assertEqual(cmd, [os.path.join(common, "Proton 5.13", "proton"), "waitforexitandrun"])  # no soldier here
        self.assertEqual(folders, [os.path.join(common, "Proton 5.13")])

    def test_made_from_steams_files(self):
        """Never started from Steam: the shortcut's Proton (config.vdf) inside the runtime it asks for,
        in the shortcut's prefix. The first Play sets that prefix up through the same command."""
        root, lib2 = make_fake_steam(self.tmp, appid=2400000001, tool="proton_9")
        common = os.path.join(lib2, "steamapps", "common")
        inst = self.install(steam_appid=2400000001)
        with mock.patch("umbra_installer.steam.steam_roots", return_value=[root]):
            cmd = inst.command(None, "Anomaly (DX11)")
            self.assertEqual(cmd, [os.path.join(common, "SteamLinuxRuntime_sniper", "_v2-entry-point"),
                                   "--verb=waitforexitandrun", "--", os.path.join(common, "Proton 9.0 (Beta)", "proton"),
                                   "waitforexitandrun", self.exe, "-p", "Umbra", "run", "-e", "Anomaly (DX11)"])
            env = inst.env(None)
            compat = os.path.join(root, "steamapps", "compatdata", "2400000001")
            self.assertEqual(env["STEAM_COMPAT_DATA_PATH"], compat)
            self.assertEqual(env["STEAM_COMPAT_CLIENT_INSTALL_PATH"], root)
            self.assertEqual(env["STEAM_COMPAT_INSTALL_PATH"], self.mo2)
            self.assertEqual(env["STEAM_COMPAT_TOOL_PATHS"].split(":"),
                             [os.path.join(common, "Proton 9.0 (Beta)"), os.path.join(common, "SteamLinuxRuntime_sniper")])
            self.assertIn(self.mo2, env["STEAM_COMPAT_MOUNTS"].split(":"))
            self.assertEqual(inst.proton(None).how, "made")
            # the first Play: Proton makes the prefix and gets Microsoft's DLLs, through the runtime
            self.assertEqual(inst.runtime_missing(None), ["Proton has not made the prefix yet"])
            logs = []
            inst.runtime_fix(None, log=lambda m, level="info": logs.append(m))
            self.assertEqual(inst.runtime_missing(None), [])
            self.assertIn("Put 59 of Microsoft's DLLs into Proton's prefix", logs)
            with open(os.path.join(compat, "entry.txt")) as f:
                self.assertTrue(all(ln.startswith("--verb=waitforexitandrun -- ") for ln in f.read().splitlines()))
            # the player switches the shortcut to GE-Proton in Steam: the next start follows
            with open(os.path.join(root, "config", "config.vdf"), encoding="utf-8") as f:
                d = steam.tvdf_load(f.read())
            d["InstallConfigStore"]["Software"]["Valve"]["Steam"]["CompatToolMapping"]["2400000001"]["name"] = \
                "GE-Proton11-7-x86_64"
            write(os.path.join(root, "config", "config.vdf"), steam.tvdf_dump(d))
            again = launcher.Install(self.work)
            self.assertEqual(again.command(None)[3], os.path.join(root, "compatibilitytools.d", "GE-Proton11-7",
                                                                  "proton"))
            self.assertIn("SteamLinuxRuntime_4", again.command(None)[0])

    def test_without_a_steam_shortcut(self):
        """Installed with --no-steam: Umbra's own prefix in its work folder."""
        root, _ = make_fake_steam(self.tmp)
        inst = self.install()
        with mock.patch("umbra_installer.steam.steam_roots", return_value=[root]):
            self.assertEqual(inst.proton(None).compat, os.path.join(self.work, "proton"))
            self.assertIn("Proton - Experimental", inst.command(None)[3])

    def test_what_steam_gave_last_time(self):
        """Started from Steam once: its command (without the Steam client's own wrappers) and Proton's
        settings are kept, and a start from the menu runs exactly that."""
        root, lib2 = make_fake_steam(self.tmp)
        common = os.path.join(lib2, "steamapps", "common")
        compat = os.path.join(self.tmp, "compat")
        entry = os.path.join(common, "SteamLinuxRuntime_4", "_v2-entry-point")
        prot = os.path.join(common, "Proton - Experimental", "proton")
        steam_cmd = [os.path.join(root, "steamrt64", "steam-launch-wrapper"), "--", entry,
                     "--verb=waitforexitandrun", "--", prot, "waitforexitandrun", self.exe]
        inst = self.install(steam_appid=2400000001)
        steam_env = {"STEAM_COMPAT_DATA_PATH": compat, "STEAM_COMPAT_CLIENT_INSTALL_PATH": root,
                     "STEAM_COMPAT_MOUNTS": "/run/media/x", "PRESSURE_VESSEL_FILESYSTEMS_RO": "/y",
                     "SteamUser": "someone", "LD_PRELOAD": "overlay.so"}
        with mock.patch.dict(os.environ, steam_env):
            self.assertEqual(inst.command(steam_cmd), steam_cmd + ["-p", "Umbra"])     # from Steam: Steam's own
            inst.remember_steam(steam_cmd)
        with open(os.path.join(self.work, launcher.STEAM_LAUNCH)) as f:
            rec = json.load(f)
        self.assertEqual(rec["command"], [entry, "--verb=waitforexitandrun", "--", prot, "waitforexitandrun"])
        self.assertEqual(sorted(rec["env"]), ["PRESSURE_VESSEL_FILESYSTEMS_RO", "STEAM_COMPAT_CLIENT_INSTALL_PATH",
                                              "STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_MOUNTS"])   # no account, no overlay
        menu = launcher.Install(self.work)                      # later, from the menu (no Steam here at all)
        self.assertEqual(menu.command(None, "Anomaly (DX11)"),
                         rec["command"] + [self.exe, "-p", "Umbra", "run", "-e", "Anomaly (DX11)"])
        env = menu.env(None)
        self.assertEqual((env["STEAM_COMPAT_DATA_PATH"], env["STEAM_COMPAT_MOUNTS"]), (compat, "/run/media/x"))
        self.assertEqual(menu.proton(None).how, "saved")
        menu.runtime_fix(None)                                  # the prefix setup runs through it too
        self.assertEqual(menu.runtime_missing(None), [])
        # that Proton was removed since: made from Steam's files instead
        shutil.rmtree(os.path.join(common, "Proton - Experimental"))
        with mock.patch("umbra_installer.steam.steam_roots", return_value=[root]):
            gone = launcher.Install(self.work)
            self.assertEqual(gone.proton(None).how, "made")
            self.assertIn("Proton 9.0 (Beta)", gone.command(None)[3])         # the newest of Valve's left

    def test_client_wrappers(self):
        self.assertEqual(launcher.without_client(["/s/ubuntu12_32/reaper", "SteamLaunch", "AppId=1", "--",
                                                  "/s/steam-launch-wrapper", "--", "/p/proton", "run"]),
                         ["/p/proton", "run"])
        self.assertEqual(launcher.without_client(["/p/proton", "run"]), ["/p/proton", "run"])


class Shortcuts(unittest.TestCase):
    """The Start menu / applications menu entry and the desktop shortcut (UMBRA_SHORTCUTS_ROOT here)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="umbra_sc_")
        self.addCleanup(shutil.rmtree, self.root, True)
        p = mock.patch.dict(os.environ, {"UMBRA_SHORTCUTS_ROOT": os.path.join(self.root, "menus")})
        p.start()
        self.addCleanup(p.stop)

    def test_exec_quoting(self):
        """The Exec key quotes the path and escapes it twice, as the Desktop Entry spec reads it."""
        for path in ("/home/a/Games/Umbra/.umbra/umbra-launcher", '/x/my "quoted" $HOME `n`/100% \\b/l'):
            v = menus.desktop_exec(path)
            # the spec's reading: the string's escapes, then %%, then the quoted argument's \" \` \$ \\
            unescaped = re.sub(r"\\(.)", lambda m: {"s": " ", "n": "\n", "t": "\t", "r": "\r"}.get(m.group(1),
                                                                                                m.group(1)), v)
            arg = unescaped.replace("%%", "%")
            self.assertTrue(arg.startswith('"') and arg.endswith('"'), arg)
            self.assertEqual(re.sub(r'\\(["`$\\])', r"\1", arg[1:-1]), path, v)
            self.assertNotRegex(v.replace("%%", ""), "%")

    @unittest.skipIf(os.name == "nt", "Linux: .desktop files")
    def test_linux_entries(self):
        work = os.path.join(self.root, "Games", "Umbra", ".umbra")
        exe = os.path.join(work, "umbra-launcher")
        made = menus.make(work, exe, os.path.join(work, "umbra-icon.png"))
        self.assertEqual(sorted(made), ["desktop", "menu"])
        for path in made.values():
            self.assertTrue(os.access(path, os.X_OK))
            with open(path) as f:
                text = f.read()
            self.assertIn("Name=Umbra\n", text)
            self.assertIn("Exec=\"%s\"\n" % exe, text)
            self.assertIn("StartupWMClass=Umbra\n", text)
            self.assertIn("Categories=Game;\n", text)
            self.assertIn("%s=%s\n" % (menus.MARK, work), text)
        # a second install is told apart by its folder's name
        work2 = os.path.join(self.root, "Other", ".umbra")
        made2 = menus.make(work2, os.path.join(work2, "umbra-launcher"), None)
        self.assertNotEqual(made2["menu"], made["menu"])
        with open(made2["menu"]) as f:
            self.assertIn("Name=Umbra (Other)\n", f.read())
        # a refresh keeps the player's choice: the desktop one they deleted stays deleted
        os.remove(made["desktop"])
        again = menus.make(work, exe, None, keep_only=[made["menu"], made["desktop"]])
        self.assertEqual(list(again), ["menu"])
        self.assertFalse(os.path.exists(made["desktop"]))
        self.assertEqual(menus.remove(list(again.values())), [made["menu"]])

    def test_xdg_desktop_dir(self):
        home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(home, "Schreibtisch"))
        write(os.path.join(home, ".config", "user-dirs.dirs"), b'XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n')
        with mock.patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}):
            self.assertEqual(menus.xdg_user_dir("DESKTOP", "Desktop"), os.path.join(home, "Schreibtisch"))
            write(os.path.join(home, ".config", "user-dirs.dirs"), b'XDG_DESKTOP_DIR="$HOME/"\n')
            self.assertIsNone(menus.xdg_user_dir("DESKTOP", "Desktop"))        # the desktop is home: none



class Typefaces(unittest.TestCase):
    """Setup's own TrueType renderer (ttf.py) and the font files it ships. typeface.py, the Tk
    side, needs tkinter: the program's self-test (umbra-setup --selftest) runs it."""
    FONTS = os.path.join(ROOT, "installer", "umbra_installer", "assets", "fonts")
    FILES = ("TekturNarrow-SemiBold.ttf", "Play-Regular.ttf", "Play-Bold.ttf")

    def font(self, fn="Play-Regular.ttf"):
        return ttf.Font(os.path.join(self.FONTS, fn))

    @staticmethod
    def ink(cov):
        return sum(cov)

    def test_the_fonts_ship_with_their_licences(self):
        have = set(os.listdir(self.FONTS))
        for fn in self.FILES + ("OFL-Tektur.txt", "OFL-Play.txt"):
            self.assertIn(fn, have)
        for fn in ("OFL-Tektur.txt", "OFL-Play.txt"):
            with open(os.path.join(self.FONTS, fn), encoding="utf-8") as f:
                self.assertIn("SIL OPEN FONT LICENSE", f.read().upper())
        # the typeface module's file table names exactly these (read as text: it imports tkinter)
        with open(os.path.join(ROOT, "installer", "umbra_installer", "typeface.py"), encoding="utf-8") as f:
            named = set(re.findall(r'"([\w-]+\.ttf)"', f.read()))
        self.assertEqual(named, set(self.FILES))

    def test_every_font_has_the_letters_umbra_writes(self):
        for fn in self.FILES:
            f = self.font(fn)
            for ch in "AZaz09.,:()/+-" + "ҐґЄєІіЇї" + "₴" + "ÄÉÑ":
                self.assertTrue(f.glyph_id(ch), "%s has no %r" % (fn, ch))
            self.assertEqual(f.glyph_id("\uE000"), 0)          # a private-use character: .notdef

    def test_text_is_drawn(self):
        f = self.font()
        w, h, cov = ttf.render(f, "Umbra", 20)
        self.assertEqual(len(cov), w * h)
        self.assertEqual(h, f.line_height(20))
        self.assertGreater(self.ink(cov), 255 * 20)
        self.assertEqual(max(cov), 255)                           # solid stems
        # nothing above the ascent or below the descent rows of an x-height word
        w2, h2, cov2 = ttf.render(f, "un", 20)
        top = f.baseline(20) - 14
        self.assertEqual(sum(cov2[:top * w2]), 0)
        # bigger is wider and inkier; empty draws nothing
        w3, h3, cov3 = ttf.render(f, "Umbra", 40)
        self.assertGreater(w3, 1.8 * w)
        self.assertGreater(self.ink(cov3), 3 * self.ink(cov))
        self.assertEqual(self.ink(ttf.render(f, "", 20)[2]), 0)
        self.assertEqual(self.ink(ttf.render(f, "   ", 20)[2]), 0)

    def test_composite_glyphs_are_drawn_whole(self):
        f = self.font("TekturNarrow-SemiBold.ttf")
        a, a_uml = f.outline(f.glyph_id("A")), f.outline(f.glyph_id("Ä"))
        self.assertGreater(len(a_uml), len(a))                   # the A and the dots
        ink_a = self.ink(ttf.render(f, "A", 30)[2])
        ink_u = self.ink(ttf.render(f, "Ä", 30)[2])
        self.assertGreater(ink_u, ink_a * 1.05)
        self.assertLess(ink_u, ink_a * 1.6)

    def test_wrapping(self):
        f = self.font()
        text = "G.A.M.M.A. itself is downloaded from its sources by stalker-gamma-cli"
        one = ttf.wrap(f, text, 14, None)
        self.assertEqual(one, [text])
        lines = ttf.wrap(f, text, 14, 200)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), text)
        for line in lines:
            self.assertLessEqual(f.width(line, 14), 200)
        self.assertEqual(ttf.wrap(f, "a\nb", 14, 200), ["a", "b"])
        long = "x" * 80                                           # a word wider than the line: alone
        self.assertEqual(ttf.wrap(f, "a " + long + " b", 14, 100), ["a", long, "b"])
        w, h, _ = ttf.render(f, text, 14, width=200)
        self.assertLessEqual(w, 202)
        self.assertEqual(h, len(lines) * f.line_height(14))

    def test_centred_and_right_aligned_lines(self):
        f = self.font()
        for justify in ("left", "center", "right"):
            w, h, cov = ttf.render(f, "wide line here\ni", 16, justify=justify)
            row = f.line_height(16)
            cols = [x for x in range(w) for y in range(row, h) if cov[y * w + x]]
            mid = (min(cols) + max(cols)) / 2.0
            want = {"left": w * 0.1, "center": w / 2.0, "right": w * 0.9}[justify]
            self.assertLess(abs(mid - want), w * 0.15, justify)

    def test_not_a_font(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.ttf")
            with open(p, "wb") as f:
                f.write(b"\0\1\0\0" + b"\0" * 40)
            with self.assertRaises(ttf.FontError):
                ttf.Font(p)


if __name__ == "__main__":
    unittest.main()
