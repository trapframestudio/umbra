#!/usr/bin/env python3
"""
Umbra Setup: installs Umbra (G.A.M.M.A. + the Umbra layer) on Windows and Linux.

    umbra-setup                         the window
    umbra-setup --install-to DIR        install G.A.M.M.A. and Umbra into an empty folder
    umbra-setup --add-to MO2_DIR        add Umbra to a G.A.M.M.A. install you already have
      --source URL|DIR                  where the pack is published (built in for releases)
      --channel stable|testing          --no-steam     --proton NAME    --yes (don't ask)
      --no-shortcuts                    no Start menu / applications menu entry or desktop shortcut
      --with OPTION / --without OPTION  optional parts (see --plan for this pack's)
      --with OPTION=CHOICE              pick one, e.g. --with ccon-season=winter
                                        (a re-run keeps the last install's picks unless told)
      --no-profile-shortcuts            no Steam shortcut for extra profiles (if the pack has any)
      --repair                          also reinstall any Umbra mod whose folder lost files
    umbra-setup --plan ...              only say what would happen
    umbra-setup --selftest              check that this program works here
    umbra-setup --launcher [--work DIR] [-- STEAM_COMMAND...]
                                        the Umbra launcher (the program also opens as the launcher
                                        when it is named "Umbra Launcher.exe" / "umbra-launcher",
                                        as Setup installs it; umbra_installer/launcher.py says how)
"""
import argparse
import os
import sys
import threading

FROZEN = getattr(sys, "frozen", False)
if not FROZEN:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from umbra_installer import VERSION, install, packdef  # noqa: E402


def apply_picks(pack, start, with_, without):
    """--with / --without on top of the picks the install starts from (raises ValueError)."""
    opts = {o["id"]: o for o in pack.get("options", [])}
    have = ", ".join(("%s=%s" % (o["id"], "|".join(c["id"] for c in o["choices"]))) if o.get("choices") else o["id"]
                     for o in opts.values()) or "none"
    out = set(start)
    for x in with_:
        oid, _, cid = x.partition("=")
        o = opts.get(oid)
        if not o or bool(cid) != bool(o.get("choices")) or (cid and cid not in [c["id"] for c in o["choices"]]):
            raise ValueError("no such option: %s (this pack has: %s)" % (x, have))
        if cid:
            out = {k for k in out if not k.startswith(oid + "=")}
        out.add(x)
    for x in without:
        o = opts.get(x)
        if not o or o.get("choices"):
            raise ValueError("--without takes a tick-box option, not %s (this pack has: %s)" % (x, have))
        out.discard(x)
    return out


def built_in_source():
    if os.environ.get("UMBRA_SOURCE"):
        return os.environ["UMBRA_SOURCE"]
    try:
        from umbra_installer import build_info
        if build_info.SOURCE:
            return build_info.SOURCE
    except ImportError:
        pass
    return beside_program()


def own_file():
    """Where this program really is: the AppImage itself when it runs from one (the program
    inside runs from a temporary mount of it), else the program's own file."""
    appdir, appimage = os.environ.get("APPDIR"), os.environ.get("APPIMAGE")
    if appdir and appimage and os.path.abspath(sys.executable).startswith(os.path.abspath(appdir) + os.sep):
        return appimage
    return sys.executable


def beside_program():
    """A pack built on this machine, when the program sits in its installer/ folder (where
    dev/build_installer puts it: <pack>/installer/Umbra-Setup-x86_64.AppImage)."""
    if not FROZEN:
        return None
    d = os.path.dirname(os.path.dirname(os.path.abspath(own_file())))
    ch = os.path.join(d, "channels")
    return d if os.path.isdir(ch) and any(n.endswith(".json") for n in os.listdir(ch)) else None


def no_xcursor():
    """Linux program only: the bundled Tk has its own libX11, which would load
    the system's libXcursor (and with it a second libX11) for the first text
    box's cursor and abort. An empty stand-in loaded first prevents that
    (dev/build_installer/nocursor.c)."""
    if not (FROZEN and sys.platform.startswith("linux")):
        return
    stub = os.path.join(getattr(sys, "_MEIPASS", ""), "libXcursor.so.1")
    if os.path.isfile(stub):
        try:
            import ctypes
            ctypes.CDLL(stub)
        except OSError:
            pass


def parse(argv):
    ap = argparse.ArgumentParser(prog="umbra-setup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install-to", metavar="DIR")
    g.add_argument("--add-to", metavar="MO2_DIR")
    g.add_argument("--selftest", action="store_true")
    g.add_argument("--version", action="store_true")
    ap.add_argument("--source", default=None)
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--no-steam", action="store_true")
    ap.add_argument("--proton", default=None, help="Linux: the Proton the Steam shortcut uses")
    ap.add_argument("--gamma-cache", default=None,
                    help="reuse a stalker-gamma-cli cache folder (new installs; the window finds yours by itself)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-mirror", action="store_true", help="download Anomaly from ModDB only")
    ap.add_argument("--with", dest="with_", action="append", default=[], metavar="OPTION",
                    help="install an optional part (e.g. hd-characters)")
    ap.add_argument("--without", action="append", default=[], metavar="OPTION")
    ap.add_argument("--no-shortcuts", action="store_true",
                    help="no Start menu / applications menu entry or desktop shortcut (they start Umbra "
                         "without Steam)")
    ap.add_argument("--no-profile-shortcuts", action="store_true",
                    help="no Steam shortcut for extra profiles (if the pack has any)")
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--launcher", action="store_true")
    ap.add_argument("--work", default=None, help="the launcher: the install's .umbra folder")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--smoke", type=float, default=None, help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def cli(a):
    opts = install.Options(
        mode="new" if a.install_to else "existing", root=a.install_to, mo2_dir=a.add_to,
        source=a.source or built_in_source(), channel=a.channel, steam=not a.no_steam, compat_tool=a.proton,
        threads=a.threads, mirror=not a.no_mirror, gamma_cache=a.gamma_cache,
        profile_shortcuts=not a.no_profile_shortcuts, repair=a.repair, shortcuts=not a.no_shortcuts)
    last = {}

    def log(msg, level="info"):
        if level == "debug":
            return
        print(("%s: " % level.upper() if level in ("warn", "error") else "") + msg, flush=True)

    def progress(step, frac, msg=""):
        key = (step, int(frac * 20))
        if last.get("k") != key:
            last["k"] = key
            print("  [%s] %3d%% %s" % (step, int(frac * 100), msg), flush=True)

    cancel = threading.Event()
    inst = install.Installer(opts, log, progress, cancel)
    plan = inst.plan()
    if plan.pack and (a.with_ or a.without):
        try:
            opts.choices = apply_picks(plan.pack, plan.choices, a.with_, a.without)
        except ValueError as e:
            print("PROBLEM: %s" % e)
            return 2
        plan = inst.plan(pack=plan.pack)
    if plan.pack:
        for o in plan.pack.get("options", []):
            if o.get("choices"):
                print("  option %-16s %-6s %s (pick one: %s)" % (
                    o["id"], packdef.picked(plan.pack, plan.choices, o["id"]), o.get("title", ""),
                    ", ".join("%s +%s" % (c["id"], packdef.human(packdef.option_bytes(
                        plan.pack, packdef.choice_key(o["id"], c["id"])))) for c in o["choices"])))
                continue
            print("  option %-16s %s  %s (+%s)" % (o["id"], "ON " if o["id"] in plan.choices else "off",
                                                   o.get("title", ""),
                                                   packdef.human(packdef.option_bytes(plan.pack, o["id"]))))
    if plan.pack:
        for pr in packdef.chosen_profiles(plan.pack, plan.choices):
            print('  MO2 profile "%s"%s' % (pr["name"], (", Steam shortcut \"%s\"" % pr["steam"]["title"])
                                            if pr.get("steam") and opts.steam and opts.profile_shortcuts else ""))
        print("Umbra %s: download %s, needs about %s free (%s free)" % (
            plan.pack["version"], packdef.human(packdef.total_download(plan.pack, plan.choices)),
            packdef.human(plan.need_bytes),
            packdef.human(plan.free_bytes)))
    for w in plan.warnings:
        print("WARNING: " + w)
    for p in plan.problems:
        print("PROBLEM: " + p)
    if not plan.ok():
        return 2
    if a.plan:
        for s in plan.steps:
            print("  step: " + s[1])
        return 0
    if not a.yes:
        try:
            if input("Install? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        except EOFError:
            return 1
    try:
        inst.run(plan)
    except KeyboardInterrupt:
        cancel.set()
        print("Cancelled. Run it again to continue where it stopped.")
        return 3
    except Exception as e:  # noqa: BLE001
        print("ERROR: %s" % e)
        return 2
    return 0


def selftest(a):
    import platform
    import traceback
    fails = []

    def step(name, fn):
        try:
            r = fn()
            print("PASS %s%s" % (name, (": %s" % r) if r else ""), flush=True)
        except Exception as e:  # noqa: BLE001
            fails.append(name)
            print("FAIL %s: %s: %s" % (name, type(e).__name__, e), flush=True)
            traceback.print_exc()

    print("Umbra Setup %s self-test: %s %s, Python %s, %s" % (VERSION, platform.system(), platform.release(),
                                                              platform.python_version(),
                                                              "frozen" if FROZEN else sys.executable))

    def t_https():
        import ssl
        import urllib.request
        ctx = ssl.create_default_context()
        return "TLS ok (%s), urllib %s" % (ssl.OPENSSL_VERSION.split()[1], bool(ctx) and urllib.request.__name__)
    step("https", t_https)

    def t_steam():
        from umbra_installer import steam
        roots = steam.steam_roots()
        return "%d Steam install%s found" % (len(roots), "" if len(roots) == 1 else "s")
    step("steam", t_steam)

    def t_assets():
        missing = [n for n in ("umbra-logo-1280.png", "umbra-icon-256.png") if not install.asset_path(n)]
        if missing and FROZEN:
            raise RuntimeError("missing: " + ", ".join(missing))
        return "logo and icon" if not missing else "not bundled (source run)"
    step("assets", t_assets)

    def t_fonts():
        from umbra_installer import ttf, typeface
        d = typeface.font_dir()
        if not d:
            raise RuntimeError("the fonts are not bundled (assets/fonts)")
        for fn in sorted(set(typeface.FILES.values())):
            w, h, cov = ttf.render(ttf.Font(os.path.join(d, fn)), "Umbra Тінь", 20)
            if not any(cov):
                raise RuntimeError("%s draws nothing" % fn)
        return "Tektur Narrow and Play read and drawn"
    step("fonts", t_fonts)

    def t_window():
        try:
            import tkinter as tk
        except ImportError as e:
            if FROZEN:
                raise
            return "skipped (no tkinter: %s)" % e
        try:
            r = tk.Tk()
        except tk.TclError as e:
            if os.name == "nt":
                raise
            return "skipped (no display: %s)" % e
        r.destroy()
        from umbra_installer import gui
        gui.main(source=a.source or built_in_source(), smoke=a.smoke or 3.0)
        return "window opened and closed (Tk %s)" % tk.TkVersion
    step("window", t_window)

    def t_runtime():
        from umbra_installer import cab, gammacli, runtime
        files = {"a.dll": b"Umbra" * 20000, "b.dll": bytes(range(256)) * 300}
        c = cab.Cab(cab.build(files))
        if [c.read(n) for n in files] != list(files.values()):
            raise RuntimeError("a cabinet did not unpack to what went in")
        if os.name == "nt":
            vc = runtime.vc_installed()
            return "cabinets unpack; Visual C++ runtime %s" % (".".join(map(str, vc)) if vc else "not installed")
        return "cabinets unpack; ICU %s" % ("found" if gammacli.has_icu() else "not found (the CLI does without)")
    step("runtime", t_runtime)

    def t_shortcuts():
        """The Start menu / desktop shortcut: Windows' shell makes one (in a temporary folder) and
        reads its target back; on Linux, where the menu entry would go."""
        import shutil
        import tempfile
        from umbra_installer import shortcuts
        target = os.path.abspath(sys.executable if FROZEN else __file__)
        if os.name != "nt":
            pl = shortcuts.places()
            return "menu entry in %s; desktop %s" % (pl["menu"], pl["desktop"] or "none")
        d = tempfile.mkdtemp(prefix="umbra-selftest-")
        try:
            lnk = os.path.join(d, "Umbra test.lnk")
            shortcuts.write_lnk(lnk, target, d)
            got = shortcuts.read_lnk(lnk)
            same = os.path.normcase(os.path.abspath(got)) == os.path.normcase(target)
            if not same:
                try:
                    same = os.path.samefile(got, target)
                except OSError:
                    pass
            if not same:
                raise RuntimeError("the shortcut points at %r, not %r" % (got, target))
            return "the shell makes a shortcut and reads it back (%s)" % os.path.basename(got)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    step("shortcuts", t_shortcuts)

    def t_launcher():
        try:
            import tkinter as tk
            tk.Tk().destroy()
        except Exception as e:  # noqa: BLE001
            if FROZEN and os.name == "nt":
                raise
            return "skipped (%s)" % e
        from umbra_installer import launcher_gui
        launcher_gui.main(work=a.work, source=a.source or built_in_source(), smoke=a.smoke or 3.0)
        return "launcher window opened and closed"
    step("launcher", t_launcher)
    print("SELFTEST %s" % ("FAILED: " + ", ".join(fails) if fails else "PASSED"))
    return 1 if fails else 0


def is_launcher():
    """Installed as the launcher (install.LAUNCHER), this program opens as the launcher."""
    me = os.path.basename(sys.executable if FROZEN else sys.argv[0]).lower()
    return me.startswith(("umbra launcher", "umbra-launcher"))


def main(argv=None):
    from umbra_installer import launcher
    own, steam_cmd = launcher.split_argv(sys.argv[1:] if argv is None else argv)
    a = parse(own)
    if (a.launcher or is_launcher()) and not (a.version or a.selftest):
        try:
            from umbra_installer import launcher_gui
        except ImportError as e:
            print("The launcher needs tkinter (%s)." % e)
            return 2
        return launcher_gui.main(work=a.work, steam_cmd=steam_cmd, source=a.source or built_in_source(),
                                 smoke=a.smoke)
    if a.version:
        print("Umbra Setup %s" % VERSION)
        return 0
    if a.selftest:
        return selftest(a)
    if a.install_to or a.add_to:
        return cli(a)
    try:
        from umbra_installer import gui
    except ImportError as e:
        print("The window needs tkinter (%s). Without it: umbra-setup --install-to DIR / --add-to MO2_DIR" % e)
        return 2
    return gui.main(source=a.source or built_in_source(), channel=a.channel, smoke=a.smoke, gamma_cache=a.gamma_cache)


if __name__ == "__main__":
    if FROZEN:
        no_xcursor()
    sys.exit(main())
