"""
The Umbra launcher: what Steam's "Umbra" opens. Play, the season and other
picks, updates and repair, Mod Organizer 2 for those who want it, credits,
and the log folders. This module is its logic; launcher_gui.py is its window.

The launcher is Umbra Setup itself, copied into the install's .umbra folder
as install.LAUNCHER (the name makes it open as the launcher). Changing a pick,
updating and repairing run Setup's own install steps, in this process.

How it starts Mod Organizer 2:

  Windows  Steam (or the Start menu / desktop shortcut) starts the launcher; the
           launcher starts ModOrganizer.exe.
  Linux    The launcher runs natively (Setup's steps need the real system), so
           MO2 runs through Proton. From Steam, the shortcut's launch options wrap
           Steam's own Proton command:
               "<install>/.umbra/umbra-launcher" -- %command%
           The launcher gets that whole command after "--", adds MO2's arguments,
           and keeps a copy (steam-launch.json: the command without the Steam
           client's own wrappers, and the STEAM_COMPAT_* settings Steam gave Proton).
           From the applications menu or the desktop (no Steam), it runs that copy,
           or, when Steam never started it, the same command made from Steam's
           files: the Proton the shortcut uses (config.vdf), the Steam Linux Runtime
           that Proton asks for (toolmanifest.vdf), and the shortcut's prefix. Steam
           itself needn't be open.

Before Play (and Open MO2), the launcher checks the game's runtime (runtime.py): on
Linux the Proton prefix Steam made for the shortcut (STEAM_COMPAT_DATA_PATH), which
it sets up through Steam's own Proton command the first time; on Windows the
Visual C++ runtime and the D3DX DLLs.

MO2's arguments: -p "<profile>" run -e "<Run-list entry>" starts the game
through MO2's virtual folder without MO2's window (MO2 exits with the game);
-p "<profile>" alone opens MO2.
"""
import json
import os
import re
import subprocess
import sys

from . import fetch, install, layers, mo2, packdef, runtime, steam

SETTINGS = "launcher.json"
STEAM_LAUNCH = "steam-launch.json"
# Steam client programs at the front of Steam's command: they talk to a running Steam
CLIENT_WRAPPERS = ("reaper", "steam-launch-wrapper", "steam-runtime-launch-client")
# what Steam tells Proton and its container (paths to mount, the prefix, the Steam folder)
STEAM_ENV = re.compile(r"^(STEAM_COMPAT_\w+|PRESSURE_VESSEL_\w+)$")
VERB = "waitforexitandrun"
ENGINE = re.compile(r"^anomalydx(\d+)(avx)?\.exe$", re.I)


def split_argv(argv):
    """(the launcher's own arguments, Steam's command after "--" or None)."""
    argv = list(argv)
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], (argv[i + 1:] or None)
    return argv, None


def mo2_index(cmd):
    """Where ModOrganizer.exe is in Steam's command (the last match), or None."""
    for i in range(len(cmd) - 1, -1, -1):
        if cmd[i].replace("\\", "/").rsplit("/", 1)[-1].lower() == "modorganizer.exe":
            return i
    return None


NO_PROTON = ("Umbra can't find Proton. Install Proton Experimental in Steam (Library > Tools), or start "
             "Umbra from your Steam library once.")


def without_client(cmd):
    """Steam's command without the Steam client's wrappers in front (reaper, steam-launch-wrapper)."""
    cmd = list(cmd)
    while cmd and os.path.basename(cmd[0]) in CLIENT_WRAPPERS and "--" in cmd:
        cmd = cmd[cmd.index("--") + 1:]
    return cmd


class ProtonRun:
    """How MO2 runs through Proton here: cmd (up to the Windows program), env (added to the
    launcher's own), compat (the prefix folder) and how ("steam", "saved" or "made")."""

    def __init__(self, cmd, env, compat, how):
        self.cmd, self.env, self.compat, self.how = list(cmd), dict(env), compat, how


def mo2_args(profile, entry=None):
    return ["-p", profile] + (["run", "-e", entry] if entry else [])


APPIMAGE_VARS = ("APPIMAGE", "APPDIR", "ARGV0", "OWD")   # what an AppImage tells the program inside it


def child_env(host=False):
    """The environment for what the launcher starts. The program's own library
    path (a built program unpacks its libraries and points LD_LIBRARY_PATH at
    them) goes back to what it was. host: for the desktop's programs (opening a
    folder), also without what Steam sets for the game (its runtime's libraries
    and the overlay)."""
    e = dict(os.environ)
    if getattr(sys, "frozen", False):
        for k in [k for k in e if k.startswith("_PYI_") or k == "_MEIPASS2"]:
            del e[k]                     # PyInstaller's own: another built program would think it's ours
        for k in APPIMAGE_VARS:          # our AppImage's (Setup's), for the same reason
            e.pop(k, None)
        orig = e.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            e["LD_LIBRARY_PATH"] = orig
        elif e.get("LD_LIBRARY_PATH"):
            mei = getattr(sys, "_MEIPASS", None)
            keep = [p for p in e["LD_LIBRARY_PATH"].split(os.pathsep) if p and p != mei]
            if keep:
                e["LD_LIBRARY_PATH"] = os.pathsep.join(keep)
            else:
                e.pop("LD_LIBRARY_PATH")
    if host:
        e.pop("LD_PRELOAD", None)
        if "SYSTEM_LD_LIBRARY_PATH" in e:
            e["LD_LIBRARY_PATH"] = e["SYSTEM_LD_LIBRARY_PATH"]
            if not e["LD_LIBRARY_PATH"]:
                e.pop("LD_LIBRARY_PATH")
        if "SYSTEM_PATH" in e:
            e["PATH"] = e["SYSTEM_PATH"]
    return e


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def names(names, most=3):
    """"A", "A and B", "A, B and C", "A, B, C and 2 more"."""
    names = list(names)
    if len(names) > most:
        names = names[:most] + ["%d more" % (len(names) - most)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def default_work():
    """The built launcher lives in the install's .umbra folder."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return None


class Install:
    """An Umbra install, as Setup left it in its work folder (.umbra)."""

    def __init__(self, work, source=None):
        self.work = os.path.abspath(work) if work else None
        self.fallback_source = source
        self.reload()

    def reload(self):
        w = self.work
        self.state = (_read_json(os.path.join(w, "state.json")) if w else None) or {}
        self.pack = _read_json(os.path.join(w, "pack.json")) if w else None
        st = self.state
        self.version = st.get("pack")
        self.source = st.get("source") or self.fallback_source
        self.channel = st.get("channel") or "stable"
        self.mode = st.get("mode") or "existing"
        self.mo2 = st.get("mo2")
        self.anomaly = st.get("anomaly") or (mo2.game_path(self.mo2) if self.mo2 else None)
        self._proton = {}
        names = st.get("profiles") or []
        self.profile = names[0] if names else ((self.pack or {}).get("profile", {}).get("name") or "Umbra")

    @property
    def found(self):
        return bool(self.mo2) and os.path.isfile(os.path.join(self.mo2, "ModOrganizer.exe"))

    @property
    def dev(self):
        """Umbra's development install (state "dev"): no updates, repair or option changes here."""
        return bool(self.state.get("dev"))

    def save_pack(self, pack):
        """The manifest of the installed version, when the launcher had to fetch it."""
        self.pack = pack
        install.Installer._save_json(os.path.join(self.work, "pack.json"), pack)

    # ------------------------------------------------------------ picks
    def picks(self):
        """The picks installed now (whole, for this pack)."""
        if not self.pack:
            return set()
        return packdef.normalize_choices(self.pack, self.state.get("choices"))

    def download_for(self, choices):
        """What these picks would download: the volumes of chosen layers that are
        neither in place nor in the download cache."""
        if not self.pack or not self.mo2:
            return 0
        cache = os.path.join(self.work, "cache")
        mods_dir = os.path.join(self.mo2, "mods")
        pending = []
        for L in packdef.chosen_layers(self.pack, choices):
            if L.get("target", "mods") == "anomaly":
                m = layers.anomaly_marker(self.anomaly, L["id"]) if self.anomaly else None
                if m and m.get("volumes") == layers.volume_keys(L):
                    continue
            else:
                _, m = layers.installed(mods_dir, L, packdef.other_ids(self.pack, L))
                if m and m.get("volumes") == layers.volume_keys(L):
                    continue
            pending.append(L)
        return sum(v["size"] for v in packdef.unique_volumes(pending)
                   if not os.path.exists(os.path.join(cache, *v["path"].split("/")) + ".ok"))

    def options(self, **kw):
        """install.Options for changing this install (never Steam: it is open, since it started us)."""
        if self.mode == "new":
            o = dict(mode="new", root=os.path.dirname(self.work), gamma_cache=self.state.get("cache"))
        else:
            o = dict(mode="existing", mo2_dir=self.mo2)
        o.update(source=self.source, channel=self.channel, steam=False, shortcuts=None)
        o.update(kw)
        return install.Options(**o)

    # ------------------------------------------------------------ the launcher's own settings
    def settings(self):
        return (_read_json(os.path.join(self.work, SETTINGS)) if self.work else None) or {}

    def save_settings(self, **kw):
        s = self.settings()
        s.update(kw)
        install.Installer._save_json(os.path.join(self.work, SETTINGS), s)

    # ------------------------------------------------------------ playing
    def engines(self, avx="detect"):
        """The Run-list entries that start Anomaly's engine and whose exe is there,
        best first: [(title, exe name)]. DirectX 11 first; the AVX builds first when
        the CPU has AVX, left out when it lacks it (and after the others when that
        can't be told). avx: True/False/None (unknown); found out when not given."""
        if avx == "detect":
            avx = install.cpu_has("avx")
        out = []
        for e in mo2.read_executables(self.mo2) if self.mo2 else []:
            name = (e.get("binary") or "").replace("\\", "/").rsplit("/", 1)[-1]
            m = ENGINE.match(name)
            if not m or not self.anomaly or not layers.find_ci(self.anomaly, "bin/" + name):
                continue
            is_avx = bool(m.group(2))
            if is_avx and avx is False:
                continue
            dx = int(m.group(1))
            out.append(((-dx, 0 if is_avx == (avx is True) else 1), e.get("title") or name, name))
        out.sort(key=lambda x: x[0])
        return [(t, n) for _, t, n in out]

    def engine(self, engines):
        """The entry Play starts: the one picked last time if it's still there, else the best."""
        titles = [t for t, _ in engines]
        last = self.settings().get("engine")
        return last if last in titles else (titles[0] if titles else None)

    def command(self, steam_cmd, entry=None):
        """The command that starts MO2 with these arguments, or None when it can't be
        made here (Linux with no Proton found)."""
        args = mo2_args(self.profile, entry)
        if os.name == "nt":
            return [os.path.join(self.mo2, "ModOrganizer.exe")] + args
        i = mo2_index(steam_cmd or [])
        if i is not None:
            return list(steam_cmd[:i + 1]) + args
        run = self.proton(None)
        return run.cmd + [os.path.join(self.mo2, "ModOrganizer.exe")] + args if run else None

    def env(self, steam_cmd):
        """The environment for what command() makes."""
        e = child_env()
        run = self.proton(steam_cmd) if os.name != "nt" else None
        if run:
            e.update(run.env)
        return e

    # ------------------------------------------------------------ Proton (Linux)
    def remember_steam(self, steam_cmd):
        """Started from Steam: keep its Proton command and what it set for Proton, so a start
        from the applications menu runs the game the same way."""
        i = mo2_index(steam_cmd or [])
        if os.name == "nt" or i is None or not self.work:
            return
        rec = {"command": without_client(steam_cmd[:i]),
               "env": {k: v for k, v in sorted(os.environ.items()) if STEAM_ENV.match(k)}}
        path = os.path.join(self.work, STEAM_LAUNCH)
        if _read_json(path) != rec:
            install.Installer._save_json(path, rec)
        self._proton.pop(None, None)

    def proton(self, steam_cmd):
        """ProtonRun for MO2 here (Linux), or None when no Proton can be found."""
        key = tuple(steam_cmd) if steam_cmd else None
        if key not in self._proton:
            self._proton[key] = self._find_proton(steam_cmd)
        return self._proton[key]

    def _find_proton(self, steam_cmd):
        if os.name == "nt":
            return None
        i = mo2_index(steam_cmd or [])
        if i is not None:                                  # Steam's own; its settings are in our environment
            return ProtonRun(steam_cmd[:i], {}, os.environ.get("STEAM_COMPAT_DATA_PATH"), "steam")
        rec = _read_json(os.path.join(self.work, STEAM_LAUNCH)) if self.work else None
        cmd = (rec or {}).get("command") or []
        if cmd and all(os.path.exists(a) for a in cmd if a.startswith("/")):
            env = dict(rec.get("env") or {})
            env.setdefault("STEAM_COMPAT_INSTALL_PATH", self.mo2)
            return ProtonRun(cmd, env, env.get("STEAM_COMPAT_DATA_PATH"), "saved")
        return self._proton_from_steam()

    def _proton_from_steam(self):
        """The command Steam would run, made from Steam's own files."""
        roots = steam.steam_roots()
        appid = self.state.get("steam_appid")
        if not roots or not self.mo2:
            return None
        root = next((r for r in roots if appid and os.path.isdir(
            os.path.join(r, "steamapps", "compatdata", str(appid)))), roots[0])
        tool = steam.find_tool(root, steam.shortcut_tool(root, appid) or self.state.get("compat_tool"))
        if not tool:
            return None
        cmd, folders = steam.tool_command(root, tool, VERB)
        # the shortcut's prefix, shared with starts from Steam; without a shortcut, Umbra's own
        compat = (os.path.join(root, "steamapps", "compatdata", str(appid)) if appid
                  else os.path.join(self.work, "proton"))
        mounts = [p for p in (self.mo2, self.anomaly, self.work) if p]
        env = {"STEAM_COMPAT_DATA_PATH": compat, "STEAM_COMPAT_CLIENT_INSTALL_PATH": root,
               "STEAM_COMPAT_INSTALL_PATH": self.mo2, "STEAM_COMPAT_TOOL_PATHS": ":".join(folders),
               "STEAM_COMPAT_LIBRARY_PATHS": ":".join(os.path.join(lib, "steamapps") for lib in steam.libraries(root)),
               "STEAM_COMPAT_MOUNTS": ":".join(mounts),
               "STEAM_COMPAT_APP_ID": "0", "SteamAppId": "0", "SteamGameId": "0"}
        return ProtonRun(cmd, env, compat, "made")

    def anomaly_changed(self):
        """Umbra's files in the Anomaly folder (xrRazom's engine) that something replaced since
        (a G.A.M.M.A. install or update puts its own engine back): {layer id: [files]}."""
        if not self.pack or not self.anomaly:
            return {}
        out = {}
        for L in packdef.chosen_layers(self.pack, self.picks()):
            if L.get("target", "mods") == "anomaly":
                ch = layers.anomaly_layer_changed(self.anomaly, L)
                if ch:
                    out[L["id"]] = ch
        return out

    def anomaly_restore(self, log=None, progress=None, cancel=None):
        """Put Umbra's files back in the Anomaly folder (what anomaly_changed lists) from the
        download cache, as Setup installed them. Just those layers: a repair would also run
        G.A.M.M.A.'s own install again, which is long and is what replaced them. Raises
        LayerError when a download is no longer in the cache (Check for updates > Repair)."""
        log = log or (lambda msg, level="info": None)
        toks = layers.tokens(self.mo2, self.anomaly, install.zgo_cache_dir(), under_wine=(os.name != "nt"))
        done = []
        for L in packdef.chosen_layers(self.pack, self.picks()):
            if L.get("target", "mods") != "anomaly" or not layers.anomaly_layer_changed(self.anomaly, L):
                continue
            vols = [os.path.join(self.cache, *v["path"].split("/")) for v in L["volumes"]]
            if not all(os.path.isfile(p) and os.path.getsize(p) == v["size"] and os.path.exists(p + ".ok")
                       for p, v in zip(vols, L["volumes"])):
                raise layers.LayerError("Umbra's download of %s is not in its cache any more. Check for "
                                        "updates > Repair gets it again." % L["id"])
            try:
                layers.install_anomaly_layer(self.anomaly, L, vols, toks, progress, cancel,
                                             self.pack.get("version", ""), new_only=bool(L.get("profile")), log=log)
            except KeyboardInterrupt:             # the unpacker's way of stopping when asked
                raise fetch.Cancelled() from None
            log("Put back %s" % L["id"])
            done.append(L["id"])
        return done

    # ------------------------------------------------------------ the game's runtime
    @property
    def cache(self):
        return os.path.join(self.work, "cache")

    def compat_dir(self, steam_cmd=None):
        """The Proton prefix folder MO2 runs in (Linux), or None."""
        run = self.proton(steam_cmd)
        return run.compat if run else None

    def runtime_missing(self, steam_cmd=None):
        """What the game still needs from Microsoft, in words ([] when ready, or when
        it can't be told here)."""
        if os.name == "nt":
            return runtime.windows_missing(self.anomaly) if self.anomaly else []
        compat = self.compat_dir(steam_cmd)
        return runtime.prefix_missing(compat) if compat else []

    def runtime_fix(self, steam_cmd, log=None, progress=None, cancel=None):
        """Put it in place: Windows asks for permission for the Visual C++ installer; on
        Linux the DLLs go into Steam's prefix, through Steam's own Proton command
        (its output goes to logs/proton-setup.log)."""
        if os.name == "nt":
            runtime.windows_install(self.cache, self.anomaly, log, progress, cancel)
            return
        run = self.proton(steam_cmd)
        if not run or not run.compat:
            raise runtime.RuntimeProblem(NO_PROTON)
        os.makedirs(run.compat, exist_ok=True)
        env = self.env(steam_cmd)
        runtime.prepare(self.cache, "prefix", log, progress, cancel)
        logs = os.path.join(self.work, "logs")
        os.makedirs(logs, exist_ok=True)

        def run_in_prefix(args):
            with open(os.path.join(logs, "proton-setup.log"), "a", encoding="utf-8") as f:
                f.write("\n$ %s\n" % " ".join(args))
                f.flush()
                return subprocess.run(run.cmd + list(args), cwd=self.mo2, env=env,
                                      stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                      timeout=900).returncode
        runtime.prefix_install(run.compat, self.cache, run_in_prefix, log)

    def folders(self):
        """[(label, path)] for the Folders menu."""
        out = []
        if self.anomaly:
            out.append(("Game logs (for bug reports)", os.path.join(self.anomaly, "appdata", "logs")))
        if self.work:
            out.append(("Umbra's logs (Setup, Proton setup)", os.path.join(self.work, "logs")))
        if self.mo2:
            out.append(("Mod Organizer 2 folder", self.mo2))
        if self.anomaly:
            out.append(("Anomaly folder", self.anomaly))
        return out
