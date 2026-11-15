# Building Umbra Setup

This folder turns `installer/umbra_setup.py` and `installer/umbra_installer/` into single-file
programs, ready to publish:

| Output (`dist/installer/`) | What |
| --- | --- |
| `Umbra-Setup-x86_64.AppImage` | **What Linux players download.** `umbra-setup-linux`, unchanged, in an AppImage: it shows the Umbra icon in file managers, and AppImage tools (Gear Lever, AppImageLauncher) add it to the menu. The type 2 runtime is static, so players need no `libfuse2`; without FUSE at all (some containers) it runs with `APPIMAGE_EXTRACT_AND_RUN=1`. |
| `umbra-setup-linux` | Linux x86_64. Runs on glibc 2.17 and later (x86_64 distros from about 2014 on, SteamOS included). The program inside the AppImage, and what Setup installs as the launcher (`.umbra/Umbra Launcher`), so the launcher needs no FUSE. It also runs on its own (`chmod +x`). |
| `Umbra Setup.exe` | Windows x64 (Windows 8.1 and later, the floor of Python 3.12). A window program with the Umbra icon and version info. |
| `SHA256SUMS.txt` | The sha256 of each program, for the release page. |

Players need nothing else: Python, Tk and the logo are inside. Rebuild whenever the installer code changes,
because each program carries a copy of it.

## Building

    dev/build_installer/build.sh linux      # dist/installer/umbra-setup-linux and the AppImage
    dev/build_installer/build.sh appimage   # only the AppImage again, from umbra-setup-linux as it is
    dev/build_installer/build.sh windows    # dist/installer/Umbra Setup.exe (under Wine)
    dev/build_installer/build.sh all        # all three
    dev/build_installer/build.sh verify     # check the programs in dist/installer again

    UMBRA_SOURCE=https://example.org/umbra dev/build_installer/build.sh all
                                            # build the pack URL in (umbra_installer.build_info.SOURCE)

Without `UMBRA_SOURCE`, the programs have no built-in pack URL, so players have to pass `--source`.
Release builds should set it.

Each build:

1. copies `installer/` into a fresh staging folder (`~/.cache/umbra-build/stage.XXXXXX`, removed
   when build.sh exits), writes `umbra_installer/build_info.py` there (`SOURCE = "<UMBRA_SOURCE>"`),
   and checks that every file compiles. The source tree is never written to. The staging copy is a
   snapshot, so edits made while a build runs don't end up half in it, and `all` builds both
   programs from the same snapshot;
2. downloads what it needs, checks each sha256 against its pin, and stops on a mismatch;
3. runs PyInstaller with `umbra_setup.spec`;
4. copies the program to `dist/installer/`, updates `SHA256SUMS.txt`, and runs `verify.py`;
5. on Linux, puts that program in an AppImage (`AppRun`, `umbra-setup.desktop`, the 256 px icon) with
   the pinned `appimagetool` and runtime, and checks it the same way.

The build needs bash, curl, python3 (3.11 or later, for verify.py and the test pack), and a C compiler
(`cc`, for the Linux program). The Windows build also needs Wine (see below). `verify.py` opens the
window for about 3 seconds, so build on a desktop, or install `xvfb-run` when there is no display.

### Files

| File | What |
| --- | --- |
| `build.sh` | the build (pins for the two Pythons, appimagetool and the AppImage runtime are at the top) |
| `requirements.txt` | the build tools, each wheel pinned by sha256 |
| `umbra_setup.spec` | PyInstaller: one file, the assets, the cursor stand-in, the runtime hook, the Windows icon (made from `assets/umbra-icon-{256,64}.png`) and version info |
| `rthook_umbra.py` | runs inside the program before `umbra_setup.py` (see the gotchas) |
| `nocursor.c` | the empty `libXcursor.so.1` stand-in for Linux (see the gotchas) |
| `verify.py` | the checks run after each build |

## What is pinned

| Input | Pin |
| --- | --- |
| Linux Python | python-build-standalone 20250409 `cpython-3.12.10+20250409-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz`, sha256 `8c59b9ac6bff2dc3934181d7bc82594f9f59a613afed8d72c9e89d7194e790ee`. It is the stripped build: same Python, no debug info (the unstripped `libpython` is 213 MB and made a 62 MB program; stripped, it is 17 MB). glibc 2.17 baseline. |
| Windows Python | python.org `python-3.12.10-amd64.exe` (the last 3.12 with a Windows installer), sha256 `67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb` |
| AppImage | `appimagetool` 1.9.1 (`appimagetool-x86_64.AppImage`, sha256 `ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0`; run unpacked with `APPIMAGE_EXTRACT_AND_RUN=1`, so building needs no FUSE) and the type 2 runtime 20251108 (`runtime-x86_64`, sha256 `2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d`), passed with `--runtime-file` so appimagetool downloads nothing. |
| Build tools | `requirements.txt`: PyInstaller 6.22.3, pyinstaller-hooks-contrib 2026.9, altgraph 0.17.5, packaging 26.3, setuptools 84.2.1, and on Windows pefile 2024.8.26 and pywin32-ctypes 0.2.3. pip installs them with `--require-hashes --only-binary :all:`, so no wheel outside the pins gets in and nothing is compiled. |

Both Python hashes match the published ones: the release's `SHA256SUMS` for python-build-standalone, and
the SPDX file next to the installer on python.org.

To move to a new pin, change the file name, URL and sha256 in `build.sh` (or the version and every
`--hash` in `requirements.txt`, one per wheel: PyInstaller has a Linux and a Windows wheel), then delete the
old download from `~/.cache/umbra-build/downloads`.

## The two gotchas

### 1. The cursor library (Linux)

python-build-standalone's tkinter has its own libX11 built in. For a named cursor (for example the I-beam
over every text box), that libX11 dlopens the system's `libXcursor.so.1`, which brings in the system's
libX11. Two copies of libX11 then drive one display connection, and X aborts with `[xcb] Unknown sequence
number while appending request` as soon as the window builds its first text box. This happens on
Fedora 44. Older systems can be lucky, but no player should depend on luck.

The fix: `build.sh` compiles `nocursor.c` (empty, no libc) into a `libXcursor.so.1` stand-in with that
SONAME, the spec bundles it at the top of the program's folder, and `umbra_setup.no_xcursor()` loads it
from `sys._MEIPASS` before Tk starts. libX11's dlopen then gets the stand-in, finds no Xcursor
functions, and uses the classic X cursors. That's why the Linux build needs `cc`. Running the `.py`
with a distro's Python is not affected, because that tkinter uses the system libX11.

The stand-in brings a second problem. PyInstaller's one-file bootloader puts the program's folder first
on `LD_LIBRARY_PATH`, and child processes inherit that. Every program Umbra Setup starts
(`xdg-open` and the file manager behind it, stalker-gamma-cli) would then load the empty stand-in in
place of the real libXcursor. GTK 3, for one, then refuses to load (`undefined symbol:
XcursorShapeLoadImages`). So `rthook_umbra.py` puts the player's own `LD_LIBRARY_PATH` back as soon as
the program starts. The program itself is not affected, because the dynamic loader read the variable at
startup.

Why not build with the system Python: a program frozen with Fedora's Python needs Fedora's glibc (2.43
here), so it would not start on Ubuntu, Debian or SteamOS. `verify.py glibc` checks the bootloader and
every bundled library, and fails above glibc 2.17.

### 2. Wine (Windows build)

The Windows program is built by the real Windows Python running in Wine, so no Windows machine is needed.

- Fedora's `wine` is 64-bit only, and python.org's installer starts with a 32-bit bootstrapper (WiX Burn),
  which then fails. So `build.sh` uses `$WINE` if set, else the newest Proton in
  `~/.local/share/Steam/compatibilitytools.d/*/files/bin/wine` (GE-Proton has WoW64; GE-Proton11-7,
  Wine 11.0, here), else `wine` from `PATH`. It exports `WINE` and `WINESERVER` (the one next to that `wine`).
- The shell helpers that wrap Wine run `"$WINE"` by its absolute path. A function named `wine` that runs
  `wine` calls itself.
- The prefix is its own: `~/.cache/umbra-build/wine` (`WINEPREFIX`), with Mono and Gecko turned off
  (`WINEDLLOVERRIDES=mscoree=;mshtml=`). Python goes to `C:\umbra-py312`, the build tools to
  `C:\umbra-venv`. Paths given to the Windows Python are converted with `winepath -w`.
- Building and checking runs Wine programs only. Nothing touches Steam itself or other prefixes.

### Windows console

`Umbra Setup.exe` is a window program (`console=False`), so double-clicking it shows no console window.
(PyInstaller's `hide_console` alternative, a console program that hides its console, can't hide the
Windows Terminal window that recent Windows 11 opens for console programs.)
For use from cmd or PowerShell, `rthook_umbra.py` attaches to the console it was started from when it
has arguments and no output handle. Then `--version`, `--selftest` and `--install-to ... --yes` print
there. Output redirected to a file or pipe works as usual. The prompt "Install? [y/N]" can't read the
keyboard in that mode, so pass `--yes` on Windows.

Wine always hands a window program the terminal's output, so the checks under Wine don't use the
attach path. Check it once on a real Windows machine: `"Umbra Setup.exe" --version` in cmd should print
the version (cmd shows its prompt first, because it doesn't wait for window programs).

## Checks (verify.py)

`build.sh` runs these after each build. Each prints a PASS or FAIL line.

| Check | Linux | AppImage | Windows (Wine) |
| --- | --- | --- | --- |
| `glibc`: newest glibc symbol version in the bootloader and bundled libraries is at most 2.17 | yes | (same program) | |
| `--version` says `Umbra Setup <VERSION of the staged sources>` | yes | yes | yes |
| `--selftest --smoke 3`: https/TLS, Steam lookup, bundled logo and icon, the real Setup window for 3 s, then the launcher window for 3 s | yes | yes | yes |
| `--add-to FAKE --source PACK --no-steam --plan` against a throwaway pack, built with `tests/test_umbra.py`'s `make_source`, `build` and `make_target` | yes | yes | yes |
| the same plan from a copy in `PACK/installer/` with no `--source`: Setup finds the pack it sits in, which inside an AppImage means reading `$APPIMAGE` (`umbra_setup.own_file`), not its own path in the temporary mount | | yes | |
| `--version` with `APPIMAGE_EXTRACT_AND_RUN=1` (no FUSE) | | yes | |

The Windows checks run in a new, empty Wine prefix (`~/.cache/umbra-build/wine-clean`, deleted after),
never the build prefix, which has Python installed: players' Windows has no Python.

The plan check expects the plan to succeed, or, when Mod Organizer 2 is running on the build machine
(Linux), to stop with exactly one problem: "Mod Organizer 2 is open". That is the installer doing its job.
Under Wine, `tasklist` only sees programs in the build prefix, so there the plan always succeeds.

Run by hand: `python3 dev/build_installer/verify.py linux dist/installer/umbra-setup-linux`. Keep it
a file, not a script on stdin: the pack builder uses worker processes, and Python 3.14 starts them
with forkserver, which imports the main script again.

## The cache

`~/.cache/umbra-build` (`UMBRA_BUILD_CACHE`): `downloads/` (the two Pythons, appimagetool, the runtime), `pip/` and `pip-win/`
(wheels), `linux/` (Python, venv, stand-in, PyInstaller's work and output), `windows/` (PyInstaller's
work and output), `appimage/` (the AppImage before it is copied out), `wine/` (the prefix). About 1 GB in all. Deleting any part of it is safe: the next
build downloads and rebuilds it, checking the same hashes.

## Building on Windows without Wine

Install Python 3.12.10 (64-bit, with Tcl/Tk), then from the umbra folder in PowerShell:

    py -3.12 -m venv $env:TEMP\umbra-venv
    & $env:TEMP\umbra-venv\Scripts\python -m pip install --require-hashes --only-binary :all: -r dev\build_installer\requirements.txt
    $stage = "$env:TEMP\umbra-stage"; Remove-Item -Recurse -Force $stage -ErrorAction Ignore
    New-Item -ItemType Directory $stage | Out-Null
    Copy-Item installer\umbra_setup.py, installer\umbra_installer $stage -Recurse
    Set-Content "$stage\umbra_installer\build_info.py" "SOURCE = ''"   # or the pack URL
    $env:UMBRA_STAGE = $stage
    & $env:TEMP\umbra-venv\Scripts\python -m PyInstaller --noconfirm --clean --distpath dist\installer `
        --workpath $env:TEMP\umbra-work dev\build_installer\umbra_setup.spec
    & "dist\installer\Umbra Setup.exe" --selftest | Out-Host

Then add its line to `SHA256SUMS.txt` (`Get-FileHash`, lower-case, two spaces, then the file name).
