# Umbra's own mods

Umbra's own files of the mods it makes, one folder per MO2 mod. These are the source: edit them
here, put them into the tested instance with `python3 dev/deploy_mods.py`, test there, then build.
Their layers in `pack/umbra.toml` say `own_files = "../mods/<folder>"`, and `dev/pack_build.py`
refuses to pack a layer whose files here and in the instance differ (`deploy_mods.py --check`
lists them).

The instance's folders also hold files that aren't kept here: files merged from other authors'
mods (below) and MO2's `meta.ini`. The pack takes those from the instance, as tested.

## New Levels - Umbra Patch

What New Levels 0.53 needs to run in G.A.M.M.A. It goes above G.A.M.M.A.'s mods; New Levels
itself sits at the bottom of the load order (`keep_place`), with its own `all.spawn`,
`dialogs.xml` and `tasks_guide.script` hidden (`*.mohidden`) in the instance.

Kept here:

- `scripts/umbra_new_levels_labs.script`: Bunker A1 and the Collider are always open to the
  player (New Levels locks them behind its Redemption storyline, which only Loners and Clear Sky
  get, and nothing in 0.53 unlocks the bunker). Stalker squads get the Collider once the
  storyline's collider reset is done, or from the start when the player can't get the storyline.
  The labs' transitions go on the PDA map once the player reaches Promzone or a lab.
- `configs/misc/mod_simulation_objects_props_umbra_new_levels.ltx`: the labs' smart terrains take
  stalker squads of every faction (DLTX).
- `configs/scripts/{bunker_a1,collaider}/smart/mod_*_umbra.ltx`: room for one more squad in each.
- `configs/mod_sr_teleport_sections_umbra_new_levels.ltx`: the map arrows point down into the labs
  and up out of them, as Anomaly's own lab entrances do.

Merged in the instance only (mostly other authors' files):

- `spawns/all.spawn`: `python3 -I dev/spawnmerge.py BASE OURS THEIRS OUT`, with Anomaly 1.5.3's
  own `all.spawn` as BASE, G.A.M.M.A.'s (from "197- New Storylines (DLTX minimodpack) -
  Demonized", which carries the Hidden Threat storyline) as OURS, and New Levels 0.53's as
  THEIRS. It keeps every game-graph vertex id of G.A.M.M.A.'s levels and every New Storylines
  object and patrol. Redo it whenever G.A.M.M.A. changes its `all.spawn`.
- `configs/gameplay/dialogs.xml` and `scripts/tasks_guide.script`: three-way merges (Anomaly's
  copy as the base) of New Levels' copies with G.A.M.M.A.'s winners, "G.A.M.M.A. Quests
  Rebalance" and "Scrunkly's Collection of Mods". Redo them when either of those changes.

## Umbra - Stalkers Underground

Stalker squads of every faction can go into the Zone's labs and underground levels (DLTX on
`simulation_objects_props.ltx`), so you can run into them down there. Most are open from the
start; the smart terrains that hold a story scene open once it's over (X-18's scripted rooms,
X-10 once the Brain Scorcher is off, X-16 once the Miracle Machine is off, Warlab once its
generators are off, Agroprom Underground's story squads' spots once those squads are dead). The
`mod_*_umbra.ltx` files next to some smart terrains' configs make room for a visiting squad.
