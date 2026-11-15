#!/usr/bin/env bash
# Build the release programs of Umbra Setup (README.md here explains everything).
#
#   dev/build_installer/build.sh linux     dist/installer/umbra-setup-linux
#                                          (python-build-standalone 3.12.10, glibc 2.17+), and the
#                                          same program as dist/installer/Umbra-Setup-x86_64.AppImage
#                                          (what Linux players download: an icon and a menu entry)
#   dev/build_installer/build.sh windows   "dist/installer/Umbra Setup.exe"
#                                          (python.org 3.12.10 inside a Wine prefix)
#   dev/build_installer/build.sh all       both
#   dev/build_installer/build.sh verify    check the programs already in dist/installer again
#
#   UMBRA_SOURCE=URL     the pack URL built into the programs (default: none; players then pass --source)
#   UMBRA_BUILD_CACHE    downloads, Pythons, build tools, Wine prefix (default ~/.cache/umbra-build)
#   WINE=/path/to/wine   the Wine for the Windows build (default: the newest Proton in
#                        ~/.local/share/Steam/compatibilitytools.d)
#
# Every download is pinned by sha256 (below, and requirements.txt for the build tools); a mismatch stops
# the build. After building, verify.py checks each program and dist/installer/SHA256SUMS.txt is updated.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
cache="${UMBRA_BUILD_CACHE:-$HOME/.cache/umbra-build}"
dl="$cache/downloads"
stage=''   # stage_sources makes a fresh one for each run; it is removed when build.sh exits
out="${UMBRA_INSTALLER_OUT:-$root/dist/installer}"   # another folder while a built Setup is running
LINUX_NAME='umbra-setup-linux'
APPIMAGE_NAME='Umbra-Setup-x86_64.AppImage'
WIN_NAME='Umbra Setup.exe'
VERSION=''

PBS_FILE='cpython-3.12.10+20250409-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
PBS_URL='https://github.com/astral-sh/python-build-standalone/releases/download/20250409/cpython-3.12.10%2B20250409-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
PBS_SHA='8c59b9ac6bff2dc3934181d7bc82594f9f59a613afed8d72c9e89d7194e790ee'
WINPY_FILE='python-3.12.10-amd64.exe'
WINPY_URL='https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
WINPY_SHA='67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb'
# The AppImage: appimagetool, and the type 2 runtime (static: players need no libfuse2)
AIT_FILE='appimagetool-1.9.1-x86_64.AppImage'
AIT_URL='https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage'
AIT_SHA='ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0'
AIRT_FILE='appimage-runtime-20251108-x86_64'
AIRT_URL='https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64'
AIRT_SHA='2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d'
PIP_FLAGS=(install --quiet --disable-pip-version-check --no-input --require-hashes --only-binary :all:)

say() { printf '\n== %s\n' "$*"; }
die() { printf 'build.sh: %s\n' "$*" >&2; exit 1; }

fetch() {   # fetch FILE URL SHA256: download once into $dl, then check the hash on every build
	mkdir -p "$dl"
	if [ ! -f "$dl/$1" ]; then
		say "downloading $1"
		curl -L --fail --proto '=https' --tlsv1.2 -o "$dl/$1.part" "$2"
		mv "$dl/$1.part" "$dl/$1"
	fi
	echo "$3  $dl/$1" | sha256sum -c --quiet - \
		|| die "the sha256 of $dl/$1 is not the pinned $3; delete the file and run again"
}

cleanup() { if [ -n "$stage" ]; then rm -rf "$stage"; fi; }
trap cleanup EXIT

stage_sources() {   # a fresh snapshot of installer/ to build from, plus the generated build_info.py
	say "staging installer/ (built-in pack URL: ${UMBRA_SOURCE:-none})"
	mkdir -p "$cache"
	stage="$(mktemp -d "$cache/stage.XXXXXX")"
	cp -p "$root/installer/umbra_setup.py" "$stage/"
	cp -rp "$root/installer/umbra_installer" "$stage/"
	find "$stage" -name __pycache__ -prune -exec rm -rf {} +
	find "$stage" -name '*.pyc' -delete
	# build_info.py exists only here, never in the source tree
	python3 -I -c 'import sys
print("# Written by dev/build_installer/build.sh into the build copy only.")
print("SOURCE = %r" % sys.argv[1])' "${UMBRA_SOURCE:-}" > "$stage/umbra_installer/build_info.py"
	# a half-saved file (someone editing right now) fails here rather than inside the program
	python3 -I -c 'import pathlib, sys
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    compile(p.read_bytes(), str(p), "exec")' "$stage" || die "the installer sources do not compile; try again in a minute"
	VERSION="$(python3 -I -c 'import re, sys
print(re.search(r"^VERSION\s*=\s*[\x22\x27]([^\x22\x27]+)", open(sys.argv[1], encoding="utf-8").read(), re.M).group(1))' \
		"$stage/umbra_installer/__init__.py")" || die "no VERSION in installer/umbra_installer/__init__.py"
	echo "Umbra Setup $VERSION"
}

record() {   # record FILE: put its sha256 into dist/installer/SHA256SUMS.txt, replacing an older line
	local name sums="$out/SHA256SUMS.txt"
	name="$(basename "$1")"
	touch "$sums"
	{ awk -v n="$name" '{ f = $0; sub(/^[0-9a-f]+ [ *]/, "", f); if (f != n) print }' "$sums"
	  ( cd "$out" && sha256sum -- "$name" ); } | sort -k2 > "$sums.new"
	mv "$sums.new" "$sums"
}

# --------------------------------------------------------------------------- Linux
nocursor() {   # the empty libXcursor.so.1 stand-in (nocursor.c says why)
	command -v cc >/dev/null || die "a C compiler (cc) is needed for the Linux program (nocursor.c)"
	local so="$cache/linux/stub/libXcursor.so.1"
	mkdir -p "$(dirname "$so")"
	cc -shared -nostdlib -fPIC -Wl,-soname,libXcursor.so.1 -o "$so" "$here/nocursor.c"
	if command -v readelf >/dev/null; then
		readelf -d "$so" | grep -q 'SONAME.*\[libXcursor\.so\.1\]' || die "the stand-in has no SONAME"
		if readelf -d "$so" | grep -q NEEDED; then die "the stand-in needs other libraries"; fi
	fi
}

linux() {
	fetch "$PBS_FILE" "$PBS_URL" "$PBS_SHA"
	local py="$cache/linux/python/bin/python3" venv="$cache/linux/venv"
	if [ ! -x "$py" ]; then
		say "unpacking Python 3.12.10 (python-build-standalone)"
		mkdir -p "$cache/linux"
		tar -xzf "$dl/$PBS_FILE" -C "$cache/linux"
	fi
	[ -x "$venv/bin/python" ] || "$py" -m venv "$venv"
	say "build tools (requirements.txt)"
	"$venv/bin/python" -m pip "${PIP_FLAGS[@]}" --cache-dir "$cache/pip" -r "$here/requirements.txt"
	nocursor
	say "building $LINUX_NAME"
	UMBRA_STAGE="$stage" UMBRA_NOCURSOR="$cache/linux/stub/libXcursor.so.1" \
		"$venv/bin/python" -m PyInstaller --noconfirm --clean --log-level WARN \
		--distpath "$cache/linux/dist" --workpath "$cache/linux/work" "$here/umbra_setup.spec"
	mkdir -p "$out"
	install -m 755 "$cache/linux/dist/$LINUX_NAME" "$out/$LINUX_NAME"
	record "$out/$LINUX_NAME"
	verify_linux
	appimage
}

appimage() {   # the Linux program, unchanged, inside an AppImage with its icon and menu entry
	[ -f "$out/$LINUX_NAME" ] || die "no $out/$LINUX_NAME to put in the AppImage; run build.sh linux"
	fetch "$AIT_FILE" "$AIT_URL" "$AIT_SHA"
	fetch "$AIRT_FILE" "$AIRT_URL" "$AIRT_SHA"
	chmod 755 "$dl/$AIT_FILE"
	say "making $APPIMAGE_NAME"
	local app="$cache/appimage/Umbra-Setup.AppDir"
	rm -rf "$app" "$cache/appimage/$APPIMAGE_NAME"
	mkdir -p "$app/usr/bin"
	install -m 755 "$out/$LINUX_NAME" "$app/usr/bin/$LINUX_NAME"
	install -m 644 "$root/installer/umbra_installer/assets/umbra-icon-256.png" "$app/umbra-setup.png"
	ln -s umbra-setup.png "$app/.DirIcon"
	cat > "$app/umbra-setup.desktop" <<-EOF
	[Desktop Entry]
	Type=Application
	Name=Umbra Setup
	Comment=Installs and updates the Umbra modpack for G.A.M.M.A.
	Exec=$LINUX_NAME
	Icon=umbra-setup
	Terminal=false
	Categories=Game;
	EOF
	# The program inside finds the AppImage's own path in $APPIMAGE (umbra_setup.own_file)
	cat > "$app/AppRun" <<-EOF
	#!/bin/sh
	here="\${APPDIR:-\$(dirname "\$(readlink -f "\$0")")}"
	exec "\$here/usr/bin/$LINUX_NAME" "\$@"
	EOF
	chmod 755 "$app/AppRun"
	# appimagetool is an AppImage itself: run it unpacked, so the build needs no FUSE either
	( cd "$cache/appimage" && ARCH=x86_64 VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 \
		"$dl/$AIT_FILE" --no-appstream --runtime-file "$dl/$AIRT_FILE" "$app" "$APPIMAGE_NAME" )
	[ -f "$cache/appimage/$APPIMAGE_NAME" ] || die "appimagetool made no $APPIMAGE_NAME"
	install -m 755 "$cache/appimage/$APPIMAGE_NAME" "$out/$APPIMAGE_NAME"
	rm -rf "$app"
	record "$out/$APPIMAGE_NAME"
	verify_appimage
}

verify_appimage() {
	say "checking $APPIMAGE_NAME"
	UMBRA_VERIFY_TMP="$cache" python3 "$here/verify.py" appimage "$out/$APPIMAGE_NAME" \
		${VERSION:+--expect-version "$VERSION"}
}

verify_linux() {
	say "checking $LINUX_NAME"
	local venv="$cache/linux/venv" rc=0
	if [ -x "$venv/bin/python" ]; then
		"$venv/bin/python" -I "$here/verify.py" glibc "$out/$LINUX_NAME" || rc=1
	fi
	UMBRA_VERIFY_TMP="$cache" python3 "$here/verify.py" linux "$out/$LINUX_NAME" \
		${VERSION:+--expect-version "$VERSION"} || rc=1
	return $rc
}

# --------------------------------------------------------------------------- Windows (under Wine)
pick_wine() {   # WINE and WINESERVER, as absolute paths
	if [ -z "${WINE:-}" ]; then
		# Fedora's wine is 64-bit only, and python.org's installer starts with a 32-bit
		# bootstrapper (WiX Burn). Proton's Wine runs both.
		local d
		for d in "$HOME/.local/share/Steam" "$HOME/.steam/steam"; do
			WINE="$(ls -d "$d"/compatibilitytools.d/*/files/bin/wine 2>/dev/null | sort -V | tail -n 1 || true)"
			[ -z "$WINE" ] || break
		done
		[ -n "$WINE" ] || WINE="$(command -v wine || true)"
		[ -n "$WINE" ] || die "no Wine found; set WINE to one that runs 32-bit programs (e.g. GE-Proton's files/bin/wine)"
	fi
	case "$WINE" in /*) ;; *) WINE="$(command -v "$WINE")" || die "WINE=$WINE is not a program" ;; esac
	[ -x "$WINE" ] || die "WINE=$WINE is not a program"
	if [ -z "${WINESERVER:-}" ]; then
		if [ -x "$(dirname "$WINE")/wineserver" ]; then WINESERVER="$(dirname "$WINE")/wineserver"
		else WINESERVER="$(command -v wineserver)" || die "no wineserver next to $WINE"; fi
	fi
	export WINE WINESERVER
}

wine_env() {
	export WINEPREFIX="${WINEPREFIX:-$cache/wine}" WINEDEBUG="${WINEDEBUG:--all}"
	# no Mono or Gecko: the build needs neither, and Wine would offer to install them; no
	# winemenubuilder: the shortcut self-test must not put entries in this machine's menus
	export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree=;mshtml=;winemenubuilder.exe=d}"
	pick_wine
	echo "Wine: $WINE (prefix $WINEPREFIX)"
}

# Absolute paths only: a function named wine that runs "wine" would call itself.
w() { "$WINE" "$@"; }
wpath() { "$WINE" winepath -w "$1" 2>/dev/null | tail -n 1; }

windows() {
	fetch "$WINPY_FILE" "$WINPY_URL" "$WINPY_SHA"
	wine_env
	if [ ! -d "$WINEPREFIX/drive_c" ]; then
		say "making the Wine prefix"
		w wineboot -i >/dev/null 2>&1 || true
		"$WINESERVER" -w
	fi
	if [ ! -f "$WINEPREFIX/drive_c/umbra-py312/python.exe" ]; then
		say "installing Python 3.12.10 into the Wine prefix"
		w "$dl/$WINPY_FILE" /quiet InstallAllUsers=0 'TargetDir=C:\umbra-py312' Include_launcher=0 \
			Include_test=0 Include_doc=0 Include_tcltk=1 Include_pip=1 PrependPath=0 Shortcuts=0 \
			AssociateFiles=0 || true
		"$WINESERVER" -w
		[ -f "$WINEPREFIX/drive_c/umbra-py312/python.exe" ] || die "Python did not install into the Wine prefix"
	fi
	local wpy='C:\umbra-venv\Scripts\python.exe'
	[ -f "$WINEPREFIX/drive_c/umbra-venv/Scripts/python.exe" ] || w 'C:\umbra-py312\python.exe' -m venv 'C:\umbra-venv'
	say "build tools (requirements.txt)"
	mkdir -p "$cache/pip-win"
	w "$wpy" -m pip "${PIP_FLAGS[@]}" --cache-dir "$(wpath "$cache/pip-win")" -r "$(wpath "$here/requirements.txt")"
	say "building $WIN_NAME"
	mkdir -p "$cache/windows"
	rm -f "$cache/windows/dist/$WIN_NAME"
	UMBRA_STAGE="$(wpath "$stage")" w "$wpy" -m PyInstaller --noconfirm --clean --log-level WARN \
		--distpath "$(wpath "$cache/windows/dist")" --workpath "$(wpath "$cache/windows/work")" \
		"$(wpath "$here/umbra_setup.spec")"
	"$WINESERVER" -w
	[ -f "$cache/windows/dist/$WIN_NAME" ] || die "PyInstaller made no $WIN_NAME"
	mkdir -p "$out"
	install -m 755 "$cache/windows/dist/$WIN_NAME" "$out/$WIN_NAME"
	record "$out/$WIN_NAME"
	verify_windows
}

verify_windows() {
	# A new, empty Wine prefix each time, never the build's (that one has Python installed):
	# players' Windows has no Python, so the program must bring everything it needs.
	say "checking $WIN_NAME under Wine, in a new prefix with no Python (as on a player's Windows)"
	local build_prefix="${WINEPREFIX:-}"
	export WINEPREFIX="$cache/wine-clean"
	rm -rf "$WINEPREFIX"
	wine_env
	w wineboot -i >/dev/null 2>&1 || true
	"$WINESERVER" -w
	if find "$WINEPREFIX/drive_c" \( -iname "python*.exe" -o -iname "python3*.dll" \) | grep -q .; then
		die "the clean Wine prefix has a Python in it"
	fi
	local rc=0
	UMBRA_VERIFY_TMP="$cache" python3 "$here/verify.py" windows "$out/$WIN_NAME" \
		${VERSION:+--expect-version "$VERSION"} || rc=1
	"$WINESERVER" -k 2>/dev/null || true
	rm -rf "$WINEPREFIX"
	if [ -n "$build_prefix" ]; then export WINEPREFIX="$build_prefix"; else unset WINEPREFIX; fi
	return $rc
}

# --------------------------------------------------------------------------- main
case "${1:-}" in
	linux) stage_sources; linux ;;
	appimage) stage_sources; appimage ;;
	windows) stage_sources; windows ;;
	all) stage_sources; linux; windows ;;
	verify)
		rc=0
		if [ -f "$out/$LINUX_NAME" ]; then verify_linux || rc=1; fi
		if [ -f "$out/$APPIMAGE_NAME" ]; then verify_appimage || rc=1; fi
		if [ -f "$out/$WIN_NAME" ]; then verify_windows || rc=1; fi
		( cd "$out" && sha256sum -c SHA256SUMS.txt ) || rc=1
		exit $rc ;;
	*) sed -n '2,17p' "$0"; exit 2 ;;
esac
say "done: $out"
( cd "$out" && cat SHA256SUMS.txt && ls -l -- * )
