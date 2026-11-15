"""
The game's runtime: the Microsoft DLLs Anomaly's engine loads that a computer
may not have. The engine imports msvcp140, vcruntime140(_1) and concrt140 (the
Visual C++ runtime), d3dcompiler_47, and d3dx9_43 / d3dx10_43 / d3dx11_43. Windows
has d3dcompiler_47 but not the rest unless something installed them; Proton's Wine
has stand-ins for all of them, which the engine does not start with (no log, no
window: it just closes).

Everything comes from Microsoft, pinned by sha256:
  vc_redist.x64.exe / vc_redist.x86.exe   the Visual C++ 2015-2022 runtime 14.44
  two Windows SDK cabinets                d3dcompiler_47 (64- and 32-bit)
  the Microsoft.DXSDK.D3DX NuGet package  d3dx9_43, d3dx10_43, d3dx11_43, d3dcompiler_43

  Windows  Setup runs Microsoft's Visual C++ installer when the runtime is older than
           VC_MIN (Windows asks for permission), and puts the D3DX DLLs next to the
           game's exe (Microsoft's way of shipping D3DX now; no permission needed).
  Linux    Setup downloads and unpacks the DLLs; the launcher, before it starts the
           game, puts them into the Proton prefix Steam made for Umbra's shortcut and
           tells Wine to use them (DLL overrides): what winetricks' vcrun2022,
           d3dcompiler_47, d3dx9, d3dx10 and d3dx11_43 do, without winetricks.
"""
import hashlib
import json
import os
import shutil
import struct
import zipfile

from . import cab, fetch

SET_ID = "1"            # this set of downloads; a new set unpacks into a new folder
VC_MIN = (14, 40)       # Visual C++ runtimes older than this break programs built with VS 2022 17.10+
VC_VERSION = (14, 44, 35211)

_MS = "https://download.microsoft.com/download/B/0/C/B0C80BA3-8AD6-4958-810B-6882485230B5/standalonesdk/Installers/"
_VS = "https://download.visualstudio.microsoft.com/download/pr/e9604e21-4b79-4096-a4b3-1b63d4b55b4d/"
DOWNLOADS = {
    "vc-x64": dict(url=_VS + "CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe",
                   sha256="cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b", size=25635768,
                   name="vc_redist.x64.exe"),
    "vc-x86": dict(url=_VS + "0C09F2611660441084CE0DF425C51C11E147E6447963C3690F97E0B25C55ED64/VC_redist.x86.exe",
                   sha256="0c09f2611660441084ce0df425c51c11e147e6447963c3690f97e0b25c55ed64", size=13953392,
                   name="vc_redist.x86.exe"),
    "d3dcompiler-x86": dict(url=_MS + "2630bae9681db6a9f6722366f47d055c.cab", size=1488323,
                            sha256="d0440eb81c532dc23639c0c63f2fcde9deddb23bb4cce01c19ac6b96cc3e269d",
                            name="d3dcompiler_47-x86.cab", member="fila319f706acfa16d6707473ebf29bdc7f"),
    "d3dcompiler-x64": dict(url=_MS + "61d57a7a82309cd161a854a6f4619e52.cab", size=1776364,
                            sha256="f736e161547095bb8d98c636b85fdfeb4070fefeee3c3745db3ce88f6eb1d9de",
                            name="d3dcompiler_47-x64.cab", member="fil3585cb2ea5db13cc0838f8d06b5c9679"),
    "d3dx": dict(url="https://api.nuget.org/v3-flatcontainer/microsoft.dxsdk.d3dx/9.29.952.8/"
                     "microsoft.dxsdk.d3dx.9.29.952.8.nupkg",
                 sha256="ead0906ae8a26c18a7525da7490127a2110f7c58f18293738283e30e97c6ea4b", size=9833468,
                 name="microsoft.dxsdk.d3dx.9.29.952.8.nupkg"),
}

VC_X86 = ["concrt140", "msvcp140", "msvcp140_1", "msvcp140_2", "msvcp140_atomic_wait", "msvcp140_codecvt_ids",
          "vcamp140", "vccorlib140", "vcomp140", "vcruntime140", "vcruntime140_threads", "mfc140", "mfc140chs",
          "mfc140cht", "mfc140deu", "mfc140enu", "mfc140esn", "mfc140fra", "mfc140ita", "mfc140jpn", "mfc140kor",
          "mfc140rus", "mfc140u", "mfcm140", "mfcm140u"]
VC_X64 = VC_X86 + ["vcruntime140_1"]
D3DX = ["d3dx9_43", "d3dx10_43", "d3dx11_43"]
# What goes where: the Proton prefix (Linux), next to the game's exe (Windows)
PREFIX = {"x64": VC_X64 + ["d3dcompiler_47"] + D3DX, "x86": VC_X86 + ["d3dcompiler_47"] + D3DX}
# the overrides a prefix must have (winetricks leaves out MFC and vcruntime140_threads,
# which the game doesn't load; Umbra sets those too)
OVERRIDES_NEEDED = sorted(set(VC_X64) - {"vcruntime140_threads"} - {n for n in VC_X64 if n.startswith("mfc")}
                          | {"d3dcompiler_47"} | set(D3DX))
APP_LOCAL = D3DX + ["d3dcompiler_43"]               # + d3dcompiler_47 where Windows lacks it
NEEDS = {"prefix": ["vc-x64", "vc-x86", "d3dcompiler-x64", "d3dcompiler-x86", "d3dx"],
         "windows": ["d3dx", "d3dcompiler-x64"]}
WINE_SIGS = (b"Wine builtin DLL", b"Wine placeholder DLL")
MACHINE = {0x8664: "x64", 0x14C: "x86"}


class RuntimeProblem(Exception):
    pass


def _quiet(msg, level="info"):
    pass


# ------------------------------------------------------------------ download and unpack
def folder(cache):
    return os.path.join(cache, "runtime", SET_ID)


def _manifest_path(cache, kind):
    return os.path.join(folder(cache), kind + ".json")


def prepared(cache, kind):
    """The unpacked files for kind ("prefix" / "windows") if they are all there and
    whole: {"x64/msvcp140.dll": sha256}; else None."""
    try:
        with open(_manifest_path(cache, kind), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    root = os.path.join(folder(cache), kind)
    for rel, size in m.get("sizes", {}).items():
        p = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(p) or os.path.getsize(p) != size:
            return None
    return m


def download_bytes(cache, kind):
    """What prepare() still has to download."""
    if prepared(cache, kind):
        return 0
    dl = os.path.join(folder(cache), "downloads")
    return sum(DOWNLOADS[k]["size"] for k in NEEDS[kind]
               if not _have(os.path.join(dl, DOWNLOADS[k]["name"]), DOWNLOADS[k]))


def _have(path, d):
    return os.path.isfile(path) and os.path.getsize(path) == d["size"] and _sha(path) == d["sha256"]


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _download(d, dst, progress=None, cancel=None):
    import urllib.request
    if _have(dst, d):
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    req = urllib.request.Request(d["url"], headers={"User-Agent": fetch.USER_AGENT})
    tmp = dst + ".part"
    got = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            if cancel and cancel():
                raise fetch.Cancelled()
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            if progress:
                progress(got)
    if os.path.getsize(tmp) != d["size"] or _sha(tmp) != d["sha256"]:
        os.unlink(tmp)
        raise RuntimeProblem("%s from %s is not the file Umbra expects (its sha256 differs); try again later"
                            % (d["name"], d["url"].split("/")[2]))
    os.replace(tmp, dst)
    return dst


def machine(data):
    """"x64" / "x86" for a PE file (a DLL or exe), else None."""
    if data[:2] != b"MZ" or len(data) < 0x40:
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return None
    return MACHINE.get(struct.unpack_from("<H", data, pe + 4)[0])


def file_version(data):
    """A PE file's version (its VS_FIXEDFILEINFO), as a tuple, or None."""
    i = data.find(struct.pack("<I", 0xFEEF04BD))
    if i < 0:
        return None
    ms, ls = struct.unpack_from("<II", data, i + 8)
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def vc_dlls(data, arch):
    """The Visual C++ runtime DLLs in a vc_redist bundle: {"msvcp140.dll": bytes}.
    The bundle keeps them in cabinets inside cabinets, named like msvcp140.dll_amd64."""
    suffix = "_amd64" if arch == "x64" else "_x86"
    out = {}
    for name, b in cab.walk(data):
        low = name.lower()
        if low.endswith(".dll" + suffix) and machine(b) == arch:
            out.setdefault(low[:-len(suffix)], b)
    return out


def _unpack(kind, paths):
    """{arch: {name.dll: bytes}} for kind, from the downloads (id -> path)."""
    def read(k):
        with open(paths[k], "rb") as f:
            return f.read()
    out = {"x64": {}, "x86": {}}
    if kind == "prefix":
        for arch in ("x64", "x86"):
            out[arch].update(vc_dlls(read("vc-" + arch), arch))
    for arch in (("x64", "x86") if kind == "prefix" else ("x64",)):
        d = DOWNLOADS["d3dcompiler-" + arch]
        c = cab.Cab(read("d3dcompiler-" + arch))
        out[arch]["d3dcompiler_47.dll"] = c.read(d["member"])
    with zipfile.ZipFile(paths["d3dx"]) as z:
        names = {n.lower(): n for n in z.namelist()}
        for arch in (("x64", "x86") if kind == "prefix" else ("x64",)):
            for dll in D3DX + (["d3dcompiler_43"] if kind == "windows" else []):
                key = "build/native/release/bin/%s/%s.dll" % (arch, dll)
                if key not in names:
                    raise RuntimeProblem("the D3DX package has no %s" % key)
                out[arch][dll + ".dll"] = z.read(names[key])
    want = PREFIX if kind == "prefix" else {"x64": APP_LOCAL + ["d3dcompiler_47"]}
    for arch, dlls in want.items():
        for dll in dlls:
            b = out[arch].get(dll + ".dll")
            if b is None or machine(b) != arch:
                raise RuntimeProblem("%s (%s) is not in Microsoft's downloads as expected" % (dll, arch))
    return {a: v for a, v in out.items() if v}


def prepare(cache, kind, log=None, progress=None, cancel=None):
    """Download (what isn't here yet) and unpack the files for kind: "prefix" (Linux:
    everything, 64- and 32-bit) or "windows" (the D3DX DLLs and d3dcompiler, 64-bit).
    Returns the manifest (prepared()). progress(done_bytes, total_bytes)."""
    log = log or _quiet
    m = prepared(cache, kind)
    if m:
        return m
    dl = os.path.join(folder(cache), "downloads")
    total = download_bytes(cache, kind)
    done = [0]
    paths = {}
    for k in NEEDS[kind]:
        d = DOWNLOADS[k]
        dst = os.path.join(dl, d["name"])
        if not _have(dst, d):
            log("Downloading %s from %s" % (d["name"], d["url"].split("/")[2]))
            base = done[0]
            _download(d, dst, (lambda n, _b=base: progress and progress(_b + n, total)), cancel)
            done[0] = base + d["size"]
        paths[k] = dst
    files = _unpack(kind, paths)
    root = os.path.join(folder(cache), kind)
    if os.path.isdir(root):
        shutil.rmtree(root)
    sizes, shas = {}, {}
    for arch, dlls in files.items():
        os.makedirs(os.path.join(root, arch), exist_ok=True)
        for name, b in sorted(dlls.items()):
            with open(os.path.join(root, arch, name), "wb") as f:
                f.write(b)
            sizes[arch + "/" + name] = len(b)
            shas[arch + "/" + name] = hashlib.sha256(b).hexdigest()
    m = {"set": SET_ID, "sizes": sizes, "sha256": shas}
    tmp = _manifest_path(cache, kind) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, sort_keys=True)
    os.replace(tmp, _manifest_path(cache, kind))
    # the downloads aren't needed any more; Windows keeps the Visual C++ installer it may run
    for k in NEEDS[kind]:
        try:
            os.remove(paths[k])
        except OSError:
            pass
    log("%s are ready (%d files)" % ("Microsoft's Visual C++ runtime and DirectX files", len(sizes)))
    return m


def file_path(cache, kind, rel):
    return os.path.join(folder(cache), kind, *rel.split("/"))


# ------------------------------------------------------------------ Linux: Steam's Proton prefix
def prefix_paths(compat):
    pfx = os.path.join(compat, "pfx")
    win = os.path.join(pfx, "drive_c", "windows")
    return pfx, {"x64": os.path.join(win, "system32"), "x86": os.path.join(win, "syswow64")}


def wine_builtin(path):
    """Is this one of Wine's own DLLs (a stand-in), not Microsoft's?"""
    try:
        with open(path, "rb") as f:
            f.seek(0x40)
            head = f.read(24)
    except OSError:
        return True
    return head.startswith(WINE_SIGS)


def dll_overrides(pfx):
    """The prefix's DLL overrides as Wine saved them: {"d3dx9_43": "native"} (lower case,
    without the "*")."""
    out = {}
    try:
        with open(os.path.join(pfx, "user.reg"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    inside = False
    for line in text.splitlines():
        if line.startswith("["):
            inside = line.split("]")[0] == "[Software\\\\Wine\\\\DllOverrides"
            continue
        if inside and line.startswith('"') and '"="' in line:
            k, _, v = line.partition('"="')
            out[k[1:].lstrip("*").lower()] = v.rstrip('"').lower()
    return out


def prefix_missing(compat):
    """What the prefix still lacks, in words ([] when it is ready to start the game)."""
    pfx, dirs = prefix_paths(compat)
    if not os.path.isfile(os.path.join(pfx, "system.reg")):
        return ["Proton has not made the prefix yet"]
    out = []
    lacking = sorted({n for arch, names in PREFIX.items() for n in names
                      if wine_builtin(os.path.join(dirs[arch], n + ".dll"))})
    if lacking:
        out.append("Wine's stand-ins instead of Microsoft's %s" % ", ".join(lacking))
    ov = dll_overrides(pfx)
    unset = [n for n in OVERRIDES_NEEDED if not ov.get(n, "").startswith("native")]
    if unset:
        out.append("no DLL override for %s" % ", ".join(unset))
    return out


def overrides_reg():
    """A .reg file (as Wine's reg.exe imports it) that makes Wine use Microsoft's DLLs."""
    names = sorted({n for names in PREFIX.values() for n in names})
    lines = ["Windows Registry Editor Version 5.00", "", "[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]"]
    lines += ['"*%s"="native,builtin"' % n for n in names]
    return ("\r\n".join(lines) + "\r\n").encode("utf-16")


REG_FILE = "umbra-runtime.reg"
CMD = "C:\\windows\\system32\\cmd.exe"
REG = "C:\\windows\\system32\\reg.exe"


def prefix_install(compat, cache, run, log=None):
    """Put the runtime into Steam's Proton prefix for the shortcut (compat:
    STEAM_COMPAT_DATA_PATH). run(args) runs a Windows command in that prefix
    through Steam's own Proton command and returns its exit code. The files must
    be prepare()d ("prefix")."""
    log = log or _quiet
    m = prepared(cache, "prefix")
    if not m:
        raise RuntimeProblem("the runtime files are not downloaded yet")
    pfx, dirs = prefix_paths(compat)
    if not os.path.isfile(os.path.join(pfx, "system.reg")):
        log("Proton makes the prefix for Umbra (the first start only)")
        code = run([CMD, "/c", "exit"])
        if not os.path.isfile(os.path.join(pfx, "system.reg")):
            raise RuntimeProblem("Proton did not make its prefix in %s (exit code %s)" % (compat, code))
    n = 0
    for rel, sha in sorted(m["sha256"].items()):
        arch, name = rel.split("/")
        dst = os.path.join(dirs[arch], name)
        if os.path.isfile(dst) and not wine_builtin(dst) and _sha(dst) == sha:
            continue
        os.makedirs(dirs[arch], exist_ok=True)
        tmp = dst + ".umbra-new"
        shutil.copyfile(file_path(cache, "prefix", rel), tmp)
        os.replace(tmp, dst)            # Wine's stand-in was a file of its own: replaced, not changed
        n += 1
    log("Put %d of Microsoft's DLLs into Proton's prefix" % n)
    reg = os.path.join(pfx, "drive_c", REG_FILE)
    with open(reg, "wb") as f:
        f.write(overrides_reg())
    try:
        code = run([REG, "import", "C:\\" + REG_FILE])
    finally:
        try:
            os.remove(reg)
        except OSError:
            pass
    if code != 0:
        raise RuntimeProblem("Wine's reg.exe could not set the DLL overrides (exit code %s)" % code)
    log("Wine now uses Microsoft's Visual C++ runtime and DirectX DLLs for Umbra")


# ------------------------------------------------------------------ Windows
def vc_installed(arch="x64"):
    """The Visual C++ 2015-2022 runtime Windows has, (major, minor, build), or None."""
    try:
        import winreg
    except ImportError:
        return None
    key = "SOFTWARE\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\" + arch
    for view in (getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | view) as k:
                if int(winreg.QueryValueEx(k, "Installed")[0]) != 1:
                    continue
                return tuple(int(winreg.QueryValueEx(k, n)[0]) for n in ("Major", "Minor", "Bld"))
        except (OSError, ValueError):
            continue
    return None


def system_dir():
    return os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")


def app_local(anomaly):
    """The DLLs that go next to the game's exe on Windows: [name.dll]."""
    names = [n + ".dll" for n in APP_LOCAL]
    if not os.path.isfile(os.path.join(system_dir(), "d3dcompiler_47.dll")):
        names.append("d3dcompiler_47.dll")
    return names


def windows_missing(anomaly, cache=None, vc=None):
    """What Windows lacks for the game, in words ([] when ready). vc: the installed
    Visual C++ version (looked up when not given)."""
    out = []
    vc = vc_installed() if vc is None else vc
    if not vc or tuple(vc[:2]) < VC_MIN:
        out.append("Microsoft's Visual C++ runtime %s or newer (%s)"
                   % (".".join(map(str, VC_MIN)), "installed: %s" % ".".join(map(str, vc)) if vc else "not installed"))
    bin_dir = os.path.join(anomaly, "bin")
    lacking = [n for n in app_local(anomaly) if not os.path.isfile(os.path.join(bin_dir, n))]
    if lacking:
        out.append("the DirectX files %s next to the game" % ", ".join(lacking))
    return out


ERROR_CANCELLED = 1223
VC_OK = {0: None, 1638: "a newer one is installed", 3010: "Windows wants a restart to finish it",
         1641: "Windows is restarting to finish it"}


def run_elevated(exe, params, cwd=None):
    """Run exe with administrator rights (Windows asks the user); returns its exit code.
    Raises PermissionError when the user says no."""
    import ctypes
    from ctypes import wintypes

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SEI)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    sei = SEI()
    sei.cbSize = ctypes.sizeof(SEI)
    sei.fMask = 0x40 | 0x100                    # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.lpVerb, sei.lpFile, sei.lpParameters, sei.lpDirectory, sei.nShow = "runas", exe, params, cwd, 1
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise PermissionError("declined")
        raise OSError(err, "could not start %s" % os.path.basename(exe))
    try:
        kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(sei.hProcess)


def windows_install(cache, anomaly, log=None, progress=None, cancel=None, vc=None, elevate=None):
    """Make Windows ready for the game: Microsoft's Visual C++ installer when the runtime
    is missing or too old (Windows asks for permission), the D3DX DLLs next to the
    game's exe. vc / elevate: for tests (the installed version; run_elevated)."""
    log = log or _quiet
    elevate = elevate or run_elevated
    vc = vc_installed() if vc is None else vc
    if not vc or tuple(vc[:2]) < VC_MIN:
        d = DOWNLOADS["vc-x64"]
        exe = os.path.join(folder(cache), "downloads", d["name"])
        if not _have(exe, d):
            log("Downloading Microsoft's Visual C++ runtime %s" % ".".join(map(str, VC_VERSION)))
            _download(d, exe, (lambda n: progress and progress(n, d["size"])), cancel)
        log("Installing Microsoft's Visual C++ runtime: Windows asks for permission")
        try:
            code = elevate(exe, "/install /quiet /norestart", os.path.dirname(exe))
        except PermissionError:
            raise RuntimeProblem("Windows did not get permission to install Microsoft's Visual C++ runtime, "
                                "which the game needs. Try again and answer Yes.")
        if code not in VC_OK:
            raise RuntimeProblem("Microsoft's Visual C++ installer failed (exit code %d)" % code)
        log("Microsoft's Visual C++ runtime is installed" + (" (%s)" % VC_OK[code] if VC_OK[code] else ""))
        try:
            os.remove(exe)
        except OSError:
            pass
    m = prepare(cache, "windows", log, progress, cancel)
    bin_dir = os.path.join(anomaly, "bin")
    n = 0
    for name in app_local(anomaly):
        rel = "x64/" + name
        dst = os.path.join(bin_dir, name)
        if os.path.isfile(dst) and _sha(dst) == m["sha256"][rel]:
            continue
        tmp = dst + ".umbra-new"
        shutil.copyfile(file_path(cache, "windows", rel), tmp)
        os.replace(tmp, dst)
        n += 1
    if n:
        log("Put %d DirectX file(s) next to the game (Microsoft's D3DX)" % n)
