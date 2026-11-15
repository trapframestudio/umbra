# Umbra's look

Tools for Umbra's visual refresh: its own splashes in place of G.A.M.M.A.'s,
the UI repaint, and HD character textures. The rule for all of it: **it must
feel like the game people know with a fresh coat of paint, never "AI" or too
clean.** Wear and texture come from the base game's own textures, and the HD
textures may only add detail the originals agree with.

Everything is generated from the source instance (`pack/umbra.toml`'s
`[source]`), so when G.A.M.M.A. changes a file we patch, a rebuild follows it.
Nothing in the instance is ever modified. Output goes to `out/` (not in git).

**Where it ships: the Umbra mod**, not a mod of its own, and never as whole
copies of G.A.M.M.A.'s files where something narrower works. The tools below
build whole files into `out/full/` (G.A.M.M.A.'s atlas with our cells painted
in; its string table with one line changed), which the previews use. Then
`python3 layer.py` writes `out/Umbra/`, the look's part of the Umbra mod
(`pack/umbra.toml`'s `umbra` layer), with only what we changed:

- **UI cells:** each atlas's changed cells are packed into an atlas of our
  own, `textures/umbra/<the atlas's path>.dds`, with 2 px of their old
  surroundings so filtering at their edges reads what it did before.
  `configs/ui/textures_descr/zzzzzzzz_umbra.xml` points those texture ids at
  it. The engine reads those files in name order and a later definition of an
  id replaces the earlier one (`CUITextureMaster::ParseShTexInfo`), so every
  other id keeps G.A.M.M.A.'s atlas, including what G.A.M.M.A. changes in it
  later. A layout or script that names an atlas by path (the PDA's map spots,
  3DSS's slot frame) keeps G.A.M.M.A.'s pixels there; `layer.py` lists them.
- **Strings:** the strings we changed go into
  `configs/text/<lang>/zzzzzzzz_umbra.xml` (a later id replaces an earlier
  one: `CStringTable::Load`).
- **Whole files** only where the game names the file itself: the loading
  screen and its side panels (engine), the menu logo and badge, the
  background loop and the faction screen's atlas (layouts, by path), and the
  fonts (by file name; `fonts.ltx` stays G.A.M.M.A.'s). Files that are only
  ours (the hryvnia strings, the font licences) ship as they are.

After changing the look: run the tool(s), then `python3 layer.py`, then
`dev/pack_build.py`.

## Files

| File | What it does |
|---|---|
| `vfs.py` | Which file the game really loads for a path: enabled MO2 mods (top of modlist.txt first), then Anomaly's gamedata, then its .db archives. `python3 vfs.py textures/ui/ui_common.dds` shows the winner and what it beats. |
| `dds.py` | DDS reading (Pillow) and uncompressed A8R8G8B8 writing, with mips averaged in linear light. |
| `paint.py` | Worn paint on steel from real game textures (the logo's method from `branding/weather.py`, with knobs for panels). |
| `splash.py` | The splashes and version strings (below). |
| `fonts.py` | The game's bitmap fonts: read, draw and build them (below). |
| `ui.py` | The UI repaint: G.A.M.M.A.'s panels in Umbra's paint (below). |
| `currency.py` | The currency is the hryvnia: string overrides for every line that says rubles (below). |
| `uirender.py` | Draws a UI screen's art from its XML, the way the engine lays it out, for before/after previews. |
| `layer.py` | The look's part of the Umbra mod (`out/Umbra`): only what we changed, from `out/full` (above). |
| `upscale/` | The HD texture pipeline (below). |

## Splashes (`python3 splash.py`)

Builds into `out/full/` (see above for what ships) and previews in
`out/preview/`:

- **Loading screen:** `textures/ui/ui_actor_loadgame_screen.dds` and
  `ui_actor_widescreen_sidepanels.dds`. The layout is fixed in the engine
  (monolith `src/Layers/xrRender/dxApplicationRender.cpp`,
  `load_draw_internal`): the atlas's (0,0)-(1024,768) is the 4:3 background,
  (0,772)-(506,776) the progress bar, the level picture covers virtual
  (0,173)-(1024,572), text starts at y 622 in grey 103. UVs are divided by a
  fixed 1024, so we ship 2048. We paint one 16:9 plate in screen pixels and cut
  it into both textures.
- **Main menu:** `textures/GAMMAMainMenuLogo.dds` (the logo, pre-squeezed for
  the 16:9 stretch), `textures/LogoGAMMA.dds` (G.A.M.M.A.'s Discord QR code,
  now the Umbra icon) and `textures/ui/ui_background_new.ogm` (the background
  loop: Ogg Theora, 1920x1080, 30 fps, 24 s, seamless).
  - **The button highlight:** `ui_inGame2_slider`. G.A.M.M.A.'s is a green-lit
    tube; Umbra's is a worn umber enamel strip in a bolted steel bracket.
    - It lives in `textures/ui/ui_actor_main_menu.dds` (G.A.M.M.A. UI's
      atlas), at (5,773) 274x61. `ui.py` repaints that cell in the winning
      atlas; `layer.py` ships just the cell.
    - The engine stretches the cell to 410x86 px, so it's painted at that size
      and shrunk into the cell.
  - **The new-game backdrop:** G.A.M.M.A.'s green-lit lab picture (1280x768)
    behind the faction screen, which xrRazom's co-op faction screen uses too.
    - It sits in `ui_actor_main_menu_one.dds` at (0,1024) and again at (0,0),
      and in `ui_actor_main_menu.dds` at (0,0) (debug screens and an unused
      background). Both atlases also hold a copy of the slider.
    - All of them get the menu's still, darkened a little because panels go
      over it, and squeezed to 1280x768 for the full-screen stretch.
    - Only those cells change; the textures are UI art, so co-op's config
      fingerprint isn't affected.
  - **The loop is a placeholder:** CS Eden's Dead City loading picture with the
    base game's fog sprites drifting across. The plan is real footage recorded
    in game, made into a seamless loop.
- **Version line:** `configs/text/<lang>/_game_version.xml` and
  `ui_st_loadscreen.xml` are the winning G.A.M.M.A. files with only
  `ui_st_game_version` / `ls_header` changed to
  `S.T.A.L.K.E.R. Umbra <pack version> (G.A.M.M.A. <its version>)`.

Arguments pick parts: `python3 splash.py loading menu video strings` (all
four by default). `menu` is the logo and badge; `video` re-encodes the loop,
which takes a few minutes and doesn't contain the logo.

**To check in game:** the text files are configs, and xrRazom's co-op join
check fingerprints configs (`config fingerprint: ... (N files in manifest)` in
the log). The engine's messages speak of "every loaded config file" and "the
merged config", which reads as the .ltx files the config system loads, not
text or texture-id XML; not proven. Compare that line (and the file count)
with the Umbra mod on and off: the `textures_descr` and text overrides must
leave it the same, so peers on plain G.A.M.M.A. + xrRazom still match.

## Fonts (`fonts.py`, in the venv below)

The UI's fonts are bitmap fonts: a glyph sheet (`.dds`, white glyphs with
coverage in alpha) and an `.ini` of cp1251 glyph boxes, both in
`textures/ui`. `configs/fonts.ltx` maps the UI's font names to them for each
resolution (picked by screen height: up to 600 `_800`, under 1024 `_1024`,
else `_1600`). We replace only the textures, never `fonts.ltx`, so co-op's
config fingerprint is untouched.

- `fonts.py build` writes all 30 (10 fonts at 3 resolutions) into
  `out/full`: **Tektur Narrow** (600 weight) for headings and menu
  buttons (`graff_*`), **Play** for all other text (`letter_*`, `arial_*`).
  The user picked them on 2026-11-15. Both are OFL; their licences go into
  the layer's `licenses/` folder, and they're credited in `pack/credits.toml`.
- Each is fitted to the font it replaces (G.A.M.M.A.'s Tactical Fonts 1k):
  - It keeps the same line height.
  - Text is no wider (Play 96-104%, Tektur 85-92%), so nothing overflows.
  - The baseline moves only if accents (Ї, Й) or descenders wouldn't
    otherwise fit the box.
- Cyrillic is drawn in its Ukrainian forms.
  - Watch for faces that default to Bulgarian shapes (Sofia Sans: т like m).
- `fonts.py previews [key ...]` draws the main menu, the loading screen and a
  text sample in each `CANDIDATES` face, with the game's own layout, into
  `out/preview/fonts/`.
- **Typefaces tried and rejected:** Arsenal, Fira Sans Condensed, Oswald,
  Sofia Sans Condensed, Roboto Condensed and Yanone Kaffeesatz were shown and
  not picked. PT Sans was left out because ParaType is a Moscow foundry; IBM
  Plex Condensed and Alumni Sans lack Ukrainian letters.
- **Not yet:** G.A.M.M.A.'s 2k/4k Tactical Fonts tiers (bigger `_1600` glyphs
  for 1440p and 4K screens). Our layer overrides them, so players on big
  screens would get 1080p-sized text. The fix is to build 2k/4k sets too and
  pick by screen size.

## UI repaint (`python3 ui.py`)

G.A.M.M.A.'s UI is a "modern"
carbon-fibre skin: diagonal hatching, CRT scanlines, brushed gunmetal. Umbra
repaints them as worn olive-black painted steel, keeping everything else:

- **Same panels.** Every shape, bevel, recess, transparency and baked-in
  label stays where it is, so layouts and item slots line up as before.
- **How:** `structure()` takes the pattern off with a median filter wider
  than the hatch, then puts the labels back whole. `repaint()` carries the
  panel's own light and shadow over onto `paint.sheet()` steel:
  - raised frames get chips and rust at their edges;
  - recesses, where items sit, get grime only (rust specks there read as
    blood);
  - light lettering comes out bone.
- **Inventory panels:** `CELLS` lists them, with per-panel settings. The
  trade bars have a coarser hatch; the lit plate under the quick slots also
  has a fine grid.
  - They're all in `textures/ui/ui_actor_menu.dds` (4096x2048). G.A.M.M.A.
    Accurate Defense Values ships the winning copy; `ui.py` repaints those
    cells in it, and `layer.py` ships only them.
  - Rank badges, icons, condition bars, drag highlights and lock overlays
    are neutral and stay as they are.
- **Everything else:** `AUTO` lists the other 16 sheets of G.A.M.M.A.'s skin:
  crafting, cooking, item details, talking, PDA, tooltips, message boxes,
  options, MCM, load/save, mutant loot, new game, sort tabs. They hold about
  450 usable cells, too many to list, so `pick()` sorts each sheet's cells
  (from its `textures_descr` rects):
  - **Left alone:** empty cells, coloured ones (icons, patches, pictures) and
    mostly-white ones (glyphs and bars the scripts colour), plus a per-sheet
    `skip` pattern (ui_common's CoP rust trims, TV frame and glass discs).
  - **Tint** (olive and bone, nothing else): frame and frameline parts the
    game stretches, small widgets, translucent overlays.
  - **Paint** (the full repaint): the big opaque panels.
  - Overlapping cells: the biggest wins. Wear is seeded by the id without its
    button-state suffix, so a button's states match.
  - Per-cell `opts`: no chips on mutant loot's lit plate; barely any grime on
    the PDA's glass; less edge rust on the crafting plate.
- **Wear details:** chips only in a band along raised frames' edges, never
  across a big plate or on thin highlight lines. Broad bright metal (the
  PDA's brushed bezel) is toned down to light olive. Coloured pixels (the
  campfire, blood) keep their colour. The grime is the painted half of a
  base-game wall, below its dado line; that line ran across big plates as a
  stripe.
- **Size:** our atlases are uncompressed (about 120 MB of UI in the Umbra
  mod). Block compression is a later step.
- **Not yet:** the HUD.
- **The currency sign:** G.A.M.M.A. bakes a ruble sign (₽) into the money
  box and the trade bars. `swap_currency()` erases it and letters a hryvnia
  sign (₴) in Play at the same height and colour.
  - `RUBLES` lists where the signs are. The build stops if the art there
    changes.
- **Hatch removal per panel:** most panels use the median. The trade bars
  use `rows`, which averages each row over one period of their coarse
  diagonal hatch: a wide enough median smears their sub-panels.
- `python3 ui.py` builds every sheet; `python3 ui.py ui/items/ui_workshop`
  builds just that one. Both then write the previews.
- `python3 ui.py previews [screen ...]` writes
  `out/preview/ui/compare-<screen>.png` (G.A.M.M.A. above, Umbra below):
  inventory, loot and trade, and the `XML_SCREENS` (crafting, cooking,
  item-detail, talk, pda, pda-tasks, message-box, options, load,
  mutant-loot, new-game, mcm, upgrade).
  - `uirender.py` draws inventory, loot and trade from `ui_inventory_16.xml`
    with what the script shows in each mode. The other screens are drawn
    whole from their layout files (`render_xml`), every part at once, so tabs
    and states overlap. It draws art only: no text or items.
  - It follows the engine's texture rules. `textures_descr` files load
    alphabetically, and a later definition of an id replaces an earlier one
    (`CMainMenu::ReadTextureInfo`, `CUITextureMaster::ParseShTexInfo`).
    Frames are `_lt`…`_rb`, and buttons draw `_e` at rest.

## Currency (`python3 currency.py`)

Umbra's currency is the hryvnia, never the ruble; the Zone is in Ukraine.

- **Art:** the ₴ in the money box and trade bars (`ui.py`, above).
- **Money read-outs:** G.A.M.M.A.'s scripts glue a literal " RU" onto
  amounts (inventory, trade, PDA, tooltips, upgrades). xrRazom already
  rewrites those at display time to its `st_xrr_currency` string ("UAH").
  We leave that to xrRazom and never touch its files.
- **Prose:** about 210 English and 270 Russian dialogue and task lines say RU,
  rubles or руб. `currency.py` writes
  `configs/text/<lang>/zzzzzzzz_umbra_currency.xml` with just those strings:
  the winning text with only the currency changed.
  - The engine reads text files in name order, and a later id replaces an
    earlier one, so this file overrides them. Its name sorts after
    G.A.M.M.A.'s `zzzz_` and xrRazom's `zzzzzz_`.
  - English becomes UAH and "hryvnias".
  - Russian swaps only the currency's own word forms (руб → грн,
    рублей → гривен, ...). Many other words start with "руб": рубеж,
    рубильник, the stalker Рубец.
  - The hryvnia is feminine, so the words that agree with it are fixed too
    (два → две, электронный → электронная).
  - `--show` lists every change.
- **Co-op:** these are text files, like the version line, so they belong in
  the same in-game fingerprint check (see Splashes).

## HD textures (`upscale/`)

Runs in its own venv: `~/.cache/umbra-look/venv` (`pip install ncnn numpy
pillow`). ncnn uses the GPU through Vulkan (RADV is fine), no ROCm.

- `pth.py` reads a PyTorch `.pth` into numpy **without PyTorch and without
  running anything in it** (a .pth is a pickle; only state-dict names are
  allowed). Run Python on downloaded models with `-I`.
- `esrgan.py convert model.pth out_dir` writes an ncnn graph for ESRGAN
  (RRDBNet) models, classic or BasicSR key names. `Upscaler` runs it in tiles.
  - ncnn.Mat borrows the numpy buffer: keep the array alive until extraction
    (forgetting this reads freed memory: garbage tiles, "NaN").
- `hd.py`: how model output becomes an Umbra texture.
  - `guarded()` is the method: broad colour and tone are the original's
    exactly; in the band where compression blocks live the model's cleaned-up
    version is used, and new fine detail is added at 0.6, **but only where the
    model's detail agrees with the original's** (local correlation) **and
    doesn't turn a pattern without a direction into stripes** (structure-tensor
    coherence). Elsewhere it falls back to the original.
  - 2K sources: the model runs 4x and is averaged down to 4K.
- `pilot.py`: comparison sheets (original / raw model / Umbra / trust map) in
  `out/preview/upscale/`.

**Model:** 4x RealisticRescaler (ESRGAN 64nf23nb, WTFPL, by Mutin Choler;
trained on realistic textures with BC1/JPEG compression). NMKD Siax was tried
and rejected: on DXT1 sources it turns stubble into worm patterns. Converted
models live in `~/.cache/umbra-look/models/` (not in git).

Speed on an RX 9070 XT (fp16): about 3 s for a 1K texture, 11 s for a 2K one.

**Still to build:** normal maps (`_bump`: A=X, B=Y, G=Z, R=gloss; upscale as
vectors and renormalise; `_bump#` holds height in alpha and a DXT error term),
block compression (BC1/BC7 with mips), the batch over `textures/act`, and the
`[[option]]` + layer so players pick original or HD in Setup.
