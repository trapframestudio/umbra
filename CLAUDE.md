# Umbra: notes for agents

Umbra is a S.T.A.L.K.E.R. modpack on G.A.M.M.A. with xrRazom co-op: a cross-platform
installer ("Setup"), a launcher, and the tools that build and host the pack. `README.md`
explains how it all works; read its sections before changing a part.

## Rules

- **Compatibility is the hard line.** Everything must keep working in single-player
  G.A.M.M.A. and in xrRazom co-op. Never edit xrRazom's files and never swap its exes.
  G.A.M.M.A.'s installer puts its own exes back over xrRazom's; Umbra checks and restores them.
- **xrRazom is never hosted by Umbra.** Its author asked that players download it from
  ModDB, so Setup fetches it from there (`players_download` in `pack/umbra.toml`).
- **Nothing goes public without the maintainer's go:** releases, uploads to the pack host,
  posts. Commit only when asked.
- **Private data never goes into the repo:** keys, emails, real names, Steam ids, home paths.
  - Keys live outside the repo (`~/.config/umbra/*.env`). Never print, pack or commit them.
  - `python3 dev/check_secrets.py --staged` checks a commit for all of the above; install it as
    `.git/hooks/pre-commit`. It reads what counts as private from the machine, so the script
    itself holds nothing private.
- **Never in git:** `dist/` (pack volumes and installers, tens of GB) and `branding/.cache/`
  (textures extracted from the game).
- **Leave running things alone:**
  - Don't write `ModOrganizer.ini` or `modlist.txt` while MO2 runs; it rewrites them on exit.
  - Don't rebuild `dist/installer/` while a Setup runs from it. Check with
    `pgrep -fa '[d]ist/installer/'` (the brackets stop pgrep matching itself).
  - Don't touch a player's install, its Proton prefix or its launcher while they play.
- **Shelved, don't re-propose:** DX12, frame generation, upscaling, bodycam, PiP scopes.
  Every engine fork with them replaces the exe and lacks xrRazom's networking.
- **Downloaded files are untrusted.** Unpack each into its own empty folder and run Python
  on them with `-I`.

## Tests

    python3 -m unittest -v tests/test_umbra.py tests/test_b2_upload.py

- Tests never touch real Steam files, real menus or a real install. They set
  `UMBRA_SHORTCUTS_ROOT` and patch `steam_roots`; keep new tests isolated the same way.
- The system Python may have no tkinter, so `gui.py` and `launcher_gui.py` can't be imported
  in tests. Keep testable logic in `launcher.py`, `install.py` and the other non-GUI modules.

## Building

- Installers: `dev/build_installer/build.sh linux|windows|all|verify` (see its README).
  Windows builds need a Wine that runs 32-bit programs; the script picks GE-Proton's.
- Pack: `dev/pack_build.py`. Dry-run first and compare layer sizes with the last build; a
  ZGO folder that lost its bake ships ZGO without its body models (expect about 7.8 GB for
  the zgo layer).
- Hosting: `dev/b2_upload.py` (`--dry-run` first).
- Releases: GitHub Actions (`.github/workflows/build.yml`) builds and checks Setup on every push
  and drafts a release for a `vX.Y.Z` tag matching `VERSION`. Pushing a tag and publishing the
  draft are the maintainer's call. The pack is never built in Actions.
