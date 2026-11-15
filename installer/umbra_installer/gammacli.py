"""
Installing G.A.M.M.A. with FaithBeam's stalker-gamma-cli, pinned.

The CLI release comes from the pack (URL + sha256). Its settings are kept
apart from the player's own: on Linux the CLI gets a private config folder
(XDG_CONFIG_HOME); on Windows, where it always uses %APPDATA%\\stalker-gamma,
Umbra adds an "Umbra" profile there and switches the active profile back to
the player's afterwards.

full-install runs with --json, so progress comes as one JSON object per line
("log", "progress", "result").
"""
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import zipfile

from . import fetch, packdef

REPO_FLAGS = {
    "stalker-gamma": "--stalker-gamma-repo-branch",
    "gamma-setup": "--gamma-setup-repo-branch",
    "gamma-large-files": "--gamma-large-files-repo-branch",
    "teivaz-anomaly-gunslinger": "--teivaz-anomaly-gunslinger-repo-branch",
}
PROFILE = "Umbra"


class GammaError(Exception):
    pass


def platform_key():
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64"
    return ("win-" if os.name == "nt" else "linux-") + arch


def _no_window():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class Cli:
    def __init__(self, pack, work_dir, log=None):
        self.pack = pack
        self.work = work_dir                     # <install>/.umbra
        self.log = log or (lambda msg, level="info": None)
        self.exe = None
        self._restore_profile = None

    # ------------------------------------------------------------ the program
    def ensure(self, cancel=None):
        """Download (once) and unpack the pinned CLI; returns its path."""
        if os.environ.get("UMBRA_GAMMA_CLI"):            # tests and debugging: another CLI
            self.exe = os.environ["UMBRA_GAMMA_CLI"]
            return self.exe
        key = platform_key()
        asset = self.pack["gamma"]["cli"]["assets"].get(key)
        if not asset:
            raise GammaError("stalker-gamma-cli has no build for %s in this pack" % key)
        ver = self.pack["gamma"]["cli"]["version"]
        d = os.path.join(self.work, "stalker-gamma-cli", ver)
        exe = os.path.join(d, "stalker-gamma.exe" if os.name == "nt" else "stalker-gamma.AppImage")
        if os.path.exists(exe + ".ok"):
            self.exe = exe
            return exe
        os.makedirs(d, exist_ok=True)
        dl = os.path.join(d, os.path.basename(asset["url"].split("?")[0]).replace("%2B", "+"))
        self.log("Downloading stalker-gamma-cli %s" % ver)
        _download_pinned(asset["url"], asset["sha256"], dl, cancel)
        if dl.endswith(".zip"):
            with zipfile.ZipFile(dl) as z:
                z.extractall(d)
            found = [os.path.join(dp, f) for dp, _, fs in os.walk(d) for f in fs
                     if f.lower().startswith("stalker-gamma") and f.lower().endswith(".exe")]
            if not found:
                raise GammaError("the stalker-gamma-cli zip has no stalker-gamma*.exe")
            exe = found[0]
        else:
            shutil.copy2(dl, exe)
            os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        open(exe + ".ok", "w").close()
        self.exe = exe
        return exe

    def env(self):
        e = dict(os.environ)
        for k in ("APPIMAGE", "APPDIR", "ARGV0", "OWD"):    # Setup's own AppImage's: the CLI is another one
            e.pop(k, None)
        if os.name != "nt":
            e["XDG_CONFIG_HOME"] = os.path.join(self.work, "cli-config")
            # AppImages need FUSE to mount themselves; this way they don't
            e["APPIMAGE_EXTRACT_AND_RUN"] = "1"
            if not has_icu():
                # it's a .NET program, which stops at start without the system's ICU (Unicode
                # tables) unless told to do without: file and mod names don't need them
                e["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
        return e

    def run(self, args, on_line=None, cancel=None, check=True):
        """Run the CLI; returns (exit code, the JSON objects it printed)."""
        cmd = ([sys.executable, self.exe] if self.exe.endswith(".py") else [self.exe]) + list(args)
        self.log("stalker-gamma " + " ".join(args), "debug")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env=self.env(), cwd=self.work, **_no_window())
        objs = []
        try:
            for raw in p.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    o = {"type": "log", "level": "Information", "message": line}
                if not isinstance(o, dict):
                    continue
                if o.get("type") != "progress":
                    objs.append(o)
                if on_line:
                    on_line(o)
                if cancel and cancel():
                    p.terminate()
                    raise fetch.Cancelled()
        finally:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            p.stdout.close()
        res = [o for o in objs if o.get("type") == "result"]
        code = p.returncode
        if check and (code != 0 or (res and not res[-1].get("success", True))):
            err = res[-1].get("error") if res else None
            tail = [o.get("message", "") for o in objs if o.get("type") == "log"][-5:]
            raise GammaError("stalker-gamma %s failed (exit %s): %s" % (args[0] if args else "", code,
                                                                      err or " / ".join(tail)))
        return code, objs

    # ------------------------------------------------------------ settings
    def configure(self, anomaly, gamma, cache, threads=4):
        """The Umbra profile in the CLI's settings, pinned to the pack's repos."""
        if os.name == "nt":
            _, objs = self.run(["config", "list", "--json"], check=False)
            for o in objs:
                if o.get("type") == "profiles":
                    act = [p.get("name") for p in o.get("profiles", []) if p.get("active")]
                    if act and act[0] != PROFILE:
                        self._restore_profile = act[0]
        args = ["config", "create", "--name", PROFILE, "--anomaly", anomaly, "--gamma", gamma, "--cache", cache,
                "--mo2-profile", self.pack["gamma"].get("mo2_profile", "G.A.M.M.A"),
                "--download-threads", str(threads)]
        for k, v in self.pack["gamma"].get("repos", {}).items():
            if k in REPO_FLAGS:
                args += [REPO_FLAGS[k], v]
        self.run(args)
        self.run(["config", "use", PROFILE], check=False)

    def restore(self):
        """Windows: make the player's own profile active again."""
        if self._restore_profile:
            self.run(["config", "use", self._restore_profile], check=False)
            self._restore_profile = None

    # ------------------------------------------------------------ the install
    def full_install(self, maker_list, modlist, on_line=None, cancel=None, resume=False, mirror=True):
        # never --skip-extract-on-hash-match: it skips every archive already in the cache with the right
        # MD5, assuming it was extracted before. With a shared or kept cache, a new install got 41 of
        # about 420 archives and no Anomaly (2026-11-14).
        args = ["full-install", "--json", "--mod-pack-maker-path", maker_list, "--mod-list-path", modlist]
        if mirror:
            args.append("--use-anomaly-mirror")
        if resume:
            args.append("--resume")
        if os.name == "nt":
            args.append("--enable-long-paths")
        return self.run(args, on_line, cancel)


def has_icu():
    """Does this Linux have ICU (libicuuc), which .NET programs use for Unicode?"""
    import ctypes.util
    import glob
    try:
        if ctypes.util.find_library("icuuc"):
            return True
    except (OSError, ValueError):
        pass
    return any(glob.glob(os.path.join(d, "libicuuc.so.*")) for d in (
        "/usr/lib64", "/usr/lib", "/usr/lib/x86_64-linux-gnu", "/lib64", "/lib/x86_64-linux-gnu"))


def _download_pinned(url, sha, dst, cancel=None):
    if os.path.exists(dst) and packdef.sha256_file(dst) == sha:
        return dst
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": fetch.USER_AGENT})
    tmp = dst + ".part"
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            if cancel and cancel():
                raise fetch.Cancelled()
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    got = packdef.sha256_file(tmp)
    if got != sha:
        os.unlink(tmp)
        raise GammaError("stalker-gamma-cli download does not match its pinned sha256 (got %s)" % got)
    os.replace(tmp, dst)
    return dst
