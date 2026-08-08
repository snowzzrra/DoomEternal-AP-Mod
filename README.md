# Doom Eternal Archipelago

Game-side repository for the DOOM Eternal Archipelago integration.

This repository owns the alpha mod package, Python bridge, external RPC client,
runtime manifests, map-generation and validation tools, and release packaging.
The APWorld source lives in the sibling Archipelago repository and is compiled
into `doometernal.apworld` during release builds.

> [!CAUTION]
> This project is an alpha, not a finished 1.0 release. Windows is the primary
> public target. Linux/Proton remains supported for development and early
> validation.

## Project status

Current `v0.3.9-alpha` release scope:

- Playable route: `Hell on Earth -> Fortress visit 1 -> Exultia -> Fortress
  visit 2 -> Cultist Base -> Doom Hunter Base -> Fortress visit 3 -> Super Gore
  Nest -> Fortress visit 4 -> ARC Complex -> Fortress visit 5 -> Mars Core ->
  Sentinel Prime -> Fortress visit 6 -> Taras Nabad -> Fortress visit 7 ->
  Nekravol -> Nekravol Part II -> Urdak -> Final Sin`.
- Public content: `307` generated map checks + `62` runtime locations = `369`
  Archipelago locations, plus `1` separate campaign goal.
- All `13` base-campaign missions are supported.

The current alpha validates map checks, item delivery, DeathLink, save-derived
locations, Fortress progression, runtime gating, and APWorld generation across
the supported campaign route.

### Version compatibility

Moving to a newer release may require generating a new world. Changes to IDs,
logic, options, or runtime contracts frequently mean that worlds created for an
older version are not compatible with the new client and APWorld.

## Installation

See [docs/INSTALL.md](docs/INSTALL.md) for the complete Windows and Linux/Proton
installation guide, directory layouts, verification steps, and troubleshooting.

## Project vision

Bring full Archipelago support to DOOM Eternal through native `.entities` map
modifications and Meathook RPC integration.

Core principles:

- Use native map modifications for location checks and item/trap command
  entities.
- Avoid idStudio full-map packages, which are too large for practical
  multiworld distribution.
- Preserve the campaign's normal feel wherever possible.
- Randomize progression, resources, and optional rewards without corrupting
  saves or vanilla inventory state.
- Prefer durable native events over console-log polling.
- Keep the APWorld source in the Archipelago fork and the game-side runtime in
  this repository.

## Repository split

This repository:

- owns the game-side runtime;
- builds the mod package;
- contains the Python bridge and external C++ RPC client;
- contains runtime manifests and item delivery data;
- contains map generation and validation tools;
- creates the final alpha release ZIP.

Sibling repository:

```text
Archipelago/worlds/doometernal/
```

- owns the APWorld source;
- defines item IDs, location IDs, regions, options, rules, and generation;
- is compiled into `doometernal.apworld` during release builds;
- is not copied into this repository as source.

## Runtime summary

- `ap_client.exe` is an external RPC client, not an injector embedded into the
  game process.
- Meathook is an external dependency that provides the in-game RPC server.
- Safe command execution is protected by a versioned, read-only memory gate.
  Unknown versions and failed reads stay fail-closed.
- Physical checks are detected through durable native `ap_event_*` files, not
  telemetry polling.
- Mission Complete and the campaign goal use native transition events, with
  save inspection retained as a fallback where applicable.

### Check flow

```text
AP-mutated pickup
  -> AP_CHECK_* relay
  -> optional AP Codex notification
  -> ap_event_<location_id>.txt
  -> bridge_client.py
  -> LocationChecks
  -> server acknowledgement
  -> event file removed
```

Event files remain until the Archipelago server confirms the location in
`checked_locations`.

### Item delivery

The bridge asks map-side `ap_rpc_v3_*` entities to perform supported item and
trap commands:

```text
ai_ScriptCmdEnt ap_rpc_v3_<item_id> activate
```

The external client imports queued commands only after the bridge arms RPC and
the memory gate confirms safe gameplay.

## Map content and validation

Every enabled map is authored only in
`content/maps/<map_key>/{descriptor,locations,runtime,publishers,assets,onboarding}.json`.
`data/map_sources.json`, manifests and runtime registries are compiled
projections for the client/package and are checked, never hand-edited.

To add a map, scaffold its package with `python -m tools.content.new_map`, fill
the six files, then run `scripts/pipeline.sh fast`, `scripts/pipeline.sh map
<map_key>`, `scripts/pipeline.sh changed`, and integration before a candidate
build.

Each accepted map has an independent semantic baseline in
`baselines/maps/<map_key>.json`. To accept one intentional map change:

```bash
python -m tools.content.accept_baseline \
    --map <map_key> \
    --reason "<intentional content change>"
```

The authoritative validation entrypoint is:

```bash
scripts/pipeline.sh fast
scripts/pipeline.sh map <map_key>
scripts/pipeline.sh changed
scripts/pipeline.sh integration
scripts/pipeline.sh release --build
```

The release gate uses a content-addressed receipt and builds:

```text
DoomEternalArchipelagoPlayableTest-v0.3.9-alpha.zip
├── README.md
├── RELEASE_MANIFEST.json
├── DoomEternalArchipelagoAlpha.zip
├── doometernal.apworld
└── client/
    ├── ap_client.exe
    ├── bridge_client.py
    ├── bridge_identity.json
    ├── save_death_probe.exe
    ├── save_decrypt.py
    ├── run_bridge.sh
    ├── start_injector_windows.bat
    ├── validate_runtime_install.sh
    ├── ap_config.example.json
    ├── data/
    ├── manifests/
    └── player_templates/
```

The release excludes APWorld source, native source, map-generation source,
tests, vanilla maps, extraction tooling, workspace memory, personal
configuration, seeds, logs, and local paths.

## Current alpha logic

- `randomize_chainsaw`, `randomize_dash`, and `randomize_first_battery`
  default to `false`; Dash remains experimental.
- Secret Encounters are normal AP locations on supported missions.
- Mission Challenges use native save state when their vanilla writer survives;
  converted physical rewards use server-checked
  `physical_event_equivalent` predicates. Weapon Masteries use native save
  state.
- Some Mission Challenges are completed by equivalent Archipelago checks when
  randomization replaces the vanilla collectible or upgrade action that
  normally advances them.
- Native Mission Challenge registration remains owned by the canonical
  `gameresources_patch2` `main.decl`; removing registration is runtime-rejected
  because it removes HUD and tracking. Individual child overrides suppress only
  Praetor rewards. DHB currently tests one fourth, impossible vanilla Horde
  registration to block native aggregate Battery completion; runtime pending.
- Weapon Point rewards remain vanilla until a safe revision-gated hook owns
  their conversion.
- Received progression/useful/trap items use the Current major card; filler
  uses the lateral Codex card. Location feedback uses the Codex presentation.

### Vanilla scripted weapon acquisitions

These acquisitions remain fully vanilla in `v0.3.9-alpha`:

- Super Shotgun — Cultist Base cutscene `5008`.
- BFG-9000 — Mars Core cutscene `4701`.
- Crucible — Taras Nabad cutscene `4137`.

The cutscene IDs are recorded for future inventory-grant stripping. That work
must remove only the weapon inventory grant while preserving each cutscene,
animation, progression edge, script, and owning mission; it is not implemented
in this release.

## Known probable issues

- Reloading the current checkpoint remains the recommended first recovery step
  for rare vanilla scripting desynchronization.
- Sentinel Battery HUD or Dossier counts may temporarily disagree with actual
  progression. Fortress socket requirements are authoritative.
- The in-game notification shows the received item name, but not the complete
  sender and source-location history. Keep the Archipelago client visible for
  the full log.
- DeathLink is currently "hardcore". Extra Lives are ignored and the player is
  killed directly.
- The Ice Bomb HUD marker may disappear when Frag Grenade is unavailable, but
  Ice Bomb itself remains usable.
- Mission Challenge and Weapon Mastery locations are detected from native save
  state. A newly completed challenge may wait until the next save write before
  being checked.
- Reaching a checkpoint, dying, reloading the current checkpoint, or returning
  to the main menu forces another save-state check.
- Sentinel Prime Archipelago locations use a static Codex-based visual
  replacement. They are fully functional, but currently inherit the Codex
  material and do not have the animated presentation used by Archipelago
  locations in other missions.
- Mission Challenges and Weapon Masteries already completed in a reused DOOM
  Eternal save are treated as pre-existing progress when that save is first
  linked to a new Archipelago session. They are not retroactively submitted;
  using a fresh campaign save is recommended for a new seed.

## Roadmap

### 0.1.1 — Runtime stabilization — DONE

- Froze the initial route through Cultist Base.
- Added RPC recovery, DeathLink, and Windows/Linux smoke coverage.

### 0.1.2 — Windows client hotfix — DONE

- Removed bundled proxy DLL aliases from the external client directory.
- Fixed Windows native-client startup and release dependency validation.

### 0.2.x — Campaign expansion and systems foundation — DONE

- Expanded the playable campaign route mission by mission while generalizing
  manifests, regions, mission completion, map checks, and per-map validation.
- Established the native `.entities` generation pipeline and the shared runtime
  foundations used for Archipelago locations, item delivery, and optional
  systems in later releases.

### 0.3.x Alpha — Base campaign map expansion — DONE

- `0.3.8`: Final Sin.
- `0.3.9`: complete base-campaign regression, cleanup, documentation and
  release stabilization; no major new system.

### 0.4.0 Beta — Installation, logic and option architecture

- Windows + Linux installer/launcher; game-path/version, dependency, injection
  and client-identity validation; seed/options-generated mod; final scripted
  weapon stripping (SSG `5008`, BFG `4701`, Crucible `4137`); subregions;
  hard/soft/combat logic options; hardcore DeathLink fix; and option-driven
  features such as randomized starting weapons and configurable starting
  inventory.

### 0.5.x Beta — The Ancient Gods

- Add The Ancient Gods Part One and Part Two campaigns, including their
  missions, regions, locations, items, progression rules, and completion flow.
- Extend the existing generation, runtime, and validation architecture to cover
  DLC-specific mechanics without weakening base-campaign behavior.

### 0.6.x Beta — Mission Access as Items

- Turn mission access into Archipelago progression items. A seed can unlock
  missions in a randomized order instead of following the vanilla campaign
  sequence, while generation guarantees a valid starting mission and reachable
  progression.

### 0.7.x Beta — Enemizer

- Add an enemy randomizer that changes enemy placements and encounter
  compositions while respecting arenas, progression-critical encounters, and
  runtime safety constraints.

### 0.8.x–0.9.x — Content freeze and polish

- Freeze the planned `1.0` scope and stabilize IDs and data formats.
- Focus on balance, installation, compatibility, save/reconnect behavior,
  discoverability, and broader community testing.
- Finish documentation, diagnostics, support tooling, and release-candidate
  validation; late `0.9.x` changes are limited to blockers and regressions.

### 1.0

- Final stable public release of the completed supported campaigns and systems.

### Post-1.0 / 2.0

- Horde Mode and Master Levels.
- Hard Mode / checkpoint removal.

## Credits

- The Archipelago project and contributors for the multiworld framework,
  protocol, server, and `CommonClient`.
- tastyfresh (from the Doom 2016+ Modding Discord server) for the original 
  large check list used to bootstrap the project.
- Zwip Zwap Zapony (from the Doom 2016+ Modding Discord server) for direct 
  technical guidance and map/runtime research.
- alby (from the Doom 2016+ Modding Discord server) for technical help, runtime
  investigation, and safe-native-behavior
  guidance.
- chrispy for creating
  [Meathook](https://github.com/brongo/m3337ho0o0ok), the RPC foundation this
  project builds on.
- PowerBall253 / brunoanc for
  [EternalResourceExtractor](https://github.com/brunoanc/EternalResourceExtractor)
  and `idFileDeCompressor`.
- FlavorfulGecko5 and the EntitySlayer contributors for
  [EntitySlayer](https://github.com/FlavorfulGecko5/EntitySlayer).
- The DOOM Modding community for EternalModInjector, wiki material, and
  general knowledge that made the map patches possible.
- Meta (from the AP After Dark Discord server) for the Archipelago Logo model.
- FridgeDuck (from the AP After Dark Discord server) for the Doom Archipelago 
  logo used by the AP client.
