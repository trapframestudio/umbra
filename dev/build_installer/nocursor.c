/*
 * An empty stand-in for libXcursor.so.1. It is bundled in umbra-setup-linux
 * only: build.sh linux compiles it and umbra_setup.spec adds it. README.md
 * here explains it under "The cursor library".
 *
 * python-build-standalone's tkinter has its own copy of libX11 built in. When
 * Tk asks for a named cursor (for example the I-beam over every text box),
 * that libX11 dlopens "libXcursor.so.1" for themed cursors. The system's
 * libXcursor pulls in the system's libX11, and then two copies of libX11
 * drive one display connection. On a recent libX11 (Fedora 44 here), X
 * aborts with "[xcb] Unknown sequence number while appending request" as
 * soon as the window builds its first text box.
 *
 * umbra_setup.py (no_xcursor) loads this file from sys._MEIPASS before Tk
 * starts. Its SONAME is libXcursor.so.1, so libX11's dlopen gets this
 * already-loaded copy. It has no symbols, so libX11 finds no
 * XcursorTryShapeCursor and falls back to the classic X cursors. It has no
 * code and no libc dependency (-nostdlib), so it loads on any x86_64 Linux.
 *
 * A runtime hook (rthook_umbra.py) removes the bundle folder from
 * LD_LIBRARY_PATH again, so programs that Umbra Setup starts (xdg-open and
 * the file manager behind it, stalker-gamma-cli) never pick this stand-in up
 * in place of the real library.
 */
