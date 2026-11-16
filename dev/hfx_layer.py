#!/usr/bin/env python3
"""
HollywoodFX's firefight effects as an MO2 mod in the tested instance: "HollywoodFX Impacts - Umbra".

    python3 -I dev/hfx_layer.py --archive HFX_v3.1.5.zip [--preset realistic|hollywood|minimal] [--dry-run]

HollywoodFX 3.1 (Deathorchid) ships one compiled particles.xr. In this mod list the loose
particle files of "Particles Cinematic VFX 3.5 1.1.2 BOTZ YAWM" beat any particles.xr, and
its particles.xr is the winning one, so installing HFX as it comes would either change little
or replace that mod's whole library. This takes from HFX only what it changes in firefights
and writes it as loose files, which beat the base's:

- entries under hit_fx\\, explosions\\, weapons\\, ghost_particles\\weapons\\ and amik\\weapons\\
  whose HFX version plays differently from what the instance plays now (its loose file, else
  the winning particles.xr);
- none that kid mode's classifier (Umbra Gore & Effects' kidfx_pe) calls gory, so blood stays
  Umbra Gore & Effects', and none that Umbra Gore & Effects ships;
- every effect and group those play that the instance lacks or has differently (a group that
  names a missing effect breaks the game); a group that would need a new gory one is left out;
- the textures they draw that the instance lacks, from the archive. Where both have a texture
  and they differ, the instance's stays: other effects draw it.

HFX's configs are not used: they move weapons' fire points and set frags = 0 on grenades.
Its presets differ only in particles.xr; Realistic is HFX's core file.

Reads the game, MO2 and the archive; writes only the mod's own folder, which it rebuilds
(MO2's meta.ini in it is kept). It never touches modlist.txt. Uses Umbra Gore & Effects'
tools (tools/zgo_kidfx: particles_xr, sc_tree, kidfx_pe) from the instance's copy of that
mod, or from --zgo.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "installer"))
import pack_build  # noqa: E402
from umbra_installer import mo2 as inst_mo2  # noqa: E402

FOLDER = "HollywoodFX Impacts - Umbra"
MANIFEST = "umbra-hfx-layer.json"
SCOPE = ("hit_fx\\", "explosions\\", "weapons\\", "ghost_particles\\weapons\\", "amik\\weapons\\")
PRESETS = {"realistic": "RealisticFX", "hollywood": "HollywoodFX", "minimal": "MinimalFX"}
KNOWN = {"c68a68367a2545bea0881a7dbb58bc5a5e3e99616926acb76bba59572f99f72d": "HFX_v3.1.5.zip"}
CORE_RE = re.compile(r"^00 \[CORE\][^/]*/gamedata/(textures/.+)$")


def tools(path):
    for name in ("particles_xr", "sc_tree", "kidfx_pe"):
        if not os.path.isfile(os.path.join(path, name + ".py")):
            raise SystemExit("no %s.py in %s (deploy Umbra Gore & Effects with its dev/deploy.py, or give --zgo "
                             "<Umbra Gore & Effects>/tools/zgo_kidfx)" % (name, path))
    sys.path.insert(0, path)
    import particles_xr
    import sc_tree
    import kidfx_pe
    return particles_xr, sc_tree, kidfx_pe


def read_archive(path, preset):
    """(sha256, particles.xr bytes, {textures/...: zip name})."""
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    z = zipfile.ZipFile(path)
    want = "02 [%s-OPTION]/gamedata/particles.xr" % PRESETS[preset]
    names = z.namelist()
    if want not in names:
        raise SystemExit("%s has no %s: not HollywoodFX 3.1's archive?" % (path, want))
    tex = {}
    for n in names:
        m = CORE_RE.match(n)
        if m and not n.endswith("/"):
            rel = m.group(1).lower()
            if ".." in rel.split("/") or rel.startswith("/"):
                raise SystemExit("unsafe path in the archive: %r" % n)
            tex[rel] = n
    return sha, z.read(want), tex, z


class Base:
    """What the instance plays now, without this mod: a loose file, else the winning particles.xr."""

    def __init__(self, px, sc_tree, paths):
        self.px = px
        res = sc_tree.Resolved(paths, ("particles/", "particles.xr", "textures/"), zgo_on_top=False)
        own = "mod:" + FOLDER
        res.chain = {k: [x for x in v if x[0].label != own] for k, v in res.chain.items()}
        res.chain = {k: v for k, v in res.chain.items() if v}
        self.res = res
        w = res.winner("particles.xr")
        self.xr_label = (w[0].label + " particles.xr") if w else None
        self.xr = px.loads(w[0].read(w[1])) if w else px.Library()

    def entry(self, kind, name):
        """(provider label, Effect|Group) or (None, None)."""
        rel = "particles/" + name.lower().replace("\\", "/") + "." + kind
        w = self.res.game_winner(rel)
        if w:
            return w[0].label, self.px.parse(kind, w[0].read(w[1]), name)
        hit = self.xr.find(name)
        if hit and hit[0] == kind:
            return self.xr_label, hit[1]
        return None, None

    def has(self, name):
        n = name.lower().replace("\\", "/")
        return any(self.res.game_winner("particles/%s.%s" % (n, k)) for k in ("pe", "pg")) or bool(self.xr.find(name))

    def texture(self, rel):
        return self.res.game_winner(rel)


def select(px, kidfx_pe, hfx, base, zgo_label, hfx_tex):
    """(rows by (kind, name lower), [selected keys], {root: why left out})."""
    rows = {}
    for kind, store in (("pe", hfx.effects), ("pg", hfx.groups)):
        for n, e in store.items():
            src, b = base.entry(kind, n)
            st = "new" if b is None else ("same" if px.same(e, b, tol=2e-6) else "differs")
            rel = n.lower().replace("\\", "/") + "." + kind
            if kind == "pe":
                color = next((a.vectors[0] for a in e.actions if a.type == 23), None)
                cls, why = kidfx_pe.classify(rel, e.shader, e.texture, color)
            else:
                cls, why = kidfx_pe.classify(rel, None, None)
            rows[(kind, n.lower())] = dict(kind=kind, name=n, status=st, base=src, cls=cls, why=why)

    def row(name):
        n = name.lower()
        return rows.get(("pe", n)) or rows.get(("pg", n))

    def tex_ok(e):
        miss = []
        for t in px.texture_names(e):
            rel = "textures/" + t.replace("\\", "/") + ".dds"
            if rel not in hfx_tex and not base.texture(rel):
                miss.append(t)
        return miss

    roots = sorted(k for k, r in rows.items() if r["name"].lower().startswith(SCOPE) and r["status"] == "differs"
                   and r["cls"] != "gore" and r["base"] != zgo_label)
    left_out = {}
    while True:
        want, bad = set(), {}
        for root in roots:
            need, stack, problem = {root}, [root], None
            while stack and not problem:
                k = stack.pop()
                if k[0] == "pe":
                    miss = tex_ok(hfx.effects[rows[k]["name"]])
                    if miss:
                        problem = "%s draws %s, which neither HFX nor the instance has" % (rows[k]["name"], miss)
                    continue
                for ch in px.children(hfx.groups[rows[k]["name"]]):
                    r = row(ch)
                    if r is None:
                        if not base.has(ch):
                            problem = "plays %s, which neither HFX nor the instance has" % ch
                        continue
                    ck = (r["kind"], r["name"].lower())
                    if ck in need or r["status"] == "same" or r["base"] == zgo_label:
                        continue
                    if r["cls"] == "gore":
                        if r["status"] == "new":
                            problem = "plays the gory %s, which the instance lacks" % ch
                        continue        # the instance's own version plays
                    need.add(ck)
                    stack.append(ck)
            if problem:
                bad[root] = problem
            else:
                want |= need
        if not bad:
            return rows, sorted(want), left_out
        for k, why in bad.items():
            left_out[rows[k]["name"]] = why
        roots = [k for k in roots if k not in bad]


def build(a):
    d = pack_build.load_def(a.defn)
    mo2 = pack_build.resolve(d["_base"], d["source"]["mo2"])
    game = pack_build.resolve(d["_base"], d["source"]["anomaly"])
    profile = d["source"]["profile"]
    zgo_layer = next(L for L in d["layer"] if L.get("zgo"))
    zgo_root = os.path.join(mo2, "mods", zgo_layer["folder"])
    px, sc_tree, kidfx_pe = tools(a.zgo or os.path.join(zgo_root, "tools", "zgo_kidfx"))
    paths = sc_tree.Paths(mo2=mo2, profile=profile, game=game, mod_root=zgo_root)
    bad = paths.check()
    if bad:
        raise SystemExit("; ".join(bad))

    sha, xr, hfx_tex, z = read_archive(a.archive, a.preset)
    if sha not in KNOWN:
        print("note: %s is not the archive this was made for (%s); check the result" % (a.archive, ", ".join(KNOWN.values())))
    hfx = px.loads(xr)
    base = Base(px, sc_tree, paths)
    rows, sel, left_out = select(px, kidfx_pe, hfx, base, sc_tree.ZGO_LABEL, hfx_tex)

    ship, kept = [], []
    seen = set()
    for kind, n in sel:
        if kind != "pe":
            continue
        for t in px.texture_names(hfx.effects[rows[(kind, n)]["name"]]):
            for ext in (".dds", ".thm"):
                rel = "textures/" + t.replace("\\", "/") + ext
                if rel in seen or rel not in hfx_tex:
                    continue
                seen.add(rel)
                w = base.texture(rel)
                if not w:
                    ship.append(rel)
                elif hashlib.sha1(z.read(hfx_tex[rel])).digest() != hashlib.sha1(w[0].read(w[1])).digest():
                    kept.append((rel, w[0].label))

    by = {}
    for k in sel:
        r = rows[k]
        key = "%s %s %s" % (k[0], r["name"].split("\\")[0], r["status"])
        by[key] = by.get(key, 0) + 1
    size = sum(z.getinfo(hfx_tex[rel]).file_size for rel in ship)
    print("HollywoodFX %s preset (%s)" % (a.preset, KNOWN.get(sha, sha[:12])))
    print("  %d effects and %d groups (%s)" % (sum(1 for k in sel if k[0] == "pe"), sum(1 for k in sel if k[0] == "pg"),
                                               ", ".join("%s %d" % kv for kv in sorted(by.items()))))
    print("  %d textures from the archive, %.0f MB; %d kept as the instance has them: %s"
          % (len(ship), size / 1e6, len(kept), ", ".join(r for r, _ in kept) or "-"))
    print("  left out: %d groups%s" % (len(left_out), "".join("\n    %s: %s" % kv for kv in sorted(left_out.items()))))
    if a.dry_run:
        return 0
    dest = os.path.abspath(a.out) if a.out else os.path.join(mo2, "mods", FOLDER)
    if not a.out and inst_mo2.running():
        print("Mod Organizer 2 is open; close it first (or give --out).")
        return 2
    if os.path.exists(dest) and not os.path.isfile(os.path.join(dest, MANIFEST)):
        raise SystemExit("%s exists and wasn't made by this script; move it away first" % dest)
    tmp = dest + ".building"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    part = os.path.join(tmp, "gamedata", "particles")
    for kind, n in sel:
        name = rows[(kind, n)]["name"]
        ent = hfx.effects[name] if kind == "pe" else hfx.groups[name]
        p = os.path.join(part, *name.lower().split("\\")) + "." + kind
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(px.entry_ini(kind, ent))
    for rel in ship:
        p = os.path.join(tmp, "gamedata", *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(z.read(hfx_tex[rel]))
    manifest = {
        "made_by": "Umbra dev/hfx_layer.py", "archive": KNOWN.get(sha, os.path.basename(a.archive)),
        "archive_sha256": sha, "preset": a.preset, "scope": list(SCOPE),
        "entries": [dict(rows[k], file="gamedata/particles/%s.%s" % (rows[k]["name"].lower().replace("\\", "/"), k[0]))
                    for k in sel],
        "textures": ship, "textures_kept_from_the_instance": [r for r, _ in kept], "left_out": left_out}
    with open(os.path.join(tmp, MANIFEST), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    with open(os.path.join(tmp, "README.txt"), "w", newline="\r\n") as f:
        f.write(README % dict(preset=a.preset, pe=sum(1 for k in sel if k[0] == "pe"),
                              pg=sum(1 for k in sel if k[0] == "pg"), tex=len(ship)))
    problems = verify(px, hfx, base, tmp)
    if problems:
        shutil.rmtree(tmp)
        raise SystemExit("the built mod fails its check, nothing written:\n  " + "\n  ".join(problems[:20]))
    meta = os.path.join(dest, "meta.ini")
    if os.path.isfile(meta):
        shutil.copy2(meta, os.path.join(tmp, "meta.ini"))
    else:
        with open(os.path.join(tmp, "meta.ini"), "w", newline="\r\n") as f:
            f.write("[General]\ngameName=stalkeranomaly\nmodid=0\nversion=3.1.5\nnewestVersion=\ncategory=\"-1,\"\n"
                    "installationFile=\nrepository=\ncomments=Built by Umbra (dev/hfx_layer.py)\nurl=\n"
                    "hasCustomURL=false\n\n[installedFiles]\nsize=0\n")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    print("wrote %s" % dest)
    return 0


def verify(px, hfx, base, folder):
    """What the game will see, read back from the written files: each file is HFX's entry
    exactly, every effect a group plays exists (here or in the instance), every texture an
    effect draws exists. Returns the problems."""
    part = os.path.join(folder, "gamedata", "particles")
    mine, problems = {}, []
    for dp, _, fns in os.walk(part):
        for fn in fns:
            if not fn.endswith((".pe", ".pg")):
                continue
            rel = os.path.relpath(os.path.join(dp, fn), part)
            kind, name = fn.rsplit(".", 1)[1], rel[:-3].replace(os.sep, "\\")
            with open(os.path.join(dp, fn), "rb") as f:
                mine[(kind, name)] = px.parse(kind, f.read(), name)
    for (kind, name), e in sorted(mine.items()):
        src = hfx.find(name)
        if not src or src[0] != kind or not px.same(e, src[1]):
            problems.append("%s.%s does not read back as HFX's" % (name, kind))
        if kind == "pg":
            for ch in px.children(e):
                n = ch.lower().replace("/", "\\")
                if ("pe", n) not in mine and ("pg", n) not in mine and not base.has(ch):
                    problems.append("%s plays %s, which is nowhere" % (name, ch))
        else:
            for tx in px.texture_names(e):
                rel = "textures/" + tx.replace("\\", "/") + ".dds"
                if not os.path.isfile(os.path.join(folder, "gamedata", *rel.split("/"))) and not base.texture(rel):
                    problems.append("%s draws %s, which is nowhere" % (name, tx))
    return problems


README = """HollywoodFX Impacts - Umbra

HollywoodFX 3.1 by Deathorchid: its bullet impacts, muzzle effects and explosions, as loose
particle files that go above Particles Cinematic VFX 3.5 (BOTZ YAWM) in Umbra's load order.
Built by Umbra's dev/hfx_layer.py from HollywoodFX 3.1.5 (%(preset)s preset):
%(pe)d effects, %(pg)d groups and %(tex)d of HollywoodFX's textures.

Left out on purpose: blood and flesh effects (Umbra Gore & Effects' own stay), HollywoodFX's
weapon configs (they move fire points and remove grenade fragments), and everything outside
firefights. umbra-hfx-layer.json lists every file and why it is here.

All effects and textures here are HollywoodFX's work; only the format changed (its compiled
particles.xr written out as the loose files the game also reads).
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--def", dest="defn", default=os.path.join(ROOT, "pack", "umbra.toml"))
    ap.add_argument("--archive", required=True, help="HollywoodFX 3.1's zip (HFX_v3.1.5.zip), as downloaded")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="realistic")
    ap.add_argument("--zgo", help="Umbra Gore & Effects' tools/zgo_kidfx (default: the instance's copy)")
    ap.add_argument("--dry-run", action="store_true", help="only say what it would write")
    ap.add_argument("--out", help="write the mod folder here instead of into the instance (to look at it)")
    return build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
