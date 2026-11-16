#!/usr/bin/env python3
"""
Put Umbra's own mod files from this repo into the tested MO2 instance.

    python3 dev/deploy_mods.py            copy them into the instance ([source] in pack/umbra.toml)
    python3 dev/deploy_mods.py --check    only say which differ (exit 1 if any do)

A layer with own_files = "../mods/<folder>" keeps Umbra's own files of that mod in this
repo (mods/, see mods/README.md). The pack is built from the instance's copy, which is the
one that was tested, and dev/pack_build.py refuses to pack it while the two differ. Edit
them here, deploy, test in the instance, build.

Files are copied over, never deleted: the instance's folder also holds files that aren't
kept here (merged or generated ones, meta.ini). MO2 must be closed, since it rewrites its
mod list when it exits.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "installer"))
import pack_build  # noqa: E402
from umbra_installer import mo2 as inst_mo2  # noqa: E402


def own_layers(d):
    """[(layer, repo folder, instance folder)] for the layers with own_files."""
    mo2 = pack_build.resolve(d["_base"], d["source"]["mo2"])
    return [(L, pack_build.resolve(d["_base"], L["own_files"]), os.path.join(mo2, "mods", L["folder"]))
            for L in d["layer"] if L.get("own_files")]


def deploy(d, check=False, log=print):
    """Copy (or with check, only compare) every own_files layer. Returns the files that differed."""
    differ = []
    for L, own, folder in own_layers(d):
        diff = pack_build.own_files_diff(own, folder)
        differ += [(L["folder"], rel) for rel in diff]
        for rel in diff:
            log("%s %s: %s" % ("differs" if check else "copy", L["folder"], rel))
            if not check:
                dst = os.path.join(folder, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(own, *rel.split("/")), dst + ".tmp")
                os.replace(dst + ".tmp", dst)
        if not diff:
            log("same %s" % L["folder"])
    return differ


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--def", dest="defn", default=os.path.join(ROOT, "pack", "umbra.toml"))
    ap.add_argument("--check", action="store_true", help="only compare; exit 1 if anything differs")
    a = ap.parse_args()
    d = pack_build.load_def(a.defn)
    if not a.check and inst_mo2.running():
        print("Mod Organizer 2 is open; close it first (it rewrites its mod list when it exits).")
        return 2
    differ = deploy(d, check=a.check)
    return 1 if a.check and differ else 0


if __name__ == "__main__":
    sys.exit(main())
