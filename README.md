# Doom Eternal Archipelago

Game-side repository for the DOOM Eternal Archipelago integration.

This repo owns the alpha mod package, the Python bridge, the external
RPC client, runtime manifests, map-generation scripts, validation scripts, and
release packaging. The APWorld source does **not** live here; it stays in the
sibling Archipelago branch and is compiled into `doometernal.apworld` during release builds.

> [!CAUTION]
> This project is an alpha build, not a finished 1.0 release. Windows is
> the primary target for public testing, while Linux/Proton remains supported
> for development and early validation.

## Project status

Current `v0.3.1-alpha` scope:

- Route: `Hell on Earth -> Fortress visit 1 -> Exultia -> Fortress visit 2 -> Cultist Base -> Doom Hunter Base -> Fortress visit 3`
- Content: `108` generated map checks + `25` runtime locations = `133`
  Archipelago locations; the goal is a
  separate native event on the Fortress visit 3 transition.
- Challenges/masteries: durable native save records drive their AP locations;
  reward suppression remains scoped to the audited owners.
- Battery economy: all `18` base-campaign currency is present; the default
  early Exultia Battery remains locked and the public Bundle ID is unchanged.
- Mission Complete: Hell, Exultia and Doom Hunter Base use exact map terminal
  events; Cultist retains its transition publisher.
- Full campaign, DLC, Master Levels, Horde Mode, enemy randomizer, and final
  Archipelago balancing are future milestones.

The goal of this PTB is to prove that map checks, item delivery, DeathLink,
runtime gating, and the APWorld can work reliably across multiple early-game
levels.

## Project vision

Bring full Archipelago support to DOOM Eternal using native `.entities` map
modifications plus Meathook RPC integration.

Core philosophy:

- Use native map modifications for location checks and item/trap command
  entities.
- Avoid idStudio full-map packaging, because idStudio saves entire map payloads
  and would make multiworld distribution unreasonably large.
- Preserve the campaign’s normal feel wherever possible.
- Randomize progression, resources, and optional rewards without corrupting
  save files or the vanilla inventory.
- Prefer durable native events over console-log polling.
- Keep the APWorld source in the Archipelago fork and the game-side runtime in
  this repository.

## Repository split

This repository:

- owns the game-side runtime;
- builds the mod package;
- contains the Python bridge;
- contains the external C++ RPC client;
- contains runtime manifests and item delivery data;
- contains map generator and validation scripts;
- creates the final PTB release ZIP.

Sibling repository:

```text
Archipelago/worlds/doometernal/
```

- owns the APWorld source;
- defines item IDs, location IDs, regions, options, rules, and generation logic;
- is compiled into `doometernal.apworld` during release builds;
- is **not** copied into this repository.

## Runtime summary

- `ap_client.exe` is an external RPC client, not an injector embedded into the
  game process.
- Meathook remains an external dependency that provides the in-game RPC server.
- Safe command execution is protected by a versioned, read-only memory gate.
  Unknown game versions and failed reads stay fail-closed.
- Physical checks are detected by native `ap_event_*` files, not by telemetry
  polling.
- Mission Complete and the runtime goal use native transition events, with the old
  autosave path kept only as fallback behavior.

## Architecture overview

### Map modification strategy

The map generator patches dumped `.entities` files directly.

For supported pickups, the generator:

1. locates vanilla pickup entities selected in `level_configs/*.json`;
2. strips or bypasses vanilla inventory rewards;
3. converts the interaction into an AP check path;
4. standardizes the visible AP pickup presentation where possible;
5. injects an `AP_CHECK_*` relay;
6. injects native notification feedback;
7. injects an `ap_event_<location_id>` command entity that writes a durable
   event file for the bridge.

This avoids inventory polling and avoids relying on `g_debugTriggers`.

### Command entity delegation

Directly executing gameplay-changing commands over asynchronous RPC can be
unsafe during loading, menus, cutscenes, and other busy game states.

Instead, every supported item/trap delivery is represented by map-side
`idTarget_Command` / relay entities. The Python bridge sends a safe activation
request such as:

```text
ai_ScriptCmdEnt ap_rpc_v3_<item_id> activate
```

The entity then executes the actual in-game command natively.

The external `ap_client.exe` imports queued commands and only consumes them when:

1. the bridge has armed RPC execution, and
2. the read-only memory gate confirms safe gameplay.

### Check flow

```text
AP-mutated pickup
  -> AP_CHECK_* relay
  -> native pickup notification
  -> ap_event_<location_id>.txt
  -> bridge_client.py
  -> LocationChecks
  -> server ack
  -> event file removed
```

Event files are kept until the Archipelago server confirms the location in
`checked_locations`.

### Mission completion flow

```text
native terminal/transition owner

  -> writes a durable AP event
  -> bridge_client.py matches its registered location or goal
  -> sends once and retains the event until server ACK
```

## Release package

## Vanilla map sources

`data/map_sources.json` is the single map registry. It drives enabled-map
enumeration, vanilla source/hash, config, generated filename, manifest,
runtime identity, resource owner/priority/path, validation, package layout and
`RELEASE_MANIFEST.json`. `map_registry.py` constructs the generation,
validation and package plans; test-only entries can exercise every plan but
are structurally excluded from release assets.

The five accepted maps are additionally frozen by
`data/frozen_map_baselines.json`. The byte SHA-256/size is the acceptance gate;
the normalized semantic hash is a diagnostic covering entity names/classes,
ordered targets, bindParent, layers, transforms, AP IDs, manifests and scripted
contracts while ignoring only comments and irrelevant whitespace.

When the installed game revision changes:

1. Extract the relevant `*.resources` or `*.resources.backup` files again.
2. Decompress them into `vanillamaps/<map>.map`.
3. Confirm the dump does not contain `AP_CHECK_`, `ap_rpc_v3_`, `ap_notify_`,
   `ap_event_`, or `ap_rpc_auto_enable`.
4. Update `data/map_sources.json` with the new `source_sha256` and
   `supported_game_revision`.
5. Re-run `./validate_all.sh` and `./build_playable_test.sh`.

If the installed resource is already contaminated by a previous AP patch, use
the clean `*.backup` variant or another known-vanilla dump before updating the
registry.

Playtests are manual. Build the playable bundle with `build_playable_test.sh`,
then install and run it through the normal local workflow. Do not commit local
paths, configuration, passwords, seeds, runtime output or logs.

### New-map onboarding

For each future map: back up the end-of-previous-mission save; add one registry
entry; audit the vanilla owner/target graph with `map_preflight.py`; add proven
APWorld locations/IDs; generate only that map while comparing all frozen
baselines; run a short transition test from the saved final checkpoint; freeze
the passed map; then continue. Keep one final save per mission. Doom Hunter Base
uses the existing end-of-Cultist save. Developer test scripts and
checklists never enter the player package.

The preflight is fail-closed for source/config/manifest/container proof,
source hash and resource priority, unique entity/AP identities, every original
target and reward/progression edge, explicit drops, bind/local transform,
layers/checkpoints/movers/gates, conditional pickup behavior, Mission Complete,
and replacement-owner proof for any DECL resource. Unknown or unused fields
fail validation; unclassified targets are never copied automatically.

### APWorld prerequisite registry

Rules live only in `worlds/doometernal/logic.py`; every direct prerequisite is
also forbidden at its own location. Current exact table:

- Armored Rain → Flame Belch; All Mission Challenges Completed `7770141` → Flame Belch.
- Sticky Bombs → Sticky Bombs; Full Auto → Full Auto.
- Precision Bolt/Micro Missiles → Heavy Cannon + matching mod.
- Heat Blast/Microwave Beam → Plasma Rifle + matching mod.
- Lock-on Burst/Remote Detonate → Rocket Launcher + matching mod.
- Destroyer Blade/Arbalest → Ballista + matching mod.
- Mobile Turret/Energy Shield → Chaingun + matching mod.

No rule is proven for Pull the Crystal or Master of Turrets. Mastery reward
items do not satisfy natural challenges, and no unrelated location exclusions
are applied.

Meat Hook Mastery is the explicit external-vanilla exception: the mandatory
Cultist Base scripted Super Shotgun/Meat Hook sequence guarantees the capability,
which has no active AP pool representation. It therefore has neither an AP
access rule nor a direct placement exclusion; validated metadata records the
exact source and rejects use for any capability active in the AP pool.

`build_playable_test.sh` produces:

```text
DoomEternalArchipelagoPlayableTest-v0.3.1-alpha.zip
├── README.md
├── RELEASE_MANIFEST.json
├── DoomEternalArchipelagoPreAlpha.zip
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

The release intentionally excludes:

- APWorld source code;
- C++ source;
- map generator source;
- tests;
- `level_configs/`;
- extraction/compression tooling;
- project memory files;
- personal config.


## Installation

> [!IMPORTANT]
> The Archipelago client directory, the DOOM Eternal game directory, and the
> DOOM Eternal save directory are three different paths. Do not point all
> settings to the DOOM Eternal installation folder.

> [!WARNING]
> Extract every new release into a brand-new empty directory. Do not copy a new
> version over an older extracted PTB.

### Before you begin

You need three separate things:

- the extracted PTB release directory;
- the real DOOM Eternal game installation;
- the real DOOM Eternal save directory.

You will also install:

- the APWorld file `doometernal.apworld`;
- Meathook `v7.2`;
- the mod ZIP `DoomEternalArchipelagoPreAlpha.zip`.

### The three paths are different

| Setting or file | What it must point to | Typical Windows example |
| --- | --- | --- |
| `doom_eternal_options.client_directory` | Extracted release `client/` folder containing `bridge_client.py`, `bridge_identity.json` and `ap_client.exe` | `C:\Games\DoomEternalArchipelago-v0.2.1\client` |
| Game Base Path | DOOM Eternal `base/` folder containing `classicwads` | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base` |
| Saved Games Path | DOOM Eternal save `base/` folder | `C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base` |
| `XINPUT1_3.dll` | Real game root, beside `DOOMEternalx64vk.exe` | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\XINPUT1_3.dll` |
| `DoomEternalArchipelagoPreAlpha.zip` | DOOM Eternal `Mods/` folder, still zipped | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\Mods\DoomEternalArchipelagoPreAlpha.zip` |

Examples:

```text
CLIENT DIRECTORY:
C:\Games\DoomEternalArchipelago-v0.3.1\client

GAME BASE PATH:
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base

SAVED GAMES PATH:
C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base
```

**These paths are not interchangeable.**

### 1. Extract the PTB

Locate:

```text
DoomEternalArchipelago-v0.3.1-alpha.zip
```

Extract it into a brand-new empty directory.

Do not:

- run files directly from inside the ZIP;
- extract the PTB over an older PTB directory;
- copy a new PTB on top of an older extracted PTB;
- extract `DoomEternalArchipelagoPreAlpha.zip`.

Expected layout after extraction:

```text
DoomEternalArchipelago-v0.3.1/
├── doometernal.apworld
├── DoomEternalArchipelagoPreAlpha.zip
├── README.md
└── client/
    ├── bridge_client.py
    ├── ap_client.exe
    ├── start_injector_windows.bat
    ├── run_bridge.sh
    └── ap_config.example.json
```

### 2. Install the APWorld

1. Open `doometernal.apworld` with `ArchipelagoLauncher`.
2. Close the launcher completely.
3. Start `ArchipelagoLauncher` again.
4. Confirm that `DOOM Eternal Client` now appears in the launcher.

### 3. Set the Archipelago client directory

When you start `DOOM Eternal Client`, Archipelago needs the location of the
external PTB client files.

The correct path ends with:

```text
\client
```

The selected directory must contain:

- `bridge_client.py`
- `ap_client.exe`
- `start_injector_windows.bat`

Correct example:

```text
C:\Games\DoomEternalArchipelago-v0.3.1\client
```

Incorrect examples:

```text
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal
C:\Games\DoomEternalArchipelago-v0.3.1
```

Why those are wrong:

- the first path is the game installation directory;
- the second path is the parent PTB directory;
- neither one is the extracted `client/` directory.

### 4. Configure the game and save paths

On first launch, `DOOM Eternal Client` will ask for:

- Game Base Path
- Saved Games Path

Game Base Path must end with:

```text
DOOMEternal\base
```

That directory must contain `classicwads`.
`DOOMEternalx64vk.exe` is one level above it.

Valid Windows example:

```text
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base
```

Saved Games Path example:

```text
C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base
```

To open the Windows Saved Games directory quickly:

1. Press `Win + R`.
2. Type `shell:SavedGames`.
3. Open `id Software`.
4. Open `DOOMEternal`.
5. Open `base`.

The setup writes:

```text
client/ap_config.json
```

Do not:

- reuse someone else's `ap_config.json`;
- copy a config from another computer;
- assume an old config is valid after moving folders.

If `ap_client.exe` was started before setup completed, close it and restart it
after `client/ap_config.json` exists.

If the native client starts before `ap_config.json` exists, the expected
warning is:

```text
Config not found yet. Run/setup the DOOM Eternal Client once, then restart ap_client.exe if needed.
```

Example `ap_config.json`:

```json
{
  "doom_base_dir": "C:\\Program Files (x86)\\Steam\\steamapps\\common\\DOOMEternal\\base",
  "save_games_dir": "C:\\Users\\YOUR_NAME\\Saved Games\\id Software\\DOOMEternal\\base"
}
```

### 5. Install Meathook

Use exactly this release:

https://github.com/brongo/m3337ho0o0ok/releases/tag/v7.2

On Windows, `XINPUT1_3.dll` must be placed beside:

```text
DOOMEternalx64vk.exe
```

Correct Windows layout:

```text
DOOMEternal/
├── DOOMEternalx64vk.exe
├── XINPUT1_3.dll
├── base/
└── Mods/
```

Do not place `XINPUT1_3.dll` in:

- `DOOMEternal\base`
- the extracted PTB `client/` directory on Windows

### 6. Install the map mod

Windows injector:

https://gamebanana.com/tools/7475

Linux injector:

https://github.com/leveste/EternalBasher/releases/tag/v6.66-rev3.12

Copy:

```text
DoomEternalArchipelagoPreAlpha.zip
```

into:

```text
DOOMEternal/Mods/
```

Keep it as a ZIP.

Do not:

- extract `DoomEternalArchipelagoPreAlpha.zip`;
- install loose `.entities` files;
- leave an older mod ZIP active without reinjecting after updates.

Run the injector again whenever you update the mod ZIP.

### 7. Start the clients on Windows

Startup order:

1. Open `Archipelago Launcher`.
2. Open `DOOM Eternal Client`.
3. Connect to the Archipelago server.
4. Start DOOM Eternal through Steam.
5. Run:

```text
client\start_injector_windows.bat
```

6. Enter normal gameplay.
7. Wait for the memory gate to open.
8. Play.

Only one `ap_client.exe` should be running at a time.

### 8. Start the clients on Linux / Proton

Set DOOM Eternal's Steam launch options to:

```text
WINEDLLOVERRIDES="XINPUT1_3=n,b" AP_CLIENT_DELAY=5 "/absolute/path/to/client/run_bridge.sh" %command%
```

Requirements:

- use an absolute path;
- do not use only `~/...`;
- place `XINPUT1_3.dll` beside `DOOMEternalx64vk.exe`.

Typical Linux / Proton paths:

```text
Game Base Path: /path/to/steamapps/common/DOOMEternal/base
Saved Games Path: /path/to/steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base
```

Typical Bazzite-style examples:

```text
Game Base Path: /var/home/YOUR_NAME/.local/share/Steam/steamapps/common/DOOMEternal/base
Saved Games Path: /var/home/YOUR_NAME/.local/share/Steam/steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base
```

### Correct directory layouts

Windows:

```text
C:\Games\DoomEternalArchipelago-v0.3.1\
├── doometernal.apworld
├── DoomEternalArchipelagoPreAlpha.zip
├── README.md
└── client/
    ├── bridge_client.py
    ├── ap_client.exe
    ├── start_injector_windows.bat
    ├── run_bridge.sh
    └── ap_config.example.json

C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\
├── DOOMEternalx64vk.exe
├── XINPUT1_3.dll
├── base/
└── Mods/
    └── DoomEternalArchipelagoPreAlpha.zip
```

### How to verify the installation

Native log path:

```text
<DOOM Eternal>\base\ap_client.log
```

Expected lines:

```text
PTB version: v0.3.1-alpha
Meathook RPC server verified.
RPC memory gate OPEN
```

`Memory state unavailable` can appear temporarily in menus, loading screens, or
transitions. It should not remain stuck during normal gameplay.

### Troubleshooting

#### DOOM Eternal Client files not found

If the message says it searched in:

```text
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal
```

then the game directory was configured as the client directory.

Fix:

1. Open `Archipelago Launcher` and open its Settings menu.
2. Find:

```text
doom_eternal_options.client_directory
```

3. Point it to the extracted release `client/` directory. Do not point at a
   checkout, Downloads, or `build/playable-test`; launcher does not fallback.
4. Save the settings.
5. Close `Archipelago Launcher` completely.
6. Start it again.
7. Launch `DOOM Eternal Client`.

If you cannot find the setting in the UI, search for `Open host.yaml` in the
Launcher and edit:

```yaml
doom_eternal_options:
  client_directory: "C:/Games/DoomEternalArchipelago-v0.3.1/client"
```

Correct fallback in `host.yaml`:

```yaml
doom_eternal_options:
  client_directory: "C:/Games/DoomEternalArchipelago-v0.3.1/client"
```

Forward slashes are valid in Windows YAML paths.

Do not use:

```yaml
doom_eternal_options:
  client_directory: "C:/Program Files (x86)/Steam/steamapps/common/DOOMEternal"
```

#### Missing `libstdc++-6.dll` or `libgcc_s_seh-1.dll`

Do not download random DLLs.

If these errors appear, the installation probably mixed files from different
PTB versions or reused an old extracted directory.

Fix:

1. Delete the extracted pre-alpha directory.
2. Extract `DoomEternalArchipelago-v0.3.1-alpha.zip` again into a
   brand-new empty directory.
3. Confirm that `client/` does not contain:
   - `version.dll`
   - `dinput8.dll`
   - `dxgi.dll`
   - `xinput1_4.dll`


## Current PTB logic

- `randomize_chainsaw` defaults to `false`.
- `randomize_dash` defaults to `false` and remains experimental.
- `randomize_first_battery` defaults to `false`. When enabled, the mandatory
  first Sentinel Battery is shuffled into the item pool instead of being
  locked to its Exultia pickup (`7770084`); the pickup remains an active,
  normally randomized AP location. In both modes, two randomized `Sentinel
  Battery Bundle (2)` items keep the total at five Batteries.
  All four physical Battery pickups remain independent AP locations with zero
  vanilla Battery grants. Their current persistent Automap carrier regression
  is a release blocker, not accepted behavior.
- Super Shotgun is currently kept vanilla/scripted in Cultist Base, this is
  due to the fact that the SSG is given by a cutscene, not a pickup. This
  fix is already being worked on, and is due to be released in a future version.
- Meat Hook is not a separate PTB item because Super Shotgun grants it by
  default.
- Secret Encounters are AP checks in Exultia and Cultist Base.
- Mission Complete identity exists for Hell, Exultia and Cultist Base; only
  Cultist also has independent goal.
- Mastery and Mission Challenge locations use durable native save records; the
  three Cultist challenge children keep `currencyToGive.num = 0`, including the
  accepted aggregate reward suppression.
- Weapon Point rewards remain fully vanilla in 0.3.0. Safe conversion is
  deferred until the project owns an in-process, revision-gated hook host.
- The first Fortress Praetor token keeps its native Suit bootstrap and AP
  check, but grants zero vanilla Praetor currency.

## Known probable issues

- Rune slots and equipped-state visuals may occasionally appear inconsistent,
  even when Rune effects are active. Planned for a future fix.
- Sentinel Battery counter may visually reset to 0 even while gameplay balance
  remains available. Socket requirements are authoritative.
- Reloading a checkpoint may still recover rare vanilla scripting desyncs,
  and most other problems, always try reloading a checkpoint.
- There's no current way to identify what you sent/received. A second screen
  dedicated to watching the AP client is recommended.
- The Rocket launcher pickup may seem buggy, but it works.
- Deathlink is currently "hardcore". If you have extra lives, it will ignore
  them, killing you anyway.

## Roadmap

### 0.1.1 PTB — Runtime stabilization — DONE

- Freeze the current route through Cultist Base: `80` map checks plus `1`
  runtime goal.
- Pin the validated Meathook and mod-installation versions and improve local
  diagnostic logs.
- Prevent silent item loss on RPC failure, add recovery behavior, and complete
  Windows/Linux smoke testing.

### 0.1.2 PTB — Windows client hotfix — DONE

- Removed bundled proxy DLL aliases from the external client directory.
- Fixed the Windows `ap_client.exe` startup failure.
- Added PE dependency and DLL-shadowing validation to release builds.
- Preserved the existing `v0.1.x` gameplay scope and item IDs.

### 0.2.0 - 0.2.1 Pre-Alpha — Campaign expansion & Optional systems foundation — DONE

- Keep the same route and avoid adding new maps during inicial stages.
- Expand Secret Encounters, mission-completion plumbing, and optional reward
  systems only where native hooks are trustworthy.
- Investigate Suit Points, Suit Upgrades, Sentinel Batteries, and future
  Slayer Gate/Empyrean Key support without freezing their final balance model
  yet.
- Add the remaining base-game missions incrementally using the existing
  `.entities` condumps.
- Generalize manifests, mission-completion checks, region rules, and per-map
  validation.
- Expand location and item coverage without treating balance or compatibility
  as final.

### 0.3.x–0.5.x Alpha — Full base campaign — IN PROGRESS

- Make all `13` base-game missions playable end to end.
- Complete progression logic, persistent upgrades, optional checks, and the
  base-game Unmaykr Protocol goal.
- Reach feature-complete base-game scope by `0.5.x`, with no known progression
  blockers in the default configuration.

### 0.7.x Beta — Content freeze and polish

- Freeze the planned `1.0` scope and stabilize IDs and data formats where
  possible.
- Focus on balance, installation, compatibility, save/reconnect behavior,
  discoverability, and broader community testing.
- Finish documentation, diagnostics, and support tooling.

### 0.9.x Release Candidate

- Ship the intended `1.0` feature set for final validation.
- Accept only blocker and regression fixes; no major systems or content
  expansion.

### 1.0

- Stable public release of the complete base campaign.
- Base-game Unmaykr Protocol goal:
  `Slayer Gate Keys -> Slayer Gates -> Empyrean Keys -> Unmaykr -> Final Sin`.
- Public documentation, release packaging, and Archipelago community
  announcement.

### Post-1.0 / 2.0

- The Ancient Gods campaigns and Seal Hunt goal.
- Mission Access items.
- Horde Mode and Master Levels.
- Enemy randomizer.
- Hard Mode / checkpoint removal.
- Starting inventory and starting weapon options.

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
