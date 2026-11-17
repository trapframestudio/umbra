"""
The install, step by step, for the window and the command line alike.

    plan = Installer(opts, ...).plan()      what will happen, sizes, problems
    Installer(opts, ...).run()              do it (safe to run again: finished
                                            steps are skipped, an update replaces
                                            only changed layers)

Two ways in:
  new       an empty folder: G.A.M.M.A. is installed there by stalker-gamma-cli
            (<folder>/anomaly, <folder>/gamma = the MO2 instance, <folder>/cache)
  existing  a G.A.M.M.A. MO2 folder the player already has: only Umbra is added
"""
import errno
import filecmp
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile

from . import VERSION, fetch, gammacli, layers, mo2, packdef, prebake, runtime, shortcuts, steam, userltx

GAMMA_BYTES = 150 * 10 ** 9          # stalker-gamma-cli: "This will take ~150GB"
STEPS = [("pack", "Reading the pack", 1), ("origins", "Getting mods from their authors' own pages", 2),
         ("gamma", "Installing G.A.M.M.A.", 60), ("download", "Downloading Umbra", 25),
         ("layers", "Unpacking Umbra", 10), ("profile", "Setting up the Umbra profiles", 1),
         ("bake", "Body models for Umbra Gore & Effects", 15),
         ("settings", "Applying Umbra's settings", 1),
         ("runtime", "Getting what the game needs from Microsoft (Visual C++, DirectX)", 2),
         ("launcher", "Installing the Umbra launcher", 1),
         ("shortcuts", "Adding Umbra to the %s and the desktop" % ("Start menu" if os.name == "nt" else
                                                                  "applications menu"), 1),
         ("steam", "Adding the Steam shortcuts", 1)]
LOGS_KEPT = 20
RUNTIME_KIND = "windows" if os.name == "nt" else "prefix"      # runtime.py
# An install whose state.json says "dev": true is the one Umbra's pack is built from: Setup and the
# launcher never update, repair or change it (only Play and Mod Organizer 2 work there)
DEV_INSTALL = ("This is Umbra's development install: the pack is built from it, so Setup doesn't update, "
               "repair or change it. Change its mods by hand (Umbra's own: dev/deploy_mods.py).")

# The launcher is this same program under another name (launcher.py says how Steam starts it)
LAUNCHER = "Umbra Launcher.exe" if os.name == "nt" else "umbra-launcher"


def took(secs):
    """How long something took, for the log: "8 seconds", "1 minute", "12 minutes"."""
    if secs < 59.5:
        n, unit = max(1, int(round(secs))), "second"
    else:
        n, unit = int(round(secs / 60)), "minute"
    return "%d %s%s" % (n, unit, "" if n == 1 else "s")


def cpu_has(flag):
    """Does the CPU have "avx" / "avx2"? True/False, or None when it can't be told."""
    try:
        if os.name == "nt":
            import ctypes
            pf = {"avx": 39, "avx2": 40}[flag]        # PF_AVX(2)_INSTRUCTIONS_AVAILABLE
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(pf))
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("flags"):
                    return flag in line.split()
    except Exception:  # noqa: BLE001
        pass
    return None


def cpu_has_avx2():
    return cpu_has("avx2")


def program_busy(path):
    """Whether the program at path is running: neither Linux ("text file busy") nor
    Windows (a sharing violation) opens a running program for writing."""
    try:
        with open(path, "r+b"):
            return False
    except OSError as e:
        return e.errno == errno.ETXTBSY or (os.name == "nt" and isinstance(e, PermissionError))


def launcher_busy(work):
    """The installed launcher is open, and this program would replace it. A running
    built program reads its own file as it goes, so it would break."""
    if not getattr(sys, "frozen", False):
        return False
    dst = os.path.join(work, LAUNCHER)
    src = os.path.abspath(sys.executable)
    if os.path.normcase(src) == os.path.normcase(os.path.abspath(dst)) or not os.path.isfile(dst):
        return False
    return program_busy(dst) and not filecmp.cmp(src, dst, shallow=False)


def install_launcher(work, log=None):
    """Put the launcher in the work folder: a copy of this program (it opens as the
    launcher under that name). Run from source on Linux, a script that runs this
    source as the launcher. Returns its path, or None (a source run on Windows)."""
    log = log or (lambda msg, level="info": None)
    dst = os.path.join(work, LAUNCHER)
    if getattr(sys, "frozen", False):
        src = os.path.abspath(sys.executable)
        if os.path.normcase(src) == os.path.normcase(os.path.abspath(dst)):
            return dst                                  # the launcher is updating its own install
        if os.path.isfile(dst) and filecmp.cmp(src, dst, shallow=False):
            return dst
        if launcher_busy(work):                         # opened since the plan said it was closed
            log("The Umbra launcher is open, so it was not updated. Close it and run Umbra Setup again "
                "to update it.", "warn")
            return dst
        shutil.copy2(src, dst + ".new")
        if os.name == "nt" and os.path.exists(dst):
            # a running exe can't be replaced, but it can be renamed out of the way
            try:
                if os.path.exists(dst + ".old"):
                    os.remove(dst + ".old")
                os.replace(dst, dst + ".old")
            except OSError:
                pass
        os.replace(dst + ".new", dst)
    elif os.name == "nt":
        log("Run from source, Umbra Setup can't make the launcher on Windows; Steam starts Mod Organizer 2.", "warn")
        return None
    else:
        setup = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "umbra_setup.py")
        with open(dst, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n# The Umbra launcher (Umbra Setup run from its source)\nexec %s %s --launcher "
                    "--work %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(setup), shlex.quote(work)))
    if os.name != "nt":
        os.chmod(dst, 0o755)
    return dst


def open_log(work):
    """A new log file in <work>/logs (the newest LOGS_KEPT are kept)."""
    d = os.path.join(work, "logs")
    os.makedirs(d, exist_ok=True)
    old = sorted(f for f in os.listdir(d) if f.startswith("setup-") and f.endswith(".log"))
    for f in old[:max(0, len(old) - LOGS_KEPT + 1)]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    base = time.strftime("setup-%Y%m%d-%H%M%S")
    name, n = base + ".log", 1
    while os.path.exists(os.path.join(d, name)):        # two runs in one second
        n += 1
        name = "%s-%d.log" % (base, n)
    return open(os.path.join(d, name), "w", encoding="utf-8")


class Options:
    def __init__(self, mode="new", root=None, mo2_dir=None, source=None, channel="stable", steam=True,
                 compat_tool=None, steam_user=None, threads=4, mirror=True, gamma_cache=None, choices=None,
                 profile_shortcuts=True, repair=False, shortcuts=True, bake=True):
        self.mode = mode                  # "new" | "existing"
        self.root = root                  # new: the install folder
        self.mo2_dir = mo2_dir            # existing: the G.A.M.M.A. MO2 folder
        self.source = source              # base URL or folder of the published pack
        self.channel = channel
        self.steam = steam
        self.compat_tool = compat_tool    # Linux: Proton to run Umbra with (None: proton_experimental)
        self.steam_user = steam_user
        self.threads = threads
        self.mirror = mirror
        self.gamma_cache = gamma_cache    # new: reuse a stalker-gamma-cli cache folder
        self.choices = choices            # the player's picks (packdef "Choices"); None: what this install
                                          # had last time, else the pack's defaults
        self.profile_shortcuts = profile_shortcuts   # a Steam shortcut for each extra profile picked
        self.repair = repair              # also reinstall a mod whose folder lost files
        self.bake = bake                  # Umbra Gore & Effects' body models: prebaked or baked here
        self.shortcuts = shortcuts        # the Start menu / applications menu and the desktop (start
                                          # without Steam): True make them, False leave them alone,
                                          # None (the launcher's own runs) make them only for an
                                          # install that never had them, else refresh those still there


class Plan:
    def __init__(self):
        self.pack = None
        self.channel = None
        self.mo2 = self.anomaly = self.work = None
        self.need_bytes = 0
        self.download_bytes = 0     # still to download (not in the cache)
        self.free_bytes = 0
        self.choices = None     # the picks this install uses (normalized)
        self.saved = None       # the picks the last install here used (None: no Umbra install here yet)
        self.changed = []       # Umbra mods installed here that this pack has in another form (their names)
        self.problems = []      # stop the install
        self.warnings = []      # worth knowing
        self.steps = []

    def ok(self):
        return not self.problems


def gamma_problems(mo2_dir, anomaly, base_profile, log=None):
    """What a G.A.M.M.A. install lacks ([] when nothing): Anomaly's own files,
    and the folders of the mods its MO2 profile switches on. A few missing
    folders are only logged: G.A.M.M.A.'s own list can name a mod wrongly (its
    pinned list has "DarkasleQif's" for "Darkasleif's"), and MO2 skips those.
    A failed install misses far more (one that skipped extracting: Anomaly and
    257 of 607 mods)."""
    out = []
    lost = [f for f in ("AnomalyLauncher.exe", "fsgame.ltx") if not os.path.isfile(os.path.join(anomaly, f))]
    if not any(fn.lower().endswith(".db0") for _, _, fns in os.walk(os.path.join(anomaly, "db")) for fn in fns):
        lost.append("db/*.db0")
    if lost:
        out.append("Anomaly itself is missing from %s (no %s)" % (anomaly, ", ".join(lost)))
    try:
        with open(os.path.join(mo2_dir, "profiles", base_profile, "modlist.txt"), encoding="utf-8-sig") as f:
            on = [l.strip()[1:] for l in f if l.startswith("+")]
    except OSError:
        return out + ['there is no "%s" profile in %s' % (base_profile, mo2_dir)]
    missing = [m for m in on if not os.path.isdir(os.path.join(mo2_dir, "mods", m))]
    if missing and len(missing) <= max(5, len(on) // 100):
        if log:
            log("G.A.M.M.A.'s list names %d mod%s that %s not there (%s); Mod Organizer 2 skips %s."
                % (len(missing), "" if len(missing) == 1 else "s", "is" if len(missing) == 1 else "are",
                   ", ".join(missing), "it" if len(missing) == 1 else "them"))
    elif missing:
        out.append("%d of the %d mods G.A.M.M.A. switches on are not there (%s%s)"
                   % (len(missing), len(on), ", ".join(missing[:3]), ", ..." if len(missing) > 3 else ""))
    return out


def default_root():
    if os.name == "nt":
        return (os.environ.get("SystemDrive") or "C:") + r"\Games\Umbra"
    return os.path.join(os.path.expanduser("~"), "Games", "Umbra")


def zgo_cache_dir():
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], "zgo_baker")
    return os.path.expanduser("~/.cache/zgo_baker")


def asset_path(name):
    """Files shipped inside the program (the logo, the icon)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(base, "assets"), os.path.join(base, "umbra_installer", "assets")):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


class Installer:
    def __init__(self, opts, log=None, progress=None, cancel=None, wait_for_download=None):
        self.o = opts
        self.log = log or (lambda msg, level="info": None)
        self.progress = progress or (lambda step, frac, msg="": None)
        self.cancel_event = cancel or threading.Event()
        # an author's site wouldn't give its file to Setup (fetch.OriginNeeded): a window hands it to the
        # player's browser and returns once the file is in their Downloads folder; None: the error stops Setup
        self.wait_for_download = wait_for_download

    def cancelled(self):
        return self.cancel_event.is_set()

    def _check_cancel(self):
        if self.cancelled():
            raise fetch.Cancelled()

    # ------------------------------------------------------------ paths
    def paths(self):
        if self.o.mode == "new":
            root = os.path.abspath(self.o.root or default_root())
            return {"root": root, "mo2": os.path.join(root, "gamma"), "anomaly": os.path.join(root, "anomaly"),
                    "cache": self.o.gamma_cache or os.path.join(root, "cache"), "work": os.path.join(root, ".umbra")}
        m = os.path.abspath(self.o.mo2_dir or "")
        return {"root": os.path.dirname(m), "mo2": m, "anomaly": mo2.game_path(m), "cache": None,
                "work": os.path.join(m, ".umbra")}

    # ------------------------------------------------------------ the plan
    def plan(self, pack=None):
        """What will happen. pack: an already loaded pack manifest (else it is fetched)."""
        p = Plan()
        P = self.paths()
        p.mo2, p.anomaly, p.work = P["mo2"], P["anomaly"], P["work"]
        if self._load_state(P).get("dev"):
            # the install Umbra's pack is built from: an update, repair or change of picks would put
            # the pack's copies over the work being tested there (README, "A development install")
            p.problems.append(DEV_INSTALL)
            return p
        if not self.o.source:
            p.problems.append("This installer has no download address built in; start it with --source URL.")
            return p
        if pack is not None:
            p.pack = pack
        else:
            try:
                p.channel, p.pack = fetch.load_pack(self.o.source, self.o.channel)
            except (fetch.FetchError, packdef.PackError) as e:
                p.problems.append("Could not read the Umbra pack: %s" % e)
                return p
        if self.o.mode == "existing":
            if not mo2.is_instance(P["mo2"]):
                p.problems.append("%s is not a Mod Organizer 2 folder (no ModOrganizer.exe and profiles)." % P["mo2"])
            elif p.pack and p.pack["profile"]["base"] not in mo2.profiles(P["mo2"]):
                p.problems.append('That MO2 has no "%s" profile, so it is not a G.A.M.M.A. install.'
                                  % p.pack["profile"]["base"])
            if not P["anomaly"]:
                p.problems.append("Could not find the Anomaly folder this MO2 uses (gamePath in ModOrganizer.ini).")
        else:
            r = P["root"]
            if os.path.exists(r) and os.listdir(r) and not os.path.isdir(P["work"]):
                p.problems.append("%s is not empty. Choose an empty folder (or one Umbra installed into)." % r)
            if any(c in r for c in "!#&'") and os.name == "nt":
                p.warnings.append("Paths with special characters can upset some mods; a plain folder is safer.")
        if p.pack:
            p.saved = self._load_state(P).get("choices")
            p.choices = packdef.normalize_choices(p.pack, self.o.choices if self.o.choices is not None else p.saved)
            dl = packdef.total_download(p.pack, p.choices)
            have = 0
            cache = os.path.join(P["work"], "cache")
            for v in packdef.unique_volumes(packdef.chosen_layers(p.pack, p.choices)):
                f = os.path.join(cache, *v["path"].split("/"))
                if os.path.exists(f + ".ok"):
                    have += v["size"]
            rt = runtime.download_bytes(os.path.join(P["work"], "cache"), RUNTIME_KIND)
            vc = runtime.vc_installed() if os.name == "nt" else None
            if os.name == "nt" and (not vc or tuple(vc[:2]) < runtime.VC_MIN):
                rt += runtime.DOWNLOADS["vc-x64"]["size"]
                p.warnings.append("Windows will ask for permission to install Microsoft's Visual C++ runtime, "
                                  "which the game needs.")
            for v in packdef.origins(p.pack, p.choices):
                if not os.path.exists(os.path.join(cache, *v["path"].split("/")) + ".ok"):
                    og = v["origin"]
                    p.warnings.append('"%s" (%s) comes from %s, as its author asks: Setup downloads it there, or uses '
                                      'the copy in your Downloads folder if you have it.'
                                      % (og["name"], packdef.human(v["size"]), og.get("site") or og["page"]))
            p.changed = self._changed(P, p.pack, p.choices)
            p.download_bytes = dl - have + rt
            p.need_bytes = (dl - have) + 2 * rt + self._unpack_bytes(P, p.pack, p.choices) + max(
                v["bytes"] for L in p.pack["layers"] for v in L["volumes"])
            if self.o.mode == "new" and not os.path.exists(os.path.join(P["mo2"], "ModOrganizer.exe")):
                p.need_bytes += GAMMA_BYTES
            p.need_bytes += self._bake_bytes(P, p.pack, p.choices, p.warnings)
            target = P["root"] if self.o.mode == "new" else P["mo2"]
            try:
                p.free_bytes = fetch.free_bytes(target)
                if p.free_bytes < p.need_bytes:
                    p.problems.append("Not enough free space: about %s needed, %s free on that drive."
                                      % (packdef.human(p.need_bytes), packdef.human(p.free_bytes)))
            except OSError:
                pass
        if p.pack:
            p.warnings += self._profile_warnings(p.pack, p.choices)
        if mo2.running():
            p.problems.append("Mod Organizer 2 is open. Close it first (it rewrites its files when it closes).")
        if launcher_busy(P["work"]):
            p.problems.append("The Umbra launcher is open. Close it first (Setup updates it).")
        if os.name != "nt":
            # Linux: the game runs through Steam's Proton, which only Umbra's Steam shortcut gets
            roots = steam.steam_roots()
            if self.o.steam and (not roots or not steam.users(roots[0])):
                p.problems.append("On Linux, Umbra runs the game through Steam's Proton, and %s. Install Steam, "
                                  "sign in once, then run Umbra Setup again."
                                  % ("Steam isn't installed" if not roots else "nobody has signed in to Steam "
                                     "here yet"))
            elif not roots:
                p.warnings.append("Umbra runs the game through Proton, which comes with Steam, and Steam isn't "
                                  "installed here. Install Steam and Proton Experimental (Library > Tools) "
                                  "before you play.")
        if self.o.steam and steam.running():
            p.warnings.append("Steam is open. Close Steam before the last step, or the shortcut can't be added "
                              "(the installer waits for it).")
        p.steps = [s for s in STEPS if not (s[0] == "gamma" and self.o.mode == "existing")
                   and not (s[0] == "steam" and not self.o.steam)
                   and not (s[0] == "shortcuts" and self.o.shortcuts is False)
                   and not (s[0] == "origins" and not (p.pack and packdef.origins(p.pack, p.choices)))
                   and not (s[0] == "bake" and not (self.o.bake and p.pack and any(
                       L.get("bake") for L in packdef.chosen_layers(p.pack, p.choices))))]
        return p

    def _origins(self, pack, choices, fetcher):
        todo = packdef.origins(pack, choices)
        for i, v in enumerate(todo):
            name = v["origin"]["name"]
            fetcher.progress = lambda done, total, msg="", _n=name, _i=i: self.progress(
                "origins", (_i + ((done / total) if total else 1.0)) / len(todo),
                "%s: %s of %s" % (_n, packdef.human(done), packdef.human(total)))
            while True:
                self._check_cancel()
                try:
                    fetcher.fetch_all([v])
                    break
                except fetch.OriginNeeded as e:
                    if not self.wait_for_download:
                        raise
                    self.log("%s did not hand \"%s\" to Setup (%s); the browser gets it instead"
                             % (e.site, e.name, e.why), "warn")
                    self.wait_for_download(e)
        if todo:
            self.progress("origins", 1.0, "done")

    def _changed(self, P, pack, choices):
        """The chosen layers installed here by an Umbra whose files were different (not the
        missing ones): what an update brings, even under the same pack version."""
        out = []
        mods_dir = os.path.join(P["mo2"], "mods") if P.get("mo2") else None
        for L in packdef.chosen_layers(pack, choices):
            if L.get("target", "mods") == "anomaly":
                m = layers.anomaly_marker(P["anomaly"], L["id"]) if P.get("anomaly") else None
                name = L.get("folder") or L["id"]
            elif mods_dir:
                _, m = layers.installed(mods_dir, L, packdef.other_ids(pack, L))
                name = layers.choose_folder(mods_dir, L)         # its name after the update
            else:
                continue
            if m and m.get("volumes") != layers.volume_keys(L):
                out.append(name)
        return out

    def _bake_bytes(self, P, pack, choices, notes):
        """Room for Umbra Gore & Effects' body models when it has none yet: the prebaked
        download and its files (a bake on this machine makes about as much)."""
        if not self.o.bake:
            return 0
        mods_dir = os.path.join(P["mo2"], "mods") if P["mo2"] else None
        for L in packdef.chosen_layers(pack, choices):
            if not L.get("bake"):
                continue
            if mods_dir and os.path.isdir(mods_dir):
                folder, _ = layers.installed(mods_dir, L, packdef.other_ids(pack, L))
                if os.path.isfile(os.path.join(mods_dir, folder, prebake.BODY_REPORT)):
                    return 0
            b = L["bake"]
            if b.get("download"):
                notes.append("Umbra Gore & Effects' body models: Setup downloads the prebaked ones (%s) when they "
                             "fit your mod list, else bakes them on this computer (a few minutes)."
                             % packdef.human(b["download"]))
            return b.get("download", 0) + b.get("bytes", 0)
        return 0

    def _unpack_bytes(self, P, pack, choices):
        """What unpacking takes: the chosen layers not already in place."""
        n = 0
        mods_dir = os.path.join(P["mo2"], "mods")
        for L in packdef.chosen_layers(pack, choices):
            if L.get("target", "mods") == "anomaly":
                m = layers.anomaly_marker(P["anomaly"], L["id"]) if P["anomaly"] else None
                if m and m.get("volumes") == layers.volume_keys(L):
                    continue
            else:
                _, m = layers.installed(mods_dir, L, packdef.other_ids(pack, L))
                if m and m.get("volumes") == layers.volume_keys(L):
                    continue
            n += L.get("bytes", 0)
        return n

    # ------------------------------------------------------------ the install
    def run(self, plan=None):
        """Install (the plan is made first when not given). Everything logged also goes
        to <work>/logs/setup-<time>.log."""
        plan = plan or self.plan()
        if not plan.ok():
            raise RuntimeError("; ".join(plan.problems))
        P = self.paths()
        os.makedirs(P["work"], exist_ok=True)
        try:
            logf = open_log(P["work"])
        except OSError:
            logf = None
        to_window = self.log

        def tee(msg, level="info"):
            if logf:
                try:
                    logf.write("%s %-5s %s\n" % (time.strftime("%H:%M:%S"), level, msg))
                    logf.flush()
                except (OSError, ValueError):
                    pass
            to_window(msg, level)
        self.log = tee
        try:
            return self._run(plan, P)
        except BaseException:
            if logf:
                logf.write(traceback.format_exc())
            raise
        finally:
            self.log = to_window
            if logf:
                logf.close()

    def _run(self, plan, P):
        t0 = time.time()
        pack = plan.pack
        state = self._load_state(P)
        self.log("Umbra %s (%s channel), installer %s" % (pack["version"], self.o.channel, VERSION))
        self.progress("pack", 1.0, "Umbra %s" % pack["version"])

        cache = os.path.join(P["work"], "cache")
        fetcher = fetch.Fetcher(self.o.source, cache, cancel=self.cancelled, workers=4, log=self.log)

        # ---- mods from their authors' own pages: first, so a site that wants the player's browser
        # is found out now and not after G.A.M.M.A.'s long install
        self._origins(pack, plan.choices, fetcher)

        # ---- G.A.M.M.A.
        if self.o.mode == "new":
            self._gamma(pack, P, fetcher)
            P["anomaly"] = P["anomaly"] if os.path.isdir(P["anomaly"]) else mo2.game_path(P["mo2"])
        self._check_cancel()

        # ---- download
        choices = plan.choices
        chosen = packdef.chosen_layers(pack, choices)
        profs = packdef.chosen_profiles(pack, choices)
        vols = packdef.unique_volumes(chosen)
        fetcher.progress = lambda done, total, msg="": self.progress(
            "download", (done / total) if total else 1.0,
            "%s of %s%s" % (packdef.human(done), packdef.human(total), (" - " + msg) if msg else ""))
        left = [v for v in vols if not os.path.exists(os.path.join(cache, *v["path"].split("/")) + ".ok")]
        if left:
            self.log("Downloading %s (%d of %d files; the others are already downloaded)"
                     % (packdef.human(sum(v["size"] for v in left)), len(left), len(vols))
                     if len(left) < len(vols) else
                     "Downloading %s (%d files)" % (packdef.human(sum(v["size"] for v in left)), len(vols)))
        else:
            self.log("Umbra's %d files are already downloaded" % len(vols))
        fetcher.fetch_all(vols + [pr["modlist"] for pr in profs])
        self.progress("download", 1.0, "done")

        # ---- layers
        toks = layers.tokens(P["mo2"], P["anomaly"], zgo_cache_dir(), under_wine=(os.name != "nt"))
        mods_dir = os.path.join(P["mo2"], "mods")
        self._renamed(pack, P, mods_dir)
        renames, folders = {}, {}
        total = sum(L.get("bytes", 0) for L in chosen) or 1
        done = 0
        for L in chosen:
            self._check_cancel()
            vpaths = [fetcher.path(v) for v in L["volumes"]]
            lb = L.get("bytes", 0)

            def prog(d, t, rel, _done=done, _lb=lb, _L=L):
                self.progress("layers", min(1.0, (_done + (_lb * d / t if t else 0)) / total),
                              "%s: %s" % (_L.get("folder") or _L["id"], rel))
            if L.get("target", "mods") == "anomaly":
                # an extra profile's engine only ever adds files: the co-op exes stay as they are
                changed = layers.install_anomaly_layer(P["anomaly"], L, vpaths, toks, prog, self.cancelled,
                                                       pack["version"], new_only=bool(L.get("profile")),
                                                       log=self.log)
                self.log("%s %s" % ("Installed" if changed else "Already in place:", L["id"]))
            else:
                folder, changed = layers.install_mods_layer(mods_dir, L, vpaths, toks, prog, self.cancelled,
                                                            pack["version"], log=self.log, repair=self.o.repair)
                folders[L["id"]] = folder
                if folder != L["folder"]:
                    renames[L["folder"]] = folder
                    self.log('"%s" already exists and is not Umbra\'s; installed as "%s"' % (L["folder"], folder),
                             "warn")
                self.log("%s %s" % ("Installed" if changed else "Already in place:", folder))
            done += lb
        # an option switched off again: its engine files leave the Anomaly folder (originals come back)
        for L in pack["layers"]:
            if L not in chosen and L.get("target", "mods") == "anomaly" and \
                    layers.anomaly_marker(P["anomaly"], L["id"]):
                layers.restore_anomaly_layer(P["anomaly"], L["id"])
                self.log("Removed %s from the Anomaly folder (its option is off)" % L["id"])
        self.progress("layers", 1.0, "done")

        # ---- the profiles
        # layers of options not picked: off if they were installed before, else not listed
        off, drop = set(), set()
        for L in pack["layers"]:
            if L in chosen or L.get("target", "mods") != "mods":
                continue
            folder = layers.choose_folder(mods_dir, L)
            if layers.read_marker(os.path.join(mods_dir, folder)):
                off.add(folder)
            else:
                drop.add(L["folder"])
        main = profs[0]
        for pr in profs:
            with open(fetcher.path(pr["modlist"]), "rb") as f:
                modlist = f.read()
            pdir = mo2.make_profile(P["mo2"], pr["name"], pr.get("base") or main["base"], modlist, renames,
                                    off=off, drop=drop)
            if pr is not main:
                self.log('Made the "%s" profile (%s)' % (pr["name"], pdir))
        for pr in packdef.all_profiles(pack):
            if pr not in profs and os.path.isdir(os.path.join(P["mo2"], "profiles", pr["name"])):
                self.log('The "%s" profile is still in Mod Organizer 2, but its option is off now, so its '
                         'engine is not installed.' % pr["name"], "warn")
        mo2.set_selected_profile(P["mo2"], main["name"])
        self.log('Mod Organizer 2 opens the "%s" profile now; G.A.M.M.A.\'s own profile is unchanged.' % main["name"])
        self.progress("profile", 1.0, "done")

        # ---- Umbra Gore & Effects' body models: prebaked when they fit, else baked here
        bake_state = self._bake(chosen, P, fetcher, folders, main["name"], mods_dir) if self.o.bake else None
        self.progress("bake", 1.0, "done")

        # ---- settings: user.ltx and Mod Organizer 2's Run list
        self._settings(pack, profs, P)
        exe_titles = self._executables(pack, profs, P, state)
        self.progress("settings", 1.0, "done")

        # ---- what the game needs from Microsoft (runtime.py)
        self._runtime(P)

        # ---- the launcher (and the manifest it reads the options from)
        self._save_json(os.path.join(P["work"], "pack.json"), pack)
        launcher = install_launcher(P["work"], self.log)
        self.progress("launcher", 1.0, "done")

        # ---- the menu and the desktop: Umbra without Steam
        made = self._shortcuts(P, state, launcher)

        # ---- Steam
        appid = state.get("steam_appid")
        shortcuts = dict(state.get("steam_shortcuts", {}))
        if self.o.steam:
            got = self._steam(P, pack, profs, launcher)
            if got:
                appid = got.pop(packdef.MAIN, None) or appid
                shortcuts = got

        state.update({"pack": pack["version"], "channel": self.o.channel, "source": self.o.source,
                      "choices": sorted(choices),
                      "mode": self.o.mode, "mo2": P["mo2"], "anomaly": P["anomaly"], "cache": P["cache"],
                      "launcher": launcher, "folders": folders,
                      "bake": bake_state or state.get("bake"),
                      "profiles": [pr["name"] for pr in profs], "executables": exe_titles,
                      "steam_appid": appid, "steam_shortcuts": shortcuts, "shortcuts": made,
                      "compat_tool": self.o.compat_tool or state.get("compat_tool"),
                      "installed": time.strftime("%Y-%m-%d %H:%M:%S"), "installer": VERSION})
        self._save_state(P, state)
        self._write_credits(P, pack)
        self.log("Done in %s." % took(time.time() - t0))
        return state

    # ---------------------------------------------------------------- the bake (prebake.py)

    def _bake(self, chosen, P, fetcher, folders, profile, mods_dir):
        """Umbra Gore & Effects' body models for this install: up to date already, the
        prebaked bake when it fits this mod list, else baked here by Umbra Effects Setup.
        Returns {"how": ..., "fingerprint": ...} for the state file. A bake that fails only
        costs the clean cuts: the mod works without it and says so in the game."""
        L = next((x for x in chosen if x.get("bake")), None)
        if L is None:
            return None
        folder = folders.get(L["id"]) or L["folder"]
        zdir = os.path.join(mods_dir, folder)
        prog = prebake.program(zdir)
        if not prog:
            self.log("Umbra Gore & Effects has no %s, so its body models were not baked" % prebake.PROGRAM, "warn")
            return {"how": "none"}
        from .launcher import child_env      # launcher imports this module
        env = child_env()
        work = os.path.join(P["work"], "bake")
        args = ["--mod", zdir, "--mo2", P["mo2"], "--profile", profile, "--game", P["anomaly"]]
        toks = layers.tokens(P["mo2"], P["anomaly"], zgo_cache_dir(), under_wine=(os.name != "nt"),
                             profile=profile, zgo_folder=folder)

        # the "bake" step's bar over all of it: it never goes back, whichever way it goes (_bake_with)
        bar = prebake.Bar(self.progress, "bake")

        def zgo(more, what, lo=0.0, hi=1.0):
            show = bar.part(lo, hi)
            job = "bake" if "--bake" in more else more[0]

            def on_line(line):
                f = prebake.progress_of(line, job)
                if f is not None:
                    show(f, "%s: %s" % (what, line.strip()[:100]))
            # a bake is minutes on a fast machine; the limits only stop one that hangs
            code, lines = prebake.run(prog, args + more, work, env, on_line, self.cancelled,
                                      timeout=(4 if "--bake" in more else 1) * 3600)
            if code is None:
                raise prebake.BakeError(lines[0])
            return code, lines, prebake.parse_result(lines)

        try:
            return self._bake_with(L, zdir, mods_dir, fetcher, toks, zgo, bar)
        except prebake.BakeError as e:
            self.log("%s. Umbra Gore & Effects works without its body models (cut limbs look rougher); run Umbra "
                     "Effects Setup from its folder (tools/zgo_setup) to bake them." % e, "warn")
            return {"how": "failed"}

    def _bake_with(self, L, zdir, mods_dir, fetcher, toks, zgo, bar):
        # the bar: the check 0-0.02; the prebaked bake 0.02-0.9 (_prebaked); a bake here to 1.0 (from
        # where the bar is); packing, the props and kid mode share what is left after the first two
        bar(0.0, "checking the body models")
        _, lines, res = zgo(["--check"], "Checking", 0.0, 0.02)
        how, fp = None, None
        if res.get("body") in prebake.FITS:
            how = "up to date"
            self.log("Umbra Gore & Effects' body models are in place and fit your mod list")
        else:
            fp = self._prebaked(L, zdir, mods_dir, fetcher, toks, zgo, bar)
            if fp:                          # (it is in place now)
                self._adopt_dates(zgo)
                bar(0.88, "checking the body models")
                _, lines, res = zgo(["--check"], "Checking", 0.88, 0.9)
                if res.get("body") in prebake.FITS:
                    how = "prebaked"
                else:
                    self.log("Umbra Effects Setup does not accept the prebaked body models (%s); baking them here "
                             "instead" % (res.get("body") or "no answer"), "warn")
        if how is None:
            return self._bake_here(zgo, fp)
        if res.get("body") == "unused":
            self._unused(lines)
        # the bake is in. Made before Umbra Gore & Effects 1.8.2 (prebaked or not), its ~80,000 small
        # parts are loose files, which the game reads one by one at every start: into a few archives.
        # Breakable props and kid mode for particles depend on the mod list too
        todo = [(w, x) for w, x, needed in (
            (8, "pack", res.get("pack") == "needed"), (3, "props", res.get("props") not in (None, "ok", "none")),
            (1, "kid", res.get("kid") not in (None, "ok", "off", "none"))) if needed]
        at, left, spans = bar.at, sum(w for w, _ in todo), {}
        for w, x in todo:
            spans[x] = (at, at + (1.0 - bar.at) * w / left)
            at = spans[x][1]
        if "pack" in spans:
            code, _, _ = zgo(["--pack", "--yes"], "Packing the bake", *spans["pack"])
            self.log("Packed Umbra Gore & Effects' bake into a few archives, so the game starts faster" if code == 0
                     else "Umbra Effects Setup could not pack the bake (exit %s); it works, but the game starts "
                     "slower" % code, "info" if code == 0 else "warn")
        if "props" in spans:
            code, _, _ = zgo(["--props", "--yes"], "Cutting the props", *spans["props"])
            self.log("Cut the breakable props for your mod list" if code == 0 else
                     "Umbra Effects Setup could not cut the breakable props (exit %s)" % code,
                     "info" if code == 0 else "warn")
        if "kid" in spans:
            zgo(["--kid-particles-apply", "--yes"], "Kid mode for particles", *spans["kid"])
        return {"how": how, "fingerprint": fp}

    def _bake_here(self, zgo, fp):
        """Umbra Effects Setup bakes for this mod list on this machine."""
        self.log("Baking Umbra Gore & Effects' body models for your mod list: a few minutes, using every CPU core")
        # --no-download: Setup has decided about the prebaked bake already. Without it, Umbra Effects Setup
        # (1.8.1 on) gets that bake itself when it fits: a second download of all of it
        code, lines, res = zgo(["--bake", "--yes", "--no-download"], "Baking", 0.05, 1.0)
        if prebake.unknown_option(code, lines):     # from before 1.8.1, which never downloads
            self.log("This Umbra Effects Setup is older than 1.8.1 and does not know --no-download (it never "
                     "downloads); baking without it")
            code, lines, res = zgo(["--bake", "--yes"], "Baking", 0.05, 1.0)
        if code == 0:
            said = [x.strip() for x in lines if x.startswith(("Baked ", "Put the ready-made bake in place"))]
            for line in said:
                self.log(line)
            got = any(x.startswith("Put the ready-made bake in place") for x in said)
            return {"how": "prebaked" if got else "baked here", "fingerprint": fp}
        tail = [x for x in lines if x.strip()][-4:]
        raise prebake.BakeError("Umbra Effects Setup could not bake the body models (exit %s): %s"
                                % (code, " | ".join(tail) or "no output"))

    def _unused(self, lines):
        """--check: body=unused (1.8.3): the bake is right, but the game doesn't load it. Not a reason
        to bake again; the player has to change something in MO2 (its words say what)."""
        why = []
        for i, x in enumerate(lines):
            if "doesn't use it" in x:
                why = [y.strip() for y in lines[i + 1:i + 3] if y.strip() and not y.startswith("RESULT ")][:1]
                break
        self.log("Umbra Gore & Effects' body models are in place, but the game won't load them: %s"
                 % (why[0] if why else "Umbra Gore & Effects is not an enabled mod of the MO2 profile"), "warn")

    def _adopt_dates(self, zgo):
        """The prebaked bake's report has the dates of the model archives and models of the machine
        that made it, and Umbra Effects Setup compares by date: wherever this machine's differ (the
        game's meshes*.db0 always do) it finds the bake out of date and would download or bake it all
        again. --adopt-dates (1.8.3) records this machine's dates, as Umbra Effects Setup does after
        its own download; --match has compared the models by content first."""
        try:
            code, lines, res = zgo(["--adopt-dates", "--yes"], "Recording this machine's file dates", 0.86, 0.88)
        except prebake.BakeError as e:
            code, lines, res = None, [str(e)], {}
        if code == 0 and "adopted" in res:
            self.log("Recorded this machine's file dates in the prebaked bake's report (%s models)" % res["adopted"])
            return
        tail = " | ".join(x.strip() for x in lines if x.strip() and not x.startswith("usage:"))[-300:]
        if code == 2:           # older than 1.8.3 (argparse: "unrecognized arguments"), or it can't run here
            self.log("This Umbra Effects Setup can't record this machine's file dates in the prebaked bake (exit 2%s; "
                     "1.8.3 can), so it may find the bake out of date" % ((": " + tail) if tail else ""))
        else:
            self.log("Umbra Effects Setup could not record this machine's file dates in the prebaked bake (exit %s%s)"
                     % (code, (": " + tail) if tail else ""), "warn")

    def _prebaked(self, L, zdir, mods_dir, fetcher, toks, zgo, bar):
        """Download the layer's prebaked bake and put it in place, if Umbra Effects Setup says
        it fits this mod list. Returns its fingerprint, or None (bake here instead)."""
        ref = L["bake"]
        tmp = os.path.join(mods_dir, ".umbra-tmp-bake")
        saved = fetcher.progress
        try:
            bar(0.02, "getting the prebaked bake's report")
            fetcher.fetch_all([ref])
            with open(fetcher.path(ref), "rb") as f:
                b = packdef.check_bake(packdef.loads(f.read(), "the bake manifest"), ref)
            fetcher.fetch_all([b["report"]])
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            names = layers.unpack([fetcher.path(b["report"])], tmp)
            if prebake.BODY_REPORT not in names:
                raise prebake.BakeError("its report is missing")
            layers.relocate(tmp, [r for r in b.get("relocate", []) if r in names], toks)
            code, lines, res = zgo(["--match", tmp], "Comparing", 0.02, 0.05)
            if "match" not in res:                  # an Umbra Effects Setup from before 1.8.0, or it failed
                tail = [x.strip() for x in lines if x.strip()][-2:]
                self.log("Umbra Effects Setup could not compare the prebaked body models with your mod list (exit "
                         "%s: %s); baking them for yours instead" % (code, " | ".join(tail) or "no output"))
                return None
            if res.get("match") != "yes":
                why = [x.strip()[2:] for x in lines if x.startswith("  - ")][:3]
                self.log("The prebaked body models don't fit this install (%s); baking them for your mod list and "
                         "settings instead" % ("; ".join(why) or "Umbra Effects Setup said no, exit %s" % code))
                return None
            vols = b["volumes"]
            left = [v for v in vols if not fetcher.have(v)]
            need = sum(v["size"] for v in left) + b.get("bytes", 0)
            free = prebake.disk_free(mods_dir)
            if free is not None and free < need:
                raise prebake.BakeError("%s free on the drive, %s needed" % (packdef.human(free), packdef.human(need)))
            self.log("The prebaked body models fit your mod list: downloading them (%s, %d models)"
                     % (packdef.human(sum(v["size"] for v in left)), b.get("models", 0)))
            down = bar.part(0.05, 0.75)
            fetcher.progress = lambda done, total, msg="": down(
                (done / total) if total else 1.0,
                "downloading %s of %s%s" % (packdef.human(done), packdef.human(total),
                                            ("; " + msg) if "retrying" in msg else ""))
            fetcher.fetch_all(vols)
            shutil.rmtree(tmp)
            os.makedirs(tmp)
            paths = [fetcher.path(v) for v in [b["report"]] + vols]
            unpack = bar.part(0.75, 0.86)
            names = layers.unpack(paths, tmp, lambda d, t, rel: unpack(d / t if t else 1.0, "unpacking: %s" % rel),
                                  self.cancelled)
            layers.relocate(tmp, b.get("relocate", []), toks)
            prebake.remove_old(zdir, names)
            prebake.move_in(tmp, zdir, names)
            for v in vols:                  # unlike the layers' volumes, Repair never needs these
                for f in (fetcher.path(v), fetcher.path(v) + ".ok"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            self.log("Put the prebaked body models in place: %d files (fingerprint %s)" % (len(names),
                                                                                          b["fingerprint"]))
            return b["fingerprint"]
        except (fetch.FetchError, packdef.PackError, prebake.BakeError, zipfile.BadZipFile, OSError,
                ValueError) as e:
            self.log("The prebaked body models could not be used (%s); baking them here instead" % e, "warn")
            return None
        finally:
            fetcher.progress = saved
            shutil.rmtree(tmp, ignore_errors=True)

    def _renamed(self, pack, P, mods_dir):
        """Mods the pack has renamed (a layer's "was"): an earlier Umbra's folder takes the new
        name, and every profile lists it under that name."""
        moved = {}
        for L in pack["layers"]:
            if L.get("target", "mods") != "mods" or not L.get("was"):
                continue
            try:
                got = layers.adopt_renamed(mods_dir, L, packdef.other_ids(pack, L))
            except OSError as e:
                self.log('Could not rename "%s" to "%s" (%s); it is installed again under the new name, '
                         'and the old folder can be deleted in Mod Organizer 2'
                         % (layers.renamed_folder(mods_dir, L) or L["was"][0], L["folder"], e), "warn")
                continue
            if got:
                moved[got[0]] = got[1]
                self.log('Renamed "%s" to "%s"' % got)
        if moved:
            mo2.rename_mods(P["mo2"], moved)

    # ------------------------------------------------------------ steps
    def _gamma(self, pack, P, fetcher):
        os.makedirs(P["root"], exist_ok=True)
        done = os.path.join(P["work"], "gamma.done")
        base = pack["profile"].get("base") or pack["gamma"].get("mo2_profile", "G.A.M.M.A")
        if os.path.exists(os.path.join(P["mo2"], "ModOrganizer.exe")) and os.path.exists(done):
            lacks = gamma_problems(P["mo2"], P["anomaly"], base, self.log)
            if not lacks:
                self.log("G.A.M.M.A. is already installed here")
                self.progress("gamma", 1.0, "already installed")
                return
            self.log("G.A.M.M.A. here is incomplete (%s), so it is installed again" % "; ".join(lacks), "warn")
            os.remove(done)
        g = pack["gamma"]
        lists = fetcher.fetch_all([g["modpack_maker_list"], g["modlist"]])
        cli = gammacli.Cli(pack, P["work"], self.log)
        self.progress("gamma", 0.0, "getting stalker-gamma-cli")
        cli.ensure(self.cancelled)
        for d in (P["anomaly"], P["mo2"], P["cache"]):
            os.makedirs(d, exist_ok=True)
        try:
            cli.configure(P["anomaly"], P["mo2"], P["cache"], self.o.threads)
            resume = os.path.exists(os.path.join(P["mo2"], "resume.txt"))
            last = {"t": 0}

            def on_line(o):
                t = o.get("type")
                if t == "log":
                    lvl = (o.get("level") or "").lower()
                    self.log(o.get("message", ""), "warn" if lvl.startswith("warn") else
                             "error" if lvl.startswith(("err", "fatal")) else "info")
                elif t == "progress":
                    now = time.time()
                    if now - last["t"] < 0.25:
                        return
                    last["t"] = now
                    total, done = o.get("total") or 0, o.get("complete") or 0
                    frac = (done / total) if total else 0.0
                    pr = o.get("progress")
                    if isinstance(pr, (int, float)) and total:
                        frac = (done + max(0.0, min(1.0, pr / 100.0 if pr > 1 else pr))) / total
                    self.progress("gamma", min(1.0, frac), "%s %s (%d/%d)" % (
                        o.get("operation", ""), o.get("name") or o.get("archiveName", ""), done, total))
            self.log("Installing G.A.M.M.A. with stalker-gamma-cli (this is the long part: Anomaly and about "
                     "400 addons are downloaded and set up)")
            cli.full_install(lists[0], lists[1], on_line, self.cancelled, resume=resume, mirror=self.o.mirror)
        finally:
            cli.restore()
        if not os.path.exists(os.path.join(P["mo2"], "ModOrganizer.exe")):
            raise gammacli.GammaError("stalker-gamma-cli finished but there is no ModOrganizer.exe in %s" % P["mo2"])
        lacks = gamma_problems(P["mo2"], P["anomaly"], base, self.log)
        if lacks:
            raise gammacli.GammaError("stalker-gamma-cli finished, but G.A.M.M.A. is incomplete: %s. Run Umbra "
                                      "Setup again to retry (what is there already is kept)." % "; ".join(lacks))
        open(done, "w").close()
        self.progress("gamma", 1.0, "done")

    def _runtime(self, P):
        cache = os.path.join(P["work"], "cache")

        def prog(done, total):
            self.progress("runtime", (done / total) if total else 1.0,
                          "%s of %s" % (packdef.human(done), packdef.human(total)))
        if os.name == "nt":
            runtime.windows_install(cache, P["anomaly"], self.log, prog, self.cancelled)
        else:
            runtime.prepare(cache, "prefix", self.log, prog, self.cancelled)
            self.log("Before the game starts, the Umbra launcher checks Proton's prefix for Microsoft's "
                     "Visual C++ runtime and DirectX files and adds any that are missing.")
        self.progress("runtime", 1.0, "done")

    def _profile_warnings(self, pack, choices):
        out = []
        for pr in packdef.chosen_profiles(pack, choices)[1:]:
            if "avx2" in pr.get("requires", []) and cpu_has_avx2() is False:
                out.append('"%s" needs a CPU with AVX2, which this one lacks: its game would close at start. '
                           'The normal Umbra profile works.' % pr["name"])
            for d in pack.get("dependencies", []):
                if d.get("profile") == pr["id"] and d.get("required") and not d.get("fetched"):
                    out.append('"%s" also needs %s, which Umbra can\'t download for you yet: get it from %s and '
                               'install it in Mod Organizer 2, switched on in the "%s" profile.'
                               % (pr["name"], d["title"], d.get("url") or "its author", pr["name"]))
        return out

    def _settings(self, pack, profs, P):
        """The console settings the pack ships (appdata/user.ltx; a profile's own file)."""
        appdata = os.path.join(P["anomaly"], "appdata")
        userltx.merge(os.path.join(appdata, "user.ltx"), pack.get("user_ltx", {}).get("lines", []), self.log)
        for pr in profs:
            u = pr.get("user_ltx") or {}
            if not u.get("lines") and not u.get("file"):
                continue
            if u.get("file"):
                userltx.seed(os.path.join(appdata, u["file"]), os.path.join(appdata, u.get("seed") or "user.ltx"),
                             u.get("lines", []), self.log)
            else:
                userltx.merge(os.path.join(appdata, "user.ltx"), u.get("lines", []), self.log)

    def _executables(self, pack, profs, P, state):
        """Mod Organizer 2's Run list: the chosen profiles' entries in, Umbra's old ones out."""
        under_wine = os.name != "nt"
        want = []
        for pr in profs:
            for e in pr.get("executables", []):
                base = P["anomaly"] if e.get("in", "anomaly") == "anomaly" else P["mo2"]
                binary = os.path.join(base, *e["binary"].replace("\\", "/").split("/"))
                cwd = os.path.join(base, *e["cwd"].split("/")) if e.get("cwd") else os.path.dirname(binary)
                want.append({"title": e["title"], "binary": mo2.mo2_path(binary, under_wine),
                             "workingDirectory": mo2.mo2_path(cwd, under_wine), "arguments": e.get("arguments", "")})
        titles = [e["title"] for e in want]
        old = [t for t in state.get("executables", []) if t not in titles]
        if want or old:
            mo2.set_executables(P["mo2"], want, remove=old, log=self.log)
        return titles

    def _shortcuts(self, P, state, launcher):
        """The Start menu (Windows) or applications menu (Linux) entry and the desktop shortcut,
        both starting the launcher: Umbra without Steam. Returns their paths."""
        had = state.get("shortcuts")
        if not launcher or self.o.shortcuts is False:
            return had or []
        keep = None
        if self.o.shortcuts is None and had is not None:
            keep = [p for p in had if os.path.exists(p)]       # the player deleted the others
        icon = None
        if os.name != "nt":
            icon = os.path.join(P["work"], "umbra-icon.png")
            src = asset_path("umbra-icon-256.png")
            if src and not os.path.isfile(icon):
                shutil.copy2(src, icon)
            icon = icon if os.path.isfile(icon) else None
        made = shortcuts.make(P["work"], launcher, icon, keep_only=keep, log=self.log)
        if made and keep is None:
            where = ([shortcuts.where_words()] if "menu" in made else []) + (["the desktop"] if "desktop" in made else [])
            self.log("Added Umbra to %s: it starts from there without Steam too." % " and ".join(where))
        self.progress("shortcuts", 1.0, "done")
        return sorted(made.values())

    def _steam(self, P, pack, profs, launcher=None):
        """The Umbra shortcut (it starts the launcher), plus one per extra profile picked
        (removed again when unpicked). Returns {profile id: appid}."""
        roots = steam.steam_roots()
        if not roots:
            self.log("Steam is not installed here; no shortcut added.", "warn")
            return None
        root = roots[0]
        us = steam.users(root)
        user = self.o.steam_user if self.o.steam_user in us else (us[0] if us else None)
        if not user:
            self.log("No Steam account has used this Steam yet; no shortcut added.", "warn")
            return None
        waited = 0
        while steam.running():
            if waited == 0:
                self.log("Waiting for Steam to close (it would overwrite the shortcut)...", "warn")
                self.progress("steam", 0.0, "close Steam to finish")
            self._check_cancel()
            time.sleep(1)
            waited += 1
        icon_src = asset_path("umbra-icon-256.png")
        icon = ""
        if icon_src:
            icon = os.path.join(P["work"], "umbra-icon.png")
            shutil.copy2(icon_src, icon)
        art = {k: asset_path(f) for k, f in (("logo", "umbra-logo-1280.png"), ("capsule", "umbra-capsule.png"),
                                              ("hero", "umbra-hero.png"), ("wide", "umbra-wide.png"))}
        exe = os.path.join(P["mo2"], "ModOrganizer.exe")
        tool = None
        if os.name != "nt":
            tool = self.o.compat_tool or "proton_experimental"
            self._flatpak_access(root, P)
        art = {k: v for k, v in art.items() if v}
        main = profs[0]
        # Windows: Steam starts the launcher. Linux: Steam still starts MO2 through Proton, wrapped by
        # the launcher (native, so it can change the install), which adds MO2's arguments to that command.
        # Without a launcher, -p: MO2 remembers the last profile it was started with.
        if launcher and os.name == "nt":
            main_exe, main_dir, main_opts = launcher, P["work"], ""
        elif launcher:
            main_exe, main_dir, main_opts = exe, P["mo2"], '"%s" -- %%command%%' % launcher
        else:
            main_exe, main_dir, main_opts = exe, P["mo2"], '-p "%s"' % main["name"]
        out = {packdef.MAIN: steam.add_shortcut(root, user, "Umbra", main_exe, main_dir, icon=icon,
                                                launch_options=main_opts, tags=("S.T.A.L.K.E.R.",),
                                                compat_tool=tool, artwork=art)}
        self.log("Added Umbra to Steam%s; it opens the Umbra launcher." % ((" (runs with %s)" % tool) if tool else ""))
        for pr in packdef.all_profiles(pack)[1:]:
            s = pr.get("steam")
            if not s:
                continue
            marker = "umbra-" + pr["id"]
            if pr in profs and self.o.profile_shortcuts:
                # "run -e" (not moshortcut://): an MO2 that is already open gets this command
                # line and refuses it if it is on another profile, instead of running the engine there
                opts = '-p "%s"' % pr["name"] + (' run -e "%s"' % s["run"] if s.get("run") else "")
                out[pr["id"]] = steam.add_shortcut(root, user, s["title"], exe, P["mo2"], icon=icon,
                                                   launch_options=opts, tags=("S.T.A.L.K.E.R.",),
                                                   compat_tool=tool, artwork=art, marker=marker)
                self.log('Added "%s" to Steam (starts %s in the "%s" profile).'
                         % (s["title"], s.get("run") or "Mod Organizer 2", pr["name"]))
            elif steam.remove_shortcut(root, user, s["title"], marker=marker):
                self.log('Removed "%s" from Steam.' % s["title"])
        self.progress("steam", 1.0, "done")
        return out

    def _flatpak_access(self, root, P):
        """Steam from Flathub sees only its own folders: let it reach the install (the launcher,
        MO2 and Anomaly), as `flatpak override` does."""
        if "/.var/app/" + steam.FLATPAK_ID + "/" not in root.replace(os.sep, "/") + "/":
            return
        where = os.path.commonpath([os.path.realpath(p) for p in (P["work"], P["mo2"], P["anomaly"])])
        cmd = ["flatpak", "override", "--user", "--filesystem=%s" % where, steam.FLATPAK_ID]
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=60)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            self.log("Steam is the Flatpak version: it may now reach %s." % where)
        else:
            self.log("Steam is the Flatpak version, and Umbra Setup couldn't let it reach %s. Run this, or the "
                     "shortcut can't start Umbra: %s" % (where, " ".join(shlex.quote(c) for c in cmd)), "warn")

    # ------------------------------------------------------------ state
    def _load_state(self, P):
        try:
            with open(os.path.join(P["work"], "state.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, P, state):
        self._save_json(os.path.join(P["work"], "state.json"), state)

    @staticmethod
    def _save_json(p, data):
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(p + ".tmp", p)

    def _write_credits(self, P, pack):
        lines = ["Umbra %s is made of other people's work. Thank you, all of you." % pack["version"], ""]
        for c in pack.get("credits", []):
            who = ", ".join(c.get("authors") or []) or "author unknown"
            lines.append("%s - %s%s" % (c.get("mod", c.get("id")), who, ("  " + c["url"]) if c.get("url") else ""))
        with open(os.path.join(P["mo2"], "UMBRA-CREDITS.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
