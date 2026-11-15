"""
The published pack: what the installer downloads and how to read it.

Everything is content-addressed and lives under one base URL (a Backblaze B2
bucket, ideally behind Cloudflare):

    channels/<channel>.json   {"format": 1, "version": "...", "pack": "packs/umbra-<v>.json", "sha256": "..."}
    packs/umbra-<v>.json      the pack manifest (below), checked against the channel's sha256
    objects/<sha256>.<ext>    every file the manifest names, checked against its sha256
    umbra-effects/<fingerprint>/<sha256>.<ext>   prebaked body models for Umbra Gore & Effects (below)

The channel file is the only thing that changes in place; publishing a new
version uploads the new objects and pack first, then switches the channel.

Pack manifest, format 5 (5 added a layer's prebaked bake; 4 added volumes that
come from their author's own page; 3 added options with choices):

    {
      "format": 3,
      "name": "Umbra", "version": "0.1.0", "channel": "stable", "built": "2026-11-14",
      "gamma": {
        "cli": {"version": "1.41.1",
                "assets": {"linux-x64": {"url": ..., "sha256": ...}, "win-x64": {...}}},
        "repos": {"stalker-gamma": "<commit>", ...},
        "mo2_profile": "G.A.M.M.A",
        "modpack_maker_list": <object>, "modlist": <object>
      },
      "profile": <profile>,              # the main profile (co-op); MO2 opens it
      "profiles": [<profile>, ...],      # more profiles, each behind an option
      "user_ltx": {"lines": ["r3_msaa st_opt_off", ...]},   # added to appdata/user.ltx if missing
      "options": [{"id": "hd-characters", "title": "...", "description": "...", "default": false},
                  {"id": "ccon-season", "title": "...", "description": "...", "default": "autumn",
                   "choices": [{"id": "spring", "title": "Spring"}, ...]}],   # pick exactly one
      "layers": [                        # highest priority first
        {"id": "zgo", "folder": "Umbra Gore & Effects", "target": "mods" | "anomaly",
         "version": "1.7.0", "files": 5853, "bytes": 7.1e9, "credit": "zgo",
         "relocate": ["zgo_bake_report.json", ...],   # files with @@UMBRA_*@@ tokens
         "option": "hd-characters",       # only installed when the player picks that option
                                          # ("ccon-season=summer": when that choice is picked)
         "profile": "extra",              # only switched on in that profile (implies its option)
         "preserve": true | ["gamedata/configs/*.ltx"],   # the player's edits survive updates
         "was": ["Old Folder Name"],      # the folder's earlier names: Setup renames an installed one
         "meta_ini": "[General]\\r\\n...",   # MO2's meta.ini, for a layer whose volume has none
         "bake": <bake object>,           # Umbra Gore & Effects' layer: its prebaked bake (below)
         "volumes": [<object> | <origin>, ...]}
      ],
      "dependencies": [{"id", "title", "url", "profile", "required", "fetched", "notes"}],
      "credits": [{"id", "mod", "authors", "url", "role", "notes"}, ...]
    }

    <profile> = {"id": "main" | "<extra id>", "name": "Umbra", "base": "G.A.M.M.A", "modlist": <object>,
                 "option": "<option id>",                # extra profiles only
                 "executables": [{"title": "Anomaly (other engine)", "in": "anomaly",
                                  "binary": "bin/AnomalyOther.exe", "cwd": "bin",
                                  "arguments": "-ltx user_other.ltx"}],
                 "user_ltx": {"file": "user_other.ltx", "seed": "user.ltx", "lines": [...]},
                 "steam": {"title": "Umbra (other engine)", "run": "Anomaly (other engine)"}}

    <object> = {"path": "objects/<sha256>.zip", "sha256": "...", "size": <bytes>,
                "files": <entries, volumes only>, "bytes": <unpacked bytes, volumes only>}

    <origin> = {"path": "origins/<sha256>.zip", "sha256": "...", "size": <bytes>, "files", "bytes",
                "origin": {"name": "xrRazom-1.5-Slim.zip", "site": "ModDB",
                           "page": "https://www.moddb.com/mods/.../addons/...",
                           "download": "https://www.moddb.com/addons/start/314090"},
                "take": [["gamedata/", "gamedata/"], ["gamma/gamedata/", "gamedata/"]]}

Origins: a mod whose author asks that players download it from their own
page (xrRazom, from ModDB, so its downloads are counted). Umbra never hosts
it: Setup takes the file from the player's Downloads folder if it is there,
or downloads it from the author's page itself (the page's download link, then
its mirror link, as a browser does), or asks the player to download it there.
It is the author's archive exactly as published (sha256 pinned), so "take"
says which of its folders the layer uses and where they go (in order; a later
pair wins, as in the mod's own fomod). Two layers may share one archive.

Choices: the player's picks are a set of strings, an option's id for a tick
box and "<option>=<choice>" for an option with choices. normalize_choices()
makes any set whole (exactly one choice per such option). The installer
remembers the picks in its state file and starts from them on the next run.

Preserve: files a layer marks as the player's to change (MCM settings). The
installer records the hash of what it shipped; on an update, a file that no
longer matches it is kept and the new one is put beside it as <name>.umbra-new.

user.ltx is one file for every MO2 profile (MO2 keeps no per-profile copy for
Anomaly). A profile whose engine needs other settings gets its own file
("file"), which its executables load with the engine's -ltx switch. It starts
as a copy of "seed" with "lines" applied over it; after that only missing
keys are added, like user.ltx itself.

Prebaked bakes: Umbra Gore & Effects bakes its body models for the player's own
mod list (Umbra Effects Setup, tools/zgo_setup in its folder). Umbra publishes
that bake, made for the pack's mod list, in its own folder of the bucket, named
by the bake's fingerprint (the same mod list content gives the same fingerprint
on every machine). The layer is the mod without it; the layer's "bake" is the
bake's manifest, <bake object> = {"path": "umbra-effects/<fingerprint>/<sha256>.json",
"sha256", "size", "download": <its volumes' bytes>, "bytes": <unpacked>} (the
last two for the plan's free-space check):

    {"format": 1, "kind": "umbra-effects-bake", "fingerprint": "<md5 hex>",
     "baker": 3, "zgo": "1.8.0", "made": "2026-11-15 12:00:00", "models": 4790,
     "files": 83789, "bytes": 7.7e9,
     "relocate": ["zgo_bake_report.json", ...],   # files with @@UMBRA_*@@ tokens
     "report": <volume>,                          # the reports alone, fetched first
     "volumes": [<volume>, ...]}                  # the baked files

with every <volume> = {"path": "umbra-effects/<fingerprint>/<sha256>.zip", "sha256",
"size", "files", "bytes"}. Setup unpacks the report volume first and asks the
installed Umbra Effects Setup whether it fits the player's mod list (--match);
only then does it download the rest. When it does not fit, or the download
fails, Umbra Effects Setup bakes for the player's mod list instead.

Relocation tokens (dev/pack_build.py writes them in place of the source
machine's paths; layers.relocate() puts the player's in):

    @@UMBRA_MO2@@          the MO2 instance folder, native form (/home/x/... or C:\\...)
    @@UMBRA_MO2_WIN@@      the same as the game sees it (C:\\... or Z:\\home\\x\\... under Wine)
    @@UMBRA_ANOMALY@@      the Anomaly folder, native form
    @@UMBRA_ANOMALY_WIN@@  the same as the game sees it
    @@UMBRA_ZGO_CACHE@@    Umbra Gore & Effects' baker work folder (its bake.py cache_dir)
Each comes in a JSON/Lua-escaped variant with _ESC appended (backslashes doubled).
A bake also has two names:
    @@UMBRA_PROFILE@@      the MO2 profile it is installed for (the pack's main profile)
    @@UMBRA_ZGO_FOLDER@@   Umbra Gore & Effects' folder in MO2's mods folder
"""
import fnmatch
import hashlib
import json
import re

FORMAT = 5
BAKE_FORMAT = 1
TOKENS = ("MO2", "ANOMALY", "ZGO_CACHE")
MAIN = "main"                         # the main profile's id


class PackError(Exception):
    pass


def sha256_file(path, cancel=None, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            if cancel and cancel():
                raise KeyboardInterrupt
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


_SHA = re.compile(r"^[0-9a-f]{64}$")
_URL = re.compile(r"^https?://[^\s\"'<>]+$")


def check_object(o, what, prefix=None):
    """prefix: the folder its path must be in (default objects/, origins/ for an origin)."""
    if not isinstance(o, dict):
        raise PackError("%s: not an object entry" % what)
    if not _SHA.match(str(o.get("sha256", ""))):
        raise PackError("%s: bad sha256" % what)
    p = str(o.get("path", ""))
    if not p.startswith(prefix or ("origins/" if "origin" in o else "objects/")) or ".." in p or "\\" in p:
        raise PackError("%s: bad path %r" % (what, p))
    if not isinstance(o.get("size"), int) or o["size"] < 0:
        raise PackError("%s: bad size" % what)
    if "origin" in o:
        og = o["origin"]
        if not isinstance(og, dict) or not safe_name(str(og.get("name", ""))) or \
                not all(_URL.match(str(og.get(k, ""))) for k in ("page", "download")):
            raise PackError("%s: an author's download needs a file name, its page and its download link" % what)
    if "take" in o:
        take = o["take"]
        if not isinstance(take, list) or not take or not all(
                isinstance(t, list) and len(t) == 2 and all(isinstance(x, str) for x in t) for t in take):
            raise PackError("%s: take must be a list of [folder in the archive, folder in the layer]" % what)
        for t in take:
            for x in t:
                if x.startswith("/") or "\\" in x or ".." in x.split("/") or ":" in x or (x and not x.endswith("/")):
                    raise PackError("%s: bad take folder %r" % (what, x))


def check_channel(ch):
    if not isinstance(ch, dict) or ch.get("format") != FORMAT:
        raise PackError("this installer reads pack format %d; the channel says %r. Get the newest installer."
                        % (FORMAT, ch.get("format") if isinstance(ch, dict) else ch))
    if not _SHA.match(str(ch.get("sha256", ""))) or not str(ch.get("pack", "")).startswith("packs/"):
        raise PackError("the channel file is damaged")
    return ch


def check_pack(p):
    """Validate a pack manifest (raises PackError); returns it."""
    if not isinstance(p, dict) or p.get("format") != FORMAT:
        raise PackError("this installer reads pack format %d; the pack is format %r. Get the newest installer."
                        % (FORMAT, p.get("format") if isinstance(p, dict) else p))
    for k in ("name", "version", "gamma", "profile", "layers"):
        if k not in p:
            raise PackError("the pack has no %r" % k)
    g = p["gamma"]
    for k in ("modpack_maker_list", "modlist"):
        check_object(g.get(k), "gamma." + k)
    if not g.get("cli", {}).get("assets"):
        raise PackError("the pack names no stalker-gamma-cli")
    opt_ids, layer_opts = set(), set()
    for o in p.get("options", []):
        if not o.get("id") or o["id"] in opt_ids or "=" in o["id"]:
            raise PackError("an option has no id or a repeated one")
        opt_ids.add(o["id"])
        if "choices" in o:
            cids = [c.get("id") for c in o["choices"]] if isinstance(o["choices"], list) else []
            if not cids or not all(cids) or len(set(cids)) != len(cids) or any("=" in c for c in cids):
                raise PackError("option %s: its choices need ids, each once" % o["id"])
            if o.get("default") not in cids:
                raise PackError("option %s: its default is not one of its choices" % o["id"])
            layer_opts |= {choice_key(o["id"], c) for c in cids}
        else:
            layer_opts.add(o["id"])
    prof_ids, prof_names = set(), set()
    for i, pr in enumerate(all_profiles(p)):
        what = "profile %d (%s)" % (i, pr.get("name"))
        if not pr.get("id") or pr["id"] in prof_ids:
            raise PackError("%s: missing or repeated id" % what)
        if not safe_name(pr.get("name", "")) or pr["name"].lower() in prof_names:
            raise PackError("%s: bad or repeated name" % what)
        if i and (not pr.get("option") or pr["option"] not in opt_ids):
            raise PackError("%s: an extra profile needs a known option" % what)
        prof_ids.add(pr["id"])
        prof_names.add(pr["name"].lower())
        check_object(pr.get("modlist"), what + " modlist")
        check_profile_extras(pr, what)
    if p["profile"].get("base", "").lower() in prof_names:
        raise PackError("a profile has the base profile's name")
    check_lines(p.get("user_ltx", {}).get("lines", []), "user_ltx")
    seen_ids, seen_folders = set(), set()
    for i, L in enumerate(p["layers"]):
        what = "layer %d (%s)" % (i, L.get("id"))
        if not L.get("id") or L["id"] in seen_ids:
            raise PackError("%s: missing or repeated id" % what)
        seen_ids.add(L["id"])
        target = L.get("target", "mods")
        if target not in ("mods", "anomaly"):
            raise PackError("%s: unknown target %r" % (what, target))
        if target == "mods":
            f = L.get("folder", "")
            if not safe_name(f) or f.lower() in seen_folders:
                raise PackError("%s: bad or repeated folder %r" % (what, f))
            seen_folders.add(f.lower())
        was = L.get("was", [])
        if not isinstance(was, list) or (was and target != "mods") or not all(
                isinstance(x, str) and safe_name(x) for x in was):
            raise PackError("%s: was must list the mods folder's earlier names" % what)
        if L.get("option") and L["option"] not in layer_opts:
            raise PackError("%s: unknown option %r" % (what, L["option"]))
        if L.get("profile"):
            pr = profile_by_id(p, L["profile"])
            if not pr or pr["id"] == MAIN or L.get("option") != pr.get("option"):
                raise PackError("%s: unknown profile %r, or not behind its option" % (what, L["profile"]))
        pres = L.get("preserve", False)
        if not (isinstance(pres, bool) or (isinstance(pres, list) and all(isinstance(x, str) for x in pres))):
            raise PackError("%s: preserve must be true/false or a list of patterns" % what)
        if not L.get("volumes"):
            raise PackError("%s: no volumes" % what)
        if "meta_ini" in L and (target != "mods" or not isinstance(L["meta_ini"], str)):
            raise PackError("%s: meta_ini is text, for a mods layer" % what)
        for j, v in enumerate(L["volumes"]):
            check_object(v, "%s volume %d" % (what, j))
        if "bake" in L:
            if L.get("target", "mods") != "mods":
                raise PackError("%s: only a mods layer has a bake" % what)
            check_object(L["bake"], what + " bake", prefix=BAKE_DIR + "/")
            if not _BAKE_PATH.match(L["bake"]["path"]) or \
                    _BAKE_PATH.match(L["bake"]["path"]).group(2) != L["bake"]["sha256"]:
                raise PackError("%s: bad bake path %r" % (what, L["bake"]["path"]))
            if not all(isinstance(L["bake"].get(k, 0), int) and L["bake"].get(k, 0) >= 0 for k in ("download", "bytes")):
                raise PackError("%s: bad bake sizes" % what)
    folders = {L["folder"].lower() for L in p["layers"] if L.get("target", "mods") == "mods"}
    olds = [x.lower() for L in p["layers"] for x in L.get("was", [])]
    if len(olds) != len(set(olds)) or folders & set(olds):
        raise PackError("a layer's earlier folder name (was) is another layer's folder, or listed twice")
    for d in p.get("dependencies", []):
        if not d.get("id") or not d.get("title"):
            raise PackError("a dependency has no id or title")
    return p


BAKE_DIR = "umbra-effects"       # the bucket folder of the prebaked bakes
_BAKE_PATH = re.compile(r"^%s/([0-9a-f]{32})/([0-9a-f]{64})\.(json|zip)$" % BAKE_DIR)


def check_bake(b, ref=None):
    """A prebaked bake's manifest (see the top); ref: the layer's bake object it was fetched by."""
    if not isinstance(b, dict) or b.get("kind") != "umbra-effects-bake":
        raise PackError("the bake manifest is not one")
    if b.get("format") != BAKE_FORMAT:
        raise PackError("this installer reads bake format %d; the bake says %r. Get the newest installer."
                        % (BAKE_FORMAT, b.get("format")))
    fp = str(b.get("fingerprint", ""))
    if not re.match(r"^[0-9a-f]{32}$", fp):
        raise PackError("the bake has a bad fingerprint")
    if ref is not None:
        m = _BAKE_PATH.match(str(ref.get("path", "")))
        if not m or m.group(1) != fp:
            raise PackError("the bake manifest is not the one its pack names")
    vols = b.get("volumes")
    if not isinstance(vols, list) or not vols:
        raise PackError("the bake has no volumes")
    for j, v in enumerate([b.get("report")] + vols):
        what = "bake report" if j == 0 else "bake volume %d" % j
        check_object(v, what, prefix="%s/%s/" % (BAKE_DIR, fp))
        m = _BAKE_PATH.match(v["path"])
        if not m or m.group(2) != v["sha256"] or m.group(3) != "zip":
            raise PackError("%s: bad path %r" % (what, v["path"]))
    rel = b.get("relocate", [])
    if not isinstance(rel, list) or not all(isinstance(r, str) and safe_rel(r) for r in rel):
        raise PackError("the bake's relocate list is bad")
    for k in ("baker", "models", "files", "bytes"):
        if not isinstance(b.get(k, 0), int):
            raise PackError("the bake's %s is not a number" % k)
    return b


def safe_rel(r):
    """A relative path inside a folder: forward slashes, no .., no drive or root."""
    return bool(r) and not r.startswith("/") and "\\" not in r and ":" not in r and \
        ".." not in r.split("/")


def safe_name(n):
    """A folder, profile or file name with no path in it."""
    return bool(n) and n == n.strip() and not any(c in n for c in '/\\:*?"<>|') and n not in (".", "..")


def check_lines(lines, what):
    if not isinstance(lines, list) or not all(isinstance(x, str) and "\n" not in x and "\r" not in x
                                              for x in lines):
        raise PackError("%s: lines must be a list of one-line strings" % what)


def check_profile_extras(pr, what):
    for e in pr.get("executables", []):
        b = str(e.get("binary", ""))
        if not e.get("title") or e.get("in", "anomaly") not in ("anomaly", "mo2") or not b or \
                b.startswith(("/", "\\")) or ".." in b.replace("\\", "/").split("/") or ":" in b:
            raise PackError("%s: bad executable %r" % (what, e.get("title")))
    u = pr.get("user_ltx")
    if u:
        for k in ("file", "seed"):
            v = u.get(k)
            if v is not None and (not safe_name(v) or " " in v or not v.lower().endswith(".ltx")):
                raise PackError("%s: bad user_ltx %s %r" % (what, k, v))
        check_lines(u.get("lines", []), what + " user_ltx")
    s = pr.get("steam")
    if s and (not s.get("title") or (s.get("run") and s["run"] not in
                                     [e["title"] for e in pr.get("executables", [])])):
        raise PackError("%s: its Steam shortcut names no title, or an executable it doesn't have" % what)


def all_profiles(p):
    """The main profile first, then the extra ones (each with its id)."""
    main = dict(p["profile"])
    main.setdefault("id", MAIN)
    return [main] + list(p.get("profiles", []))


def profile_by_id(p, pid):
    for pr in all_profiles(p):
        if pr["id"] == pid:
            return pr
    return None


def chosen_profiles(p, choices=None):
    """The profiles this install gets: the main one, plus those of chosen options."""
    choices = normalize_choices(p, choices)
    return [pr for pr in all_profiles(p) if not pr.get("option") or pr["option"] in choices]


def other_ids(pack, layer):
    """The ids of the pack's layers other than this one."""
    return {L["id"] for L in pack["layers"]} - {layer["id"]}


def preserved(layer, rel):
    """Is this file of the layer the player's to change (kept on updates)?"""
    rule = layer.get("preserve", False)
    if rule is True:
        return True
    if not rule:
        return False
    r = rel.lower()
    return any(fnmatch.fnmatchcase(r, pat.lower()) for pat in rule)


def loads(data, what):
    try:
        return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (ValueError, UnicodeDecodeError) as e:
        raise PackError("%s is not valid JSON (%s)" % (what, e))


def choice_key(oid, cid):
    """How a picked choice is written in a set of choices and in a layer's "option"."""
    return "%s=%s" % (oid, cid)


def default_choices(p):
    out = set()
    for o in p.get("options", []):
        if o.get("choices"):
            out.add(choice_key(o["id"], o["default"]))
        elif o.get("default"):
            out.add(o["id"])
    return out


def normalize_choices(p, choices):
    """The player's picks made whole for this pack: ids it doesn't know are
    dropped, and every option with choices has exactly one (its default when
    none, or more than one, of its choices is picked). None: the defaults."""
    if choices is None:
        return default_choices(p)
    choices, out = set(choices), set()
    for o in p.get("options", []):
        if o.get("choices"):
            picked = [c["id"] for c in o["choices"] if choice_key(o["id"], c["id"]) in choices]
            out.add(choice_key(o["id"], picked[0] if len(picked) == 1 else o["default"]))
        elif o["id"] in choices:
            out.add(o["id"])
    return out


def picked(p, choices, oid):
    """The choice picked for an option with choices."""
    for k in normalize_choices(p, choices):
        if k.startswith(oid + "="):
            return k.split("=", 1)[1]
    return None


def chosen_layers(p, choices=None):
    """The layers this install gets: every layer without an option, plus those of chosen options."""
    choices = normalize_choices(p, choices)
    return [L for L in p["layers"] if not L.get("option") or L["option"] in choices]


def unique_volumes(layers):
    """The volumes of these layers, each once (layers may share an author's archive)."""
    out = {}
    for L in layers:
        for v in L["volumes"]:
            out.setdefault(v["sha256"], v)
    return list(out.values())


def origins(p, choices=None):
    """The authors' own downloads these picks need (each once)."""
    return [v for v in unique_volumes(chosen_layers(p, choices)) if "origin" in v]


def option_bytes(p, oid):
    """Download size of a tick box's layers, or of one choice's ("<option>=<choice>")."""
    return sum(v["size"] for v in unique_volumes([L for L in p["layers"] if L.get("option") == oid]))


def total_download(p, choices=None):
    return sum(v["size"] for v in unique_volumes(chosen_layers(p, choices)))


def total_unpacked(p, choices=None):
    return sum(L.get("bytes", 0) for L in chosen_layers(p, choices))


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1000.0
