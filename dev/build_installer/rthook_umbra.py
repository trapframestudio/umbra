# PyInstaller runtime hook for Umbra Setup. umbra_setup.spec adds it, and it
# runs inside the built program before umbra_setup.py does. README.md here
# says why each part exists.
import os
import sys


def _linux_child_env():
    # The one-file bootloader puts the bundle folder first on LD_LIBRARY_PATH.
    # That folder holds the empty libXcursor.so.1 stand-in (nocursor.c). This
    # process read LD_LIBRARY_PATH when it started and keeps its libraries, but
    # the programs Umbra Setup starts (xdg-open and the file manager behind
    # it, stalker-gamma-cli) would load the stand-in instead of the real
    # libXcursor and fail. Give them the player's own environment back.
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def _windows_console():
    # "Umbra Setup.exe" is a window program, so double-clicking it shows no
    # console window. Run with arguments from cmd or PowerShell (--version,
    # --selftest, --install-to ...), it attaches to that console so its text
    # shows up there. When the output is redirected (to a file or a pipe),
    # Python already has it and nothing changes. There is no keyboard input
    # in this mode, so a command-line install on Windows needs --yes.
    if len(sys.argv) < 2 or (sys.stdout is not None and sys.stderr is not None):
        return
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return
        if sys.stdout is None:
            sys.stdout = open("CONOUT$", "w", buffering=1, errors="replace")
        if sys.stderr is None:
            sys.stderr = open("CONOUT$", "w", buffering=1, errors="replace")
        if sys.stdin is None:
            sys.stdin = open(os.devnull)
    except (AttributeError, OSError):
        pass


if sys.platform.startswith("linux"):
    _linux_child_env()
elif sys.platform == "win32":
    _windows_console()
