#!/usr/bin/env python3
"""
Checks a built Umbra Setup program. build.sh runs it after each build; README.md here explains it.

    verify.py linux PROGRAM     --version, --selftest (its window opens for about 3 s) and a --plan run
                                against a throwaway pack made with tests/test_umbra.py's helpers
    verify.py appimage APPIMAGE the same as linux, then: the --plan run again from a copy of the
                                AppImage in the throwaway pack's installer/ folder with no --source
                                (Setup finds the pack it sits in), and --version without FUSE
                                (APPIMAGE_EXTRACT_AND_RUN=1, what players without FUSE use)
    verify.py windows EXE       --version, --selftest and the same --plan run under Wine
                                ($WINE, else "wine"; $WINEPREFIX)
    verify.py glibc PROGRAM     the newest glibc symbol version the Linux program and the libraries
                                bundled in it need (run it with the build venv's Python: it reads the
                                bundle with PyInstaller)

    --expect-version X          also check that --version says X

It prints one PASS or FAIL line per check and exits 1 if anything failed. Run it as a file (not
from stdin): the pack builder uses worker processes, and this machine's Python starts them with
forkserver, which imports this file again.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True  # leave no __pycache__ in other people's folders
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (": " + detail) if detail else ""), flush=True)
    return ok


def run(cmd, timeout=300, env=None):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        return None, (e.stdout or b"").decode("utf-8", "replace") + "\n(timed out after %d s)" % timeout
    return p.returncode, p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


NOISE = re.compile(r"^(ntsync: |WARNING: radv is not a conformant Vulkan)")  # Wine and Mesa, not the program


def indent(text, n=30):
    lines = [ln for ln in text.strip().splitlines() if not NOISE.match(ln)]
    if len(lines) > n:
        lines = lines[:n // 2] + ["  ..."] + lines[-n // 2:]
    return "\n".join("    | " + ln for ln in lines)


def mo2_open():
    """Is Mod Organizer 2 open on this machine? (Setup's own test: the plan stops then.)"""
    sys.path.insert(0, os.path.join(ROOT, "installer"))
    from umbra_installer import mo2
    return mo2.running()


# --------------------------------------------------------------------------- the checks
def t_version(cmd, expect, env=None):
    code, out = run(cmd + ["--version"], timeout=180, env=env)
    m = re.search(r"^Umbra Setup (\S+)$", out, re.M)
    ok = code == 0 and m is not None and (expect is None or m.group(1) == expect)
    check("--version", ok, m.group(0) if m else "exit %s, no version line" % code)
    if not ok:
        print(indent(out))


def t_selftest(cmd, env=None):
    code, out = run(cmd + ["--selftest", "--smoke", "3"], timeout=300, env=env)
    ok = code == 0 and "SELFTEST PASSED" in out
    window = re.search(r"^(PASS|FAIL) window.*$", out, re.M)
    check("--selftest --smoke 3", ok, window.group(0) if window else "exit %s" % code)
    print(indent(out))
    if ok and window and "skipped" in window.group(0):
        print("    (the window part was skipped: no display)")


def t_plan(cmd, to_path, mo2_expected_open, env=None, beside=False):
    """Build a throwaway pack and a fake G.A.M.M.A. install, then ask the program for its plan.
    beside: run a copy of the program put in the pack's installer/ folder, with no --source."""
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    import test_umbra as T  # noqa: E402 (also puts installer/ and dev/ on sys.path)

    base = tempfile.mkdtemp(prefix="verify-", dir=os.environ.get("UMBRA_VERIFY_TMP") or None)
    try:
        toml = T.make_source(base)
        dist = os.path.join(base, "dist")
        T.build(toml, dist)
        t_mo2, _ = T.make_target(os.path.join(base, "player"))
        source = ["--source", to_path(dist)]
        if beside:
            copy = os.path.join(dist, "installer", os.path.basename(cmd[-1]))
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            shutil.copy2(cmd[-1], copy)
            cmd, source = cmd[:-1] + [copy], []
        code, out = run(cmd + ["--add-to", to_path(t_mo2)] + source + ["--no-steam", "--plan"],
                        timeout=300, env=env)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    loaded = re.search(r"^Umbra 0\.0\.1: download .*$", out, re.M)
    problems = re.findall(r"^PROBLEM: .*$", out, re.M)
    steps = re.findall(r"^  step: ", out, re.M)
    if mo2_expected_open:
        ok = (code == 2 and loaded is not None and len(problems) == 1
              and problems[0].startswith("PROBLEM: Mod Organizer 2 is open"))
        detail = "pack read, stopped at %r as expected (MO2 is open here)" % (problems[0] if problems else None)
    else:
        ok = code == 0 and loaded is not None and not problems and len(steps) > 0
        detail = "pack read, %d steps planned" % len(steps)
    check("--add-to FAKE_GAMMA %s --no-steam --plan" % ("(in THROWAWAY_PACK/installer)" if beside
                                                           else "--source THROWAWAY_PACK"), ok,
          detail if ok else "exit %s" % code)
    print(indent(out))


def t_glibc(prog):
    from PyInstaller.archive.readers import CArchiveReader

    tmp = tempfile.mkdtemp(prefix="umbra-glibc-")
    try:
        files = [("(bootloader)", prog)]
        arch = CArchiveReader(prog)
        for name, entry in arch.toc.items():
            if entry[-1] == "b":
                data = arch.extract(name)
                if data[:4] == b"\x7fELF":
                    p = os.path.join(tmp, "%d.so" % len(files))
                    with open(p, "wb") as f:
                        f.write(data)
                    files.append((name, p))
        worst, need = (0,), {}
        for name, p in files:
            out = subprocess.run(["objdump", "-p", p], capture_output=True, text=True).stdout
            vers = [tuple(map(int, v.split("."))) for v in re.findall(r"\bGLIBC_(\d+(?:\.\d+)+)\b", out)]
            top = max(vers) if vers else (0,)
            need[name] = top
            worst = max(worst, top)
        print("    bundled ELF files: " + ", ".join(sorted(n for n, _ in files)))
        top_name = [n for n, v in need.items() if v == worst]
        check("glibc", worst <= (2, 17), "needs glibc %s (%s); release target is 2.17"
              % (".".join(map(str, worst)), ", ".join(top_name[:4])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["linux", "appimage", "windows", "glibc"])
    ap.add_argument("program")
    ap.add_argument("--expect-version")
    a = ap.parse_args()
    prog = os.path.abspath(a.program)
    if not os.path.isfile(prog):
        sys.exit("no such program: " + prog)
    print("== checking %s" % prog, flush=True)

    if a.what == "glibc":
        t_glibc(prog)
    elif a.what in ("linux", "appimage"):
        cmd = [prog]
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", prog]
        t_version([prog], a.expect_version)
        t_selftest(cmd)
        open_now = mo2_open()
        if open_now:
            print("    (Mod Organizer 2 is open on this machine, so the plan must stop at that)")
        t_plan([prog], lambda p: p, open_now)
        if a.what == "appimage":
            t_plan([prog], lambda p: p, open_now, beside=True)
            code, out = run([prog, "--version"], timeout=180,
                            env=dict(os.environ, APPIMAGE_EXTRACT_AND_RUN="1"))
            check("--version without FUSE (APPIMAGE_EXTRACT_AND_RUN=1)", code == 0 and "Umbra Setup " in out,
                  out.strip().splitlines()[-1] if out.strip() else "exit %s" % code)
    else:
        wine = os.environ.get("WINE") or shutil.which("wine") or "wine"
        env = dict(os.environ, WINEDEBUG=os.environ.get("WINEDEBUG", "-all"))
        cmd = [wine, prog]

        def to_path(p):
            code, out = run([wine, "winepath", "-w", p], timeout=120, env=env)
            return out.strip().splitlines()[-1] if code == 0 and out.strip() else p
        t_version(cmd, a.expect_version, env)
        t_selftest(cmd, env)
        # Wine's tasklist only sees programs in this Wine prefix, so MO2 counts as closed there
        t_plan(cmd, to_path, False, env)

    failed = results.count(False)
    print("== %s: %d checks, %s" % (os.path.basename(prog), len(results), "%d FAILED" % failed if failed else "all passed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
