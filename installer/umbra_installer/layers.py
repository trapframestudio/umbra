"""
Putting layers in place: unpack each layer's volumes into its MO2 mod folder
(or into the Anomaly folder), put the player's paths into relocated files,
and leave a marker so a re-run, an update or a removal knows what is Umbra's.

Rules that keep a player's own files safe:
  * a mod folder is only written if it doesn't exist, or holds Umbra's marker
    (umbra.layer.json). Someone else's folder with the same name is left
    alone and the layer goes into "<folder> (Umbra)" instead.
  * an Anomaly-folder layer (xrRazom's engine) backs up every file it
    replaces into <anomaly>/.umbra-backup/<layer id>/ first.
  * a layer is unpacked into a temporary folder next to its target and moved
    into place only when every volume unpacked cleanly.
  * files a layer marks "preserve" (MCM settings) are the player's: the
    marker records the sha256 of what Umbra shipped, and an update keeps a
    file that no longer matches it, putting Umbra's new one beside it as
    <name>.umbra-new.
  * a layer that belongs to an extra profile (e.g. a single-player engine) may only add
    files to the Anomaly folder: it refuses to replace one it didn't create,
    so nothing the co-op setup runs is touched.
"""
import hashlib
import json
import os
import shutil
import struct
import time
import zipfile

from . import packdef

MARKER = "umbra.layer.json"
NEW_SUFFIX = ".umbra-new"


class LayerError(Exception):
    pass


def _safe_rel(name):
    n = name.replace("\\", "/")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or n.startswith("/") or ".." in parts or ":" in parts[0]:
        raise LayerError("refusing a file with an unsafe path in a pack volume: %r" % name)
    return "/".join(parts)


def _utc_mtime(info):
    """The exact time from the Info-ZIP extended timestamp the packer writes."""
    extra = info.extra
    i = 0
    while i + 4 <= len(extra):
        hid, size = struct.unpack("<HH", extra[i:i + 4])
        if hid == 0x5455 and size >= 5 and extra[i + 4] & 1:
            return struct.unpack("<l", extra[i + 5:i + 9])[0]
        i += 4 + size
    return time.mktime(info.date_time + (0, 0, -1))


def take_entries(infos, take):
    """What a layer takes from its author's own archive: [(rel, info)], in the archive's order.
    take: [[folder in the archive, folder in the layer], ...]; a later pair wins when two put a
    file in the same place (as a fomod's later folders do)."""
    out = {}
    for frm, to in take:
        for info in infos:
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.startswith(frm) or name == frm:
                continue
            rel = _safe_rel(to + name[len(frm):])
            out.pop(rel, None)
            out[rel] = info
    return list(out.items())


def volume_keys(layer):
    """What a layer was installed from: each volume's sha256 (and what it takes from it)."""
    return [v["sha256"] + ("|" + json.dumps(v["take"]) if v.get("take") else "") for v in layer["volumes"]]


def unpack(volumes, dest, progress=None, cancel=None, takes=None):
    """Unpack volume zips into dest; returns [rel paths]. takes[i]: only those files of
    volume i, where take_entries() puts them (an author's own archive)."""
    names = []
    takes = takes or [None] * len(volumes)
    plan, total = [], 0
    for v, take in zip(volumes, takes):
        if take:
            with zipfile.ZipFile(v) as z:
                ents = take_entries(z.infolist(), take)
            total += sum(i.compress_size for _, i in ents)
        else:
            ents = None
            total += os.path.getsize(v)
        plan.append((v, ents))
    done = 0
    for v, ents in plan:
        with zipfile.ZipFile(v) as z:
            for rel, info in ents if ents is not None else ((None, i) for i in z.infolist()):
                if info.is_dir():
                    continue
                if cancel and cancel():
                    raise KeyboardInterrupt
                rel = rel or _safe_rel(info.filename)
                out = os.path.join(dest, *rel.split("/"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(info) as fin, open(out, "wb") as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
                if os.name != "nt" and info.create_system == 3 and (info.external_attr >> 16) & 0o111:
                    os.chmod(out, 0o755)        # a Linux program (ZGO Setup's, say): runnable as installed
                t = _utc_mtime(info)
                os.utime(out, (t, t))
                names.append(rel)
                done += info.compress_size
                if progress:
                    progress(done, total, rel)
    return names


def win_path(path, under_wine):
    """How the game (a Windows program) sees a path."""
    if not under_wine:
        return path
    return "Z:" + os.path.abspath(path).replace("/", "\\")


def tokens(mo2, anomaly, zgo_cache, under_wine):
    """Token -> the player's path (raw; relocate() escapes where the file format needs it)."""
    out = {}
    for name, p in (("MO2", mo2), ("ANOMALY", anomaly), ("ZGO_CACHE", zgo_cache)):
        out["@@UMBRA_%s@@" % name] = p
        out["@@UMBRA_%s_WIN@@" % name] = win_path(p, under_wine)
        out["@@UMBRA_%s_WIN_ESC@@" % name] = win_path(p, under_wine)
    return out


ESCAPED_FORMATS = (".json", ".script", ".lua")


def relocate(folder, rels, toks):
    """Put the player's paths into files the packer tokenised. In JSON and Lua
    files a backslash must be written as two, and _ESC tokens always are."""
    for rel in rels:
        p = os.path.join(folder, *rel.split("/"))
        try:
            with open(p, "rb") as f:
                text = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        escaped = rel.lower().endswith(ESCAPED_FORMATS)
        for k in sorted(toks, key=len, reverse=True):     # _WIN_ESC before _WIN
            v = toks[k]
            if escaped or k.endswith("_ESC@@"):
                v = v.replace("\\", "\\\\")
            text = text.replace(k, v)
        with open(p, "wb") as f:
            f.write(text.encode("utf-8"))


def read_marker(folder):
    try:
        with open(os.path.join(folder, MARKER), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def marker_ok(folder, layer):
    """Already installed, same volumes."""
    m = read_marker(folder)
    return bool(m) and m.get("id") == layer["id"] and m.get("volumes") == volume_keys(layer)


def choose_folder(mods_dir, layer):
    """The folder this layer goes in: its own name, unless someone else's folder has it."""
    name = layer["folder"]
    for cand in (name, name + " (Umbra)", name + " (Umbra 2)"):
        p = os.path.join(mods_dir, cand)
        if not os.path.exists(p):
            return cand
        m = read_marker(p)
        if m and m.get("id") == layer["id"]:
            return cand
    raise LayerError('"%s" and its alternatives are taken by other mods' % name)


def renamed_folder(mods_dir, layer, other_ids=()):
    """The folder an earlier pack installed this layer in under an older name (layer["was"]):
    one holding Umbra's marker, for this layer or for none of the pack's other layers (other_ids:
    a layer whose id changed with its name). None if there is none."""
    for old in layer.get("was", []):
        for cand in (old, old + " (Umbra)", old + " (Umbra 2)"):
            p = os.path.join(mods_dir, cand)
            m = read_marker(p) if os.path.isdir(p) else None
            if m and (m.get("id") == layer["id"] or m.get("id") not in other_ids):
                return cand
    return None


def installed(mods_dir, layer, other_ids=()):
    """Where this layer is installed now: (folder, its marker). The folder it goes in when it
    isn't there yet, with marker None. An older name counts until Setup renames it."""
    folder = choose_folder(mods_dir, layer)
    m = read_marker(os.path.join(mods_dir, folder))
    if m and m.get("id") == layer["id"]:
        return folder, m
    old = renamed_folder(mods_dir, layer, other_ids)
    if old:
        return old, read_marker(os.path.join(mods_dir, old))
    return folder, None


def adopt_renamed(mods_dir, layer, other_ids=()):
    """Give a layer an earlier pack installed under an older name its new name, so it's updated
    in place (the player's preserved files with it) instead of installed again beside the old
    one. Returns (old folder, new folder), or None when there's nothing to rename."""
    new = choose_folder(mods_dir, layer)
    if os.path.exists(os.path.join(mods_dir, new)):
        return None
    old = renamed_folder(mods_dir, layer, other_ids)
    if not old:
        return None
    os.replace(os.path.join(mods_dir, old), os.path.join(mods_dir, new))
    m = read_marker(os.path.join(mods_dir, new))
    if m.get("id") != layer["id"]:
        m["id"] = layer["id"]
        with open(os.path.join(mods_dir, new, MARKER), "w", encoding="utf-8") as f:
            json.dump(m, f, indent=1)
    return old, new


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _rel_files(folder):
    out = []
    for dp, _dns, fns in os.walk(folder):
        for fn in fns:
            out.append(os.path.relpath(os.path.join(dp, fn), folder).replace(os.sep, "/"))
    return out


def keep_players_files(layer, old_dir, new_dir, names, old_marker, log=None):
    """Preserve rule for a mods layer: new_dir holds the new version, old_dir
    the installed one. Returns {rel: sha256 Umbra ships} for preserved files;
    new_dir is changed so the player's edited files win."""
    log = log or (lambda msg, level="info": None)
    shipped_before = (old_marker or {}).get("shipped", {})
    shipped = {}
    for rel in names:
        if packdef.preserved(layer, rel):
            shipped[rel] = file_sha256(os.path.join(new_dir, *rel.split("/")))
    if not old_dir or not os.path.isdir(old_dir):
        return shipped
    for rel in _rel_files(old_dir):
        if rel == MARKER or rel.endswith(NEW_SUFFIX) or not packdef.preserved(layer, rel):
            continue
        mine = os.path.join(old_dir, *rel.split("/"))
        cur = file_sha256(mine)
        before = shipped_before.get(rel)
        if cur == before or cur == shipped.get(rel):
            continue          # as Umbra shipped it (an update replaces or drops it), or already the new one
        # the player's: changed since Umbra shipped it, or their own file
        dst = os.path.join(new_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        where = layer.get("folder") or layer["id"]
        if rel in shipped:
            # Umbra's own version unchanged (the mod is updated for its other files): the player's
            # stays without a word, and a .umbra-new from an earlier update stays beside it
            same = shipped_before.get(rel) == shipped[rel]
            if not same or os.path.exists(mine + NEW_SUFFIX):
                os.replace(dst, dst + NEW_SUFFIX)
            if not same:
                log('Kept your changed "%s" in %s; Umbra\'s new version is beside it as %s%s'
                    % (rel, where, os.path.basename(rel), NEW_SUFFIX), "warn")
        else:
            log('Kept your own "%s" in %s' % (rel, where), "warn")
        shutil.copy2(mine, dst)
    return shipped


def files_missing(folder):
    """Fewer files in an installed layer's folder than it was installed with?"""
    m = read_marker(folder) or {}
    n = 0
    for _, _, fns in os.walk(folder):
        n += sum(1 for fn in fns if fn != MARKER and not fn.endswith(NEW_SUFFIX))
    return n < m.get("files", 0)


def install_mods_layer(mods_dir, layer, volumes, toks, progress=None, cancel=None, pack_version="", log=None,
                       repair=False):
    """Install a mods-target layer; returns the folder name used.
    repair: also reinstall it when its folder lost files."""
    folder = choose_folder(mods_dir, layer)
    final = os.path.join(mods_dir, folder)
    if os.path.isdir(final) and marker_ok(final, layer) and not (repair and files_missing(final)):
        return folder, False
    tmp = os.path.join(mods_dir, ".umbra-tmp-" + layer["id"])
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    try:
        names = unpack(volumes, tmp, progress, cancel, [v.get("take") for v in layer["volumes"]])
        if "meta_ini" in layer and "meta.ini" not in names:   # an author's archive has no MO2 meta.ini
            with open(os.path.join(tmp, "meta.ini"), "w", encoding="utf-8", newline="") as f:
                f.write(layer["meta_ini"])
            names.append("meta.ini")
        relocate(tmp, layer.get("relocate", []), toks)
        old_marker = read_marker(final) if os.path.isdir(final) else None
        shipped = keep_players_files(layer, final if old_marker else None, tmp, names, old_marker, log)
        info = {"id": layer["id"], "version": layer.get("version", ""), "pack": pack_version,
                "volumes": volume_keys(layer), "files": len(names)}
        if shipped:
            info["shipped"] = shipped
        with open(os.path.join(tmp, MARKER), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
        if os.path.exists(final):
            old = final + ".umbra-old"
            if os.path.exists(old):
                shutil.rmtree(old)
            os.replace(final, old)
            os.replace(tmp, final)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return folder, True


def find_ci(base, rel):
    """The existing path for rel under base, matching names case-insensitively
    (the game, a Windows program, sees them that way); None if there is none."""
    cur = base
    for part in rel.split("/"):
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        if part in names:
            cur = os.path.join(cur, part)
            continue
        low = [n for n in names if n.lower() == part.lower()]
        if not low:
            return None
        cur = os.path.join(cur, low[0])
    return cur


def anomaly_marker(anomaly, layer_id):
    try:
        with open(os.path.join(anomaly, ".umbra-backup", layer_id, MARKER), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def anomaly_layer_changed(anomaly, layer, m=None):
    """The files of an installed Anomaly-folder layer that are no longer Umbra's (missing, or
    replaced: a G.A.M.M.A. install or update puts its own engine over xrRazom's). Files the
    player may change (the layer's preserve list) don't count. A marker from before Umbra
    kept each file's sha256 can't be checked file by file: all its files count."""
    m = m if m is not None else anomaly_marker(anomaly, layer["id"])
    if not m:
        return None
    shas = m.get("sha256")
    out = []
    for rel in m.get("files", []):
        if packdef.preserved(layer, rel):
            continue
        p = os.path.join(anomaly, *rel.split("/"))
        if not os.path.isfile(p) or shas is None or rel not in shas or (
                os.path.getsize(p) != m.get("sizes", {}).get(rel, -1) or file_sha256(p) != shas[rel]):
            out.append(rel)
    return out


def install_anomaly_layer(anomaly, layer, volumes, toks, progress=None, cancel=None, pack_version="",
                          new_only=False, log=None):
    """Copy a layer into the Anomaly folder itself, backing up what it replaces.
    new_only: refuse to replace any file this layer didn't put there itself
    (layers of extra profiles: the co-op engine's files are never touched)."""
    log = log or (lambda msg, level="info": None)
    state_dir = os.path.join(anomaly, ".umbra-backup", layer["id"])
    mpath = os.path.join(state_dir, MARKER)
    m = anomaly_marker(anomaly, layer["id"])
    changed = anomaly_layer_changed(anomaly, layer, m) if m else None
    if m and m.get("volumes") == volume_keys(layer) and not changed:
        return False
    if m and changed and m.get("sha256"):
        log("%s: %d file(s) in the Anomaly folder are not Umbra's any more (%s); putting them back"
            % (layer["id"], len(changed), ", ".join(changed[:4]) + (", ..." if len(changed) > 4 else "")), "warn")
    tmp = os.path.join(anomaly, ".umbra-tmp-" + layer["id"])
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    try:
        names = unpack(volumes, tmp, progress, cancel, [v.get("take") for v in layer["volumes"]])
        relocate(tmp, layer.get("relocate", []), toks)
        backed = list((m or {}).get("backed_up", []))
        created = list((m or {}).get("created", []))
        if new_only:
            clash = [rel for rel in names if rel not in created and find_ci(anomaly, rel)]
            if clash:
                raise LayerError("%s would replace %s in the Anomaly folder; it may only add new files"
                                 % (layer["id"], ", ".join(clash[:5])))
        ours = set((m or {}).get("files", []))
        shipped_before = (m or {}).get("shipped", {})
        ours_before = (m or {}).get("sha256", {})
        legacy = bool(m) and "sha256" not in m          # a marker from before Umbra kept hashes
        shipped = {}
        shas, sizes = {}, {}

        def foreign(dst, rel):
            """Someone else's file where Umbra's was (a G.A.M.M.A. install or update)?"""
            cur = file_sha256(dst)
            if cur == shas[rel]:
                return False
            return cur != ours_before[rel] if rel in ours_before else legacy
        for rel in names:
            src = os.path.join(tmp, *rel.split("/"))
            dst = os.path.join(anomaly, *rel.split("/"))
            if packdef.preserved(layer, rel):
                shipped[rel] = file_sha256(src)
                if rel in ours and os.path.isfile(dst):
                    cur = file_sha256(dst)
                    if cur != shipped_before.get(rel) and cur != shipped[rel]:
                        os.replace(src, dst + NEW_SUFFIX)
                        log('Kept your changed "%s"; Umbra\'s new version is beside it as %s%s'
                            % (rel, os.path.basename(rel), NEW_SUFFIX), "warn")
                        continue
            shas[rel], sizes[rel] = file_sha256(src), os.path.getsize(src)
            known = rel in backed or rel in created
            b = os.path.join(state_dir, "files", *rel.split("/"))
            if os.path.exists(dst) and not known:
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(dst, b)
                backed.append(rel)
            elif known and os.path.isfile(dst) and foreign(dst, rel):
                # someone else's file is the original now: removing Umbra puts that back (not
                # the one from before, and not nothing where Umbra had made the file)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(dst, b)
                if rel in created:
                    created.remove(rel)
                    backed.append(rel)
            elif not os.path.exists(dst) and not known:
                created.append(rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
        os.makedirs(state_dir, exist_ok=True)
        info = {"id": layer["id"], "version": layer.get("version", ""), "pack": pack_version,
                "volumes": volume_keys(layer), "files": names,
                "sha256": shas, "sizes": sizes, "backed_up": backed, "created": created}
        if shipped:
            info["shipped"] = shipped
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def restore_anomaly_layer(anomaly, layer_id):
    """Undo install_anomaly_layer: put the originals back, delete what it added."""
    state_dir = os.path.join(anomaly, ".umbra-backup", layer_id)
    try:
        with open(os.path.join(state_dir, MARKER), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return False
    for rel in m.get("backed_up", []):
        src = os.path.join(state_dir, "files", *rel.split("/"))
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(anomaly, *rel.split("/")))
    for rel in m.get("created", []):
        try:
            os.unlink(os.path.join(anomaly, *rel.split("/")))
        except OSError:
            pass
    for rel in m.get("files", []):
        try:
            os.unlink(os.path.join(anomaly, *rel.split("/")) + NEW_SUFFIX)
        except OSError:
            pass
    shutil.rmtree(state_dir, ignore_errors=True)
    return True
