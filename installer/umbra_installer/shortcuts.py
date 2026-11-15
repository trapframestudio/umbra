"""
Umbra in the system's own menus, so it starts without Steam too.

  Windows  "Umbra.lnk" in the Start menu (the player's own Programs folder: no admin
           needed) and on the desktop, made with the shell's own ShellLink object.
  Linux    "umbra-<id>.desktop" in the applications menu ($XDG_DATA_HOME/applications)
           and on the desktop ($XDG_DESKTOP_DIR), marked trusted for GNOME's desktop.

Each starts the launcher with no arguments; launcher.py says how it then runs the game
without Steam (on Linux: through Steam's Proton, with Steam itself closed or open).

UMBRA_SHORTCUTS_ROOT (tests): both go under that folder instead (menu/, desktop/).
"""
import hashlib
import os
import re
import subprocess

NAME = "Umbra"
WM_CLASS = "Umbra"          # the launcher window's class (launcher_gui.WM_CLASS, which Tk capitalises)
COMMENT = "Play Umbra (G.A.M.M.A. with Umbra's mods), pick its options and update it"
MARK = "X-Umbra-Work"       # in our .desktop files: the install they start
FOLDERID = {"programs": "A77F5D77-2E2B-44C3-A6A2-ABA601054A51",   # Start menu > Programs (the player's)
            "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"}


# ---------------------------------------------------------------- where
def known_folder(guid):
    """A Windows known folder (wherever the player moved it), or None."""
    try:
        import ctypes

        g = _guid(guid)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001 - not Windows
        return None


def xdg_user_dir(key, fallback):
    """An XDG user folder (user-dirs.dirs: XDG_DESKTOP_DIR...), if it exists and isn't home itself."""
    home = os.path.expanduser("~")
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "user-dirs.dirs")
    path = None
    try:
        with open(cfg, encoding="utf-8") as f:
            m = re.search(r'^XDG_%s_DIR="([^"]*)"' % key, f.read(), re.M)
        if m:
            path = m.group(1).replace("$HOME", home)
    except OSError:
        pass
    path = path or os.path.join(home, fallback)
    if os.path.isdir(path) and os.path.realpath(path) != os.path.realpath(home):
        return path
    return None


def places():
    """{"menu": folder or None, "desktop": folder or None}."""
    test = os.environ.get("UMBRA_SHORTCUTS_ROOT")
    if test:
        return {"menu": os.path.join(test, "menu"), "desktop": os.path.join(test, "desktop")}
    if os.name == "nt":
        return {"menu": known_folder(FOLDERID["programs"]), "desktop": known_folder(FOLDERID["desktop"])}
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return {"menu": os.path.join(data, "applications"), "desktop": xdg_user_dir("DESKTOP", "Desktop")}


def where_words():
    return "the Start menu" if os.name == "nt" else "the applications menu"


def label(work):
    """The install's own name, for telling two Umbra installs apart: its folder."""
    return os.path.basename(os.path.dirname(os.path.abspath(work))) or "Umbra"


# ---------------------------------------------------------------- Linux: .desktop files
def _desktop_string(s):
    return s.replace("\\", "\\\\")


def desktop_exec(path):
    """A path as the Exec key's program (Desktop Entry spec): quoted, with " ` $ \\ escaped inside
    the quotes, then the string's own escaping (\\ -> \\\\) and % -> %%."""
    inner = re.sub(r'(["`$\\])', r"\\\1", path)
    return ('"%s"' % inner).replace("\\", "\\\\").replace("%", "%%")


def desktop_entry(work, launcher, icon, name=NAME):
    lines = ["[Desktop Entry]", "Type=Application", "Version=1.0", "Name=" + _desktop_string(name),
             "GenericName=S.T.A.L.K.E.R. Anomaly modpack", "Comment=" + COMMENT,
             "Exec=" + desktop_exec(launcher), "Path=" + _desktop_string(work)]
    if icon:
        lines.append("Icon=" + _desktop_string(icon))
    lines += ["Terminal=false", "Categories=Game;", "Keywords=stalker;anomaly;gamma;", "StartupNotify=true",
              "StartupWMClass=" + WM_CLASS, "%s=%s" % (MARK, _desktop_string(work))]
    return "\n".join(lines) + "\n"


def _ours(path):
    """The install a .desktop file of ours starts, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r"^%s=(.*)$" % MARK, f.read(), re.M)
        return m.group(1).replace("\\\\", "\\") if m else None
    except OSError:
        return None


def desktop_file_name(work):
    return "umbra-%s.desktop" % hashlib.sha1(os.path.realpath(work).encode("utf-8")).hexdigest()[:8]


def _write(path, text, mode=0o755):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path + ".tmp", mode)
    os.replace(path + ".tmp", path)


def _trust(path, log):
    """GNOME's desktop only starts a .desktop file there once it is marked trusted."""
    if os.environ.get("UMBRA_SHORTCUTS_ROOT"):
        return                                    # tests: leave GNOME's metadata alone
    from .launcher import child_env               # (launcher imports install, which imports this)
    try:
        subprocess.run(["gio", "set", path, "metadata::trusted", "true"], env=child_env(host=True),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass                                      # no gio (not GNOME): nothing needs it


# ---------------------------------------------------------------- Windows: .lnk files
def _guid(text):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _method(obj, index, restype, *argtypes):
    """A COM method of obj by its place in the vtable."""
    import ctypes
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *a: fn(obj, *a)


class _ShellLink:
    """IShellLinkW and its IPersistFile (ShObjIdl.h's vtable order)."""
    CLSID = "00021401-0000-0000-C000-000000000046"
    IID_LINK = "000214F9-0000-0000-C000-000000000046"
    IID_FILE = "0000010B-0000-0000-C000-000000000046"

    def __enter__(self):
        import ctypes
        self.ct = ctypes
        ole32 = ctypes.windll.ole32
        self.inited = ole32.CoInitialize(None) in (0, 1)    # else COM is already up in another mode: fine
        self.link, self.file = ctypes.c_void_p(), ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(_guid(self.CLSID)), None, 1, ctypes.byref(_guid(self.IID_LINK)),
                                    ctypes.byref(self.link))
        if hr != 0:
            raise OSError("the shell's ShellLink is not available (0x%08x)" % (hr & 0xFFFFFFFF))
        _method(self.link, 0, ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)(
            ctypes.byref(_guid(self.IID_FILE)), ctypes.byref(self.file))
        return self

    def __exit__(self, *exc):
        ct = self.ct
        for obj in (self.file, self.link):
            if obj.value:
                _method(obj, 2, ct.c_ulong)()
        if self.inited:
            ct.windll.ole32.CoUninitialize()

    def set(self, index, *args):
        types = [self.ct.c_wchar_p if isinstance(a, str) else self.ct.c_int for a in args]
        _method(self.link, index, self.ct.HRESULT, *types)(*args)

    def save(self, path):
        _method(self.file, 6, self.ct.HRESULT, self.ct.c_wchar_p, self.ct.c_int)(path, 1)

    def load(self, path):
        _method(self.file, 5, self.ct.HRESULT, self.ct.c_wchar_p, self.ct.c_ulong)(path, 0)

    def path(self):
        buf = self.ct.create_unicode_buffer(1024)
        _method(self.link, 3, self.ct.HRESULT, self.ct.c_wchar_p, self.ct.c_int, self.ct.c_void_p,
                self.ct.c_ulong)(buf, 1024, None, 0)
        return buf.value


def write_lnk(path, target, workdir, icon=None, description=COMMENT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _ShellLink() as sl:
        sl.set(20, target)                      # SetPath
        sl.set(9, workdir)                      # SetWorkingDirectory
        sl.set(7, description)                  # SetDescription
        sl.set(17, icon or target, 0)           # SetIconLocation
        sl.save(path)


def read_lnk(path):
    """A .lnk file's target."""
    with _ShellLink() as sl:
        sl.load(path)
        return sl.path()


def _lnk_work(path):
    """The work folder of the launcher a .lnk starts (normcased), or None when it can't be read."""
    try:
        target = read_lnk(path)
    except OSError:
        return None
    return os.path.normcase(os.path.dirname(os.path.abspath(target))) if target else None


# ---------------------------------------------------------------- both
def make(work, launcher, icon=None, keep_only=None, log=None):
    """Put Umbra in the menu and on the desktop: {"menu": path, "desktop": path} of what is
    there now. keep_only: rewrite only these (paths made before that still exist), so a
    shortcut the player deleted stays deleted."""
    log = log or (lambda msg, level="info": None)
    out = {}
    for place, folder in places().items():
        if not folder:
            continue
        if os.name == "nt":
            path = os.path.join(folder, NAME + ".lnk")
            if os.path.exists(path) and _lnk_work(path) not in (None, os.path.normcase(os.path.abspath(work))):
                path = os.path.join(folder, "%s (%s).lnk" % (NAME, label(work)))   # another install's
        else:
            path = os.path.join(folder, desktop_file_name(work))
        if keep_only is not None and (path not in keep_only or not os.path.exists(path)):
            continue
        try:
            if os.name == "nt":
                write_lnk(path, launcher, work, icon=launcher)
            else:
                others = [f for f in os.listdir(folder) if f.startswith("umbra-") and f.endswith(".desktop")
                          and f != os.path.basename(path)] if os.path.isdir(folder) else []
                twin = any(_ours(os.path.join(folder, f)) not in (None, work) for f in others)
                _write(path, desktop_entry(work, launcher, icon, "%s (%s)" % (NAME, label(work)) if twin else NAME))
                if place == "desktop":
                    _trust(path, log)
            out[place] = path
        except OSError as e:
            log("Could not add Umbra to %s: %s" % ("the desktop" if place == "desktop" else where_words(), e), "warn")
    return out


def remove(paths):
    """Delete shortcuts made before (an uninstall). Returns what was removed."""
    gone = []
    for p in paths or []:
        try:
            os.remove(p)
            gone.append(p)
        except OSError:
            pass
    return gone
