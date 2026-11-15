"""
A Steam shortcut ("non-Steam game") for Umbra: Steam's userdata/<id>/config/shortcuts.vdf
(binary VDF), its library artwork (config/grid/), and on Linux the Proton
version to run it with (config/config.vdf, CompatToolMapping).

Steam rewrites both files when it exits, so it has to be closed while they
are edited. Every file is backed up next to itself (*.umbra-bak) first, and
other shortcuts are kept as they are.
"""
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import zlib

# ---------------------------------------------------------------- binary VDF (shortcuts.vdf)
T_MAP, T_STR, T_INT, T_END = 0, 1, 2, 8


class VdfError(Exception):
    pass


def bvdf_load(data):
    """Binary VDF -> nested dicts (order kept). Ints stay ints, strings str."""
    pos = 0

    def cstr():
        nonlocal pos
        end = data.index(b"\0", pos)
        s = data[pos:end].decode("utf-8", errors="surrogateescape")
        pos = end + 1
        return s

    def read_map():
        nonlocal pos
        out = {}
        while True:
            if pos >= len(data):
                raise VdfError("binary VDF ends early")
            t = data[pos]
            pos += 1
            if t == T_END:
                return out
            key = cstr()
            if t == T_MAP:
                out[key] = read_map()
            elif t == T_STR:
                out[key] = cstr()
            elif t == T_INT:
                out[key] = int.from_bytes(data[pos:pos + 4], "little", signed=True)
                pos += 4
            else:
                raise VdfError("unknown binary VDF type %d at %d" % (t, pos - 1))

    root = read_map()
    return root


def bvdf_dump(obj):
    out = bytearray()

    def write_map(m):
        for k, v in m.items():
            kb = k.encode("utf-8", errors="surrogateescape") + b"\0"
            if isinstance(v, dict):
                out.append(T_MAP)
                out.extend(kb)
                write_map(v)
            elif isinstance(v, bool) or isinstance(v, int):
                out.append(T_INT)
                out.extend(kb)
                out.extend(int(v).to_bytes(4, "little", signed=True))
            else:
                out.append(T_STR)
                out.extend(kb)
                out.extend(str(v).encode("utf-8", errors="surrogateescape") + b"\0")
        out.append(T_END)

    write_map(obj)
    return bytes(out)


# ---------------------------------------------------------------- text VDF (config.vdf)

_TOK = re.compile(r'\s*(?:(//[^\n]*)|(\{)|(\})|"((?:[^"\\]|\\.)*)"|([^\s{}"]+))', re.S)


def tvdf_load(text):
    toks = []
    pos = 0
    while pos < len(text):
        m = _TOK.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise VdfError("text VDF: cannot read at %d" % pos)
        pos = m.end()
        if m.group(1):
            continue
        if m.group(2):
            toks.append("{")
        elif m.group(3):
            toks.append("}")
        else:
            s = m.group(4) if m.group(4) is not None else m.group(5)
            if m.group(4) is not None:
                s = re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t"}.get(e.group(1), e.group(1)), s)
            toks.append(("s", s))
    i = 0

    def read_map(top):
        nonlocal i
        out = {}
        while i < len(toks):
            t = toks[i]
            if t == "}":
                if top:
                    raise VdfError("text VDF: unexpected }")
                i += 1
                return out
            if t == "{":
                raise VdfError("text VDF: unexpected {")
            key = t[1]
            i += 1
            if i >= len(toks):
                raise VdfError("text VDF: key without value")
            if toks[i] == "{":
                i += 1
                out[key] = read_map(False)
            elif toks[i] == "}":
                raise VdfError("text VDF: key without value")
            else:
                out[key] = toks[i][1]
                i += 1
        if not top:
            raise VdfError("text VDF: missing }")
        return out

    return read_map(True)


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def tvdf_dump(obj, depth=0):
    out = []
    ind = "\t" * depth
    for k, v in obj.items():
        if isinstance(v, dict):
            out.append("%s%s\n%s{\n%s%s}\n" % (ind, _q(k), ind, tvdf_dump(v, depth + 1), ind))
        else:
            out.append("%s%s\t\t%s\n" % (ind, _q(k), _q(str(v))))
    return "".join(out)


# ---------------------------------------------------------------- finding Steam

FLATPAK_ID = "com.valvesoftware.Steam"      # Steam from Flathub


def steam_roots():
    """Steam installs on this machine (real paths, no duplicates)."""
    cands = []
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                cands.append(winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\"))
        except OSError:
            pass
        cands += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    else:
        h = os.path.expanduser("~")
        cands += [os.path.join(h, ".local/share/Steam"), os.path.join(h, ".steam/steam"),
                  os.path.join(h, ".steam/root"),
                  os.path.join(h, ".var/app", FLATPAK_ID, ".local/share/Steam"),     # Flathub
                  os.path.join(h, "snap/steam/common/.local/share/Steam")]           # Ubuntu's Snap
    out, seen = [], set()
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "userdata")):
            r = os.path.realpath(c)
            if r not in seen:
                seen.add(r)
                out.append(r)
    return out


def users(root):
    """Steam account ids that have used this install, most recently used first."""
    ud = os.path.join(root, "userdata")
    out = []
    for n in os.listdir(ud) if os.path.isdir(ud) else []:
        if not n.isdigit() or n == "0":
            continue
        cfg = os.path.join(ud, n, "config")
        t = 0
        for f in ("localconfig.vdf", "shortcuts.vdf"):
            try:
                t = max(t, os.path.getmtime(os.path.join(cfg, f)))
            except OSError:
                pass
        out.append((t, n))
    return [n for _, n in sorted(out, reverse=True)]


def running(root=None):
    """Is Steam open? (Then it would overwrite what we write.)"""
    try:
        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as k:
                    if int(winreg.QueryValueEx(k, "pid")[0]) == 0:
                        return False
            except OSError:
                pass
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True,
                                 text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "steam.exe" in out.stdout.lower()
        if sys.platform.startswith("linux"):
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open("/proc/%s/comm" % pid) as f:
                        comm = f.read().strip()
                except OSError:
                    continue
                if comm in ("steam", "steamwebhelper"):
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def compat_tools(root):
    """[(internal name, display name)] of Proton versions Steam knows, newest custom first."""
    out = []
    for vdf in sorted(glob.glob(os.path.join(root, "compatibilitytools.d", "*", "compatibilitytool.vdf")),
                      reverse=True):
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                d = tvdf_load(f.read())
            for name, info in (d.get("compatibilitytools", {}).get("compat_tools", {}) or {}).items():
                out.append((name, (info or {}).get("display_name", name)))
        except (OSError, VdfError):
            continue
    out.append(("proton_experimental", "Proton Experimental"))
    return out


# ---------------------------------------------------------------- running Proton without Steam

def _read_tvdf(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return tvdf_load(f.read())
    except (OSError, VdfError):
        return None


def _key(d, key):
    """d[key], whatever its case (Steam writes some keys in either)."""
    k = _find_key(d, key) if isinstance(d, dict) else None
    return d.get(k) if k else None


def libraries(root):
    """Steam's library folders, this install's own first."""
    out = [root]
    d = _read_tvdf(os.path.join(root, "steamapps", "libraryfolders.vdf")) or {}
    for v in (_key(d, "libraryfolders") or {}).values():
        p = v.get("path") if isinstance(v, dict) else None
        if p and os.path.isdir(os.path.join(p, "steamapps")) and os.path.realpath(p) not in map(os.path.realpath, out):
            out.append(p)
    return out


def app_dir(root, appid):
    """Where an installed Steam app (a Proton, a Steam Linux Runtime) is, from its appmanifest."""
    for lib in libraries(root):
        m = _read_tvdf(os.path.join(lib, "steamapps", "appmanifest_%s.acf" % appid)) or {}
        inst = _key(_key(m, "AppState") or {}, "installdir")
        if inst and os.path.isdir(os.path.join(lib, "steamapps", "common", inst)):
            return os.path.join(lib, "steamapps", "common", inst)
    return None


def shortcut_tool(root, appid):
    """The Proton Steam runs a shortcut with (its internal name, e.g. proton_experimental), or None."""
    d = _read_tvdf(os.path.join(root, "config", "config.vdf")) or {}
    node = d
    for k in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
        node = _key(node, k) or {}
    entry = node.get(str(appid)) if appid is not None else None
    return (entry or {}).get("name") or None


def _version_keys(name):
    """Keys a Valve Proton's name answers to: "Proton 9.0" -> {"9", "90"}, "Proton 5.13" -> {"513"},
    "Proton - Experimental" -> {"experimental"}, as in Steam's proton_9 / proton_513 names."""
    rest = re.sub(r"(?i)^proton\b[\s-]*", "", name).strip().lower()
    m = re.match(r"^(\d+)\.(\d+)", rest)
    if m:
        return {m.group(1) + m.group(2)} | ({m.group(1)} if m.group(2) == "0" else set())
    return {re.sub(r"[^a-z0-9]", "", rest)}


def tools(root):
    """Proton versions installed here: [(internal name or None, display name, folder)], custom
    tools (compatibilitytools.d) first, then Valve's, newest first."""
    custom = []
    for base in (os.path.join(root, "compatibilitytools.d"), "/usr/share/steam/compatibilitytools.d",
                 "/usr/local/share/steam/compatibilitytools.d"):
        for vdf in sorted(glob.glob(os.path.join(base, "*", "compatibilitytool.vdf")), reverse=True):
            d = _read_tvdf(vdf) or {}
            for name, info in ((_key(d, "compatibilitytools") or {}).get("compat_tools") or {}).items():
                path = os.path.normpath(os.path.join(os.path.dirname(vdf), (info or {}).get("install_path", ".")))
                if os.path.isfile(os.path.join(path, "proton")):
                    custom.append((name, (info or {}).get("display_name", name), path))
    valve = []
    for lib in libraries(root):
        for d in glob.glob(os.path.join(lib, "steamapps", "common", "Proton*")):
            if os.path.isfile(os.path.join(d, "proton")):
                valve.append((None, os.path.basename(d), d))

    def newest(t):
        name = t[1].lower()
        m = re.search(r"(\d+)\.(\d+)", name)
        return (2 if "experimental" in name else 1 if m else 0, tuple(map(int, m.groups())) if m else ())
    return custom + sorted(valve, key=newest, reverse=True)


def find_tool(root, name=None):
    """The folder of the Proton called name (Steam's internal name), else the best one here
    (Proton Experimental, else the newest)."""
    have = tools(root)
    if name:
        for n, _, path in have:
            if n == name:
                return path
        want = re.sub(r"(?i)^proton_?", "", name).lower()
        for n, display, path in have:
            if n is None and want in _version_keys(display):
                return path
    valve = [path for n, _, path in have if n is None]
    return (valve or [path for _, _, path in have] or [None])[0]


def _tool_cmd(folder, verb):
    m = _read_tvdf(os.path.join(folder, "toolmanifest.vdf")) or {}
    m = _key(m, "manifest") or {}
    line = (m.get("commandline") or "").replace("%verb%", verb)
    args = shlex.split(line)
    if not args:
        return None, m
    return [os.path.join(folder, args[0].lstrip("/"))] + args[1:], m


def tool_command(root, folder, verb="waitforexitandrun"):
    """What Steam runs for a Proton (toolmanifest.vdf), inside the Steam Linux Runtime it asks for
    when that is installed: ([command up to the Windows program], [tool folders])."""
    cmd, m = _tool_cmd(folder, verb)
    if not cmd:
        cmd = [os.path.join(folder, "proton"), verb]
    folders = [folder]
    need = m.get("require_tool_appid")
    rt = app_dir(root, need) if need else None
    if rt:
        rcmd, _ = _tool_cmd(rt, verb)
        if rcmd:
            cmd = rcmd + cmd
            folders.append(rt)
    return cmd, folders


# ---------------------------------------------------------------- the shortcut

def shortcut_appid(exe, name):
    """The id tools like Steam ROM Manager give a shortcut (unsigned 32-bit)."""
    return (zlib.crc32((exe + name).encode("utf-8")) & 0xFFFFFFFF) | 0x80000000


def _signed(u):
    return u - (1 << 32) if u >= 1 << 31 else u


def _get(entry, key):
    for k, v in entry.items():
        if k.lower() == key.lower():
            return v
    return None


def _backup(path):
    if os.path.exists(path):
        shutil.copy2(path, path + ".umbra-bak")


def add_shortcut(root, user, name, exe, start_dir, icon="", launch_options="", tags=(), compat_tool=None,
                 artwork=None, marker="umbra"):
    """Add (or update) the shortcut; returns its unsigned appid.

    artwork: {"logo": png, "hero": png, "capsule": png, "wide": png} (any subset).
    The entry is found again by its appname or by the marker in its tags."""
    cfg = os.path.join(root, "userdata", user, "config")
    path = os.path.join(cfg, "shortcuts.vdf")
    os.makedirs(cfg, exist_ok=True)
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = bvdf_load(f.read())
    else:
        data = {"shortcuts": {}}
    sc = data.setdefault("shortcuts", {})
    q_exe = '"%s"' % exe
    q_dir = '"%s"' % start_dir
    found = None
    for k, e in sc.items():
        t = _get(e, "tags") or {}
        if (_get(e, "AppName") == name) or (marker and marker in (t.values() if isinstance(t, dict) else [])):
            found = k
            break
    appid = shortcut_appid(q_exe, name)
    if found is not None:
        old = _get(sc[found], "appid")
        if isinstance(old, int) and old:
            appid = old & 0xFFFFFFFF
    entry = {
        "appid": _signed(appid), "AppName": name, "Exe": q_exe, "StartDir": q_dir, "icon": icon or "",
        "ShortcutPath": "", "LaunchOptions": launch_options, "IsHidden": 0, "AllowDesktopConfig": 1,
        "AllowOverlay": 1, "OpenVR": 0, "Devkit": 0, "DevkitGameID": "", "DevkitOverrideAppID": 0,
        "LastPlayTime": 0, "FlatpakAppID": "", "sortas": "",
        "tags": {str(i): t for i, t in enumerate(list(tags) + ([marker] if marker else []))},
    }
    if found is not None:
        prev = sc[found]
        entry["LastPlayTime"] = _get(prev, "LastPlayTime") or 0
        sc[found] = entry
    else:
        idx = 0
        while str(idx) in sc:
            idx += 1
        sc[str(idx)] = entry
    _backup(path)
    with open(path + ".tmp", "wb") as f:
        f.write(bvdf_dump(data))
    os.replace(path + ".tmp", path)

    if artwork:
        grid = os.path.join(cfg, "grid")
        os.makedirs(grid, exist_ok=True)
        names = {"capsule": "%dp", "wide": "%d", "hero": "%d_hero", "logo": "%d_logo"}
        for kind, src in artwork.items():
            if src and os.path.isfile(src) and kind in names:
                shutil.copy2(src, os.path.join(grid, (names[kind] % appid) + os.path.splitext(src)[1]))
    if compat_tool and os.name != "nt":
        set_compat_tool(root, appid, compat_tool)
    return appid


def set_compat_tool(root, appid, tool):
    path = os.path.join(root, "config", "config.vdf")
    if not os.path.exists(path):
        raise VdfError("Steam has no config/config.vdf yet (start Steam once)")
    with open(path, encoding="utf-8") as f:
        d = tvdf_load(f.read())
    node = d
    for k in ("InstallConfigStore", "Software", "Valve", "Steam"):
        node = node.setdefault(_find_key(node, k) or k, {})
    ctm = node.setdefault(_find_key(node, "CompatToolMapping") or "CompatToolMapping", {})
    ctm[str(appid)] = {"name": tool, "config": "", "priority": "250"}
    _backup(path)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(tvdf_dump(d))
    os.replace(path + ".tmp", path)


def _find_key(d, key):
    for k in d:
        if k.lower() == key.lower():
            return k
    return None


def remove_shortcut(root, user, name, marker="umbra"):
    path = os.path.join(root, "userdata", user, "config", "shortcuts.vdf")
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        data = bvdf_load(f.read())
    sc = data.get("shortcuts", {})
    keep = [e for e in sc.values()
            if not (_get(e, "AppName") == name or marker in ((_get(e, "tags") or {}).values()))]
    if len(keep) == len(sc):
        return False
    data["shortcuts"] = {str(i): e for i, e in enumerate(keep)}
    _backup(path)
    with open(path + ".tmp", "wb") as f:
        f.write(bvdf_dump(data))
    os.replace(path + ".tmp", path)
    return True


def rungame_url(appid):
    """steam://rungameid/... for a shortcut (64-bit game id)."""
    return "steam://rungameid/%d" % ((appid << 32) | 0x02000000)
