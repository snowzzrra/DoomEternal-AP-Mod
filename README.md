# Doom Eternal Archipelago

Game-side repository for the DOOM Eternal Archipelago integration.

This repo owns the playable-test mod package, the Python bridge, the external
RPC client, runtime manifests, map-generation scripts, validation scripts, and
release packaging. The APWorld source does **not** live here; it stays in the
sibling `Archipelago/worlds/doometernal/` checkout and is compiled into
`doometernal.apworld` during release builds.

> [!CAUTION]
> This project is a playable test build, not a finished 1.0 release. Windows is
> the primary target for public testing, while Linux/Proton remains supported
> for development and early validation.

## Project status

Current PTB scope:

- Route: `Hell on Earth -> Fortress visit 1 -> Exultia -> Fortress visit 2 -> Cultist Base`
- Content: `78` map checks + `1` runtime goal
- Goal: report completion when the runtime sees
  `ap_transition_e1m3_cult_to_e1m4_boss.evt`
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
- The runtime goal uses the native transition event above, with the old
  autosave path kept only as fallback behavior.
- `g_debugTriggers` is no longer required for normal check detection.

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

### Goal flow

```text
ap_client.exe detects:
game/sp/e1m3_cult/e1m3_cult -> game/sp/e1m4_boss/e1m4_boss

  -> writes ap_transition_e1m3_cult_to_e1m4_boss.evt
  -> bridge_client.py consumes it durably
  -> sends Cultist Base - Mission Complete
  -> sends CLIENT_GOAL
```

## Release package

`build_playable_test.sh` produces:

```text
DoomEternalArchipelagoPlayableTest-v0.1.2-ptb.zip
├── README.md
├── RELEASE_MANIFEST.json
├── DoomEternalArchipelagoPreAlpha.zip
├── doometernal.apworld
└── client/
    ├── ap_client.exe
    ├── ap_logger.exe
    ├── bridge_client.py
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

The PTB client directory intentionally does **not** bundle Meathook proxy DLL
aliases such as `version.dll`, `dxgi.dll`, `dinput8.dll`, or `xinput1_4.dll`.
Those names can shadow Windows system DLL imports for `ap_client.exe`.

## Installation

> [!IMPORTANT]
> The Archipelago client directory, the DOOM Eternal game directory, and the
> DOOM Eternal save directory are three different paths. Do not point all
> settings to the DOOM Eternal installation folder.

> [!WARNING]
> Extract every new release into a brand-new empty directory. Do not copy a new
> version over an older extracted PTB.

Hotfix note for `v0.1.2-ptb`:

`Fixed a Windows startup failure caused by a bundled proxy DLL shadowing the system VERSION.dll required by ap_client.exe.`

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
| `doom_eternal_options.client_directory` | Extracted PTB `client/` folder containing `bridge_client.py` and `ap_client.exe` | `C:\Games\DoomEternalArchipelago-v0.1.2\client` |
| Game Base Path | DOOM Eternal `base/` folder containing `classicwads` | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base` |
| Saved Games Path | DOOM Eternal save `base/` folder | `C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base` |
| `XINPUT1_3.dll` | Real game root, beside `DOOMEternalx64vk.exe` | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\XINPUT1_3.dll` |
| `DoomEternalArchipelagoPreAlpha.zip` | DOOM Eternal `Mods/` folder, still zipped | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\Mods\DoomEternalArchipelagoPreAlpha.zip` |

Examples:

```text
CLIENT DIRECTORY:
C:\Games\DoomEternalArchipelago-v0.1.2\client

GAME BASE PATH:
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base

SAVED GAMES PATH:
C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base
```

**These paths are not interchangeable.**

### 1. Extract the PTB

Locate:

```text
DoomEternalArchipelagoPlayableTest-v0.1.2-ptb.zip
```

Extract it into a brand-new empty directory.

Do not:

- run files directly from inside the ZIP;
- extract the PTB over an older PTB directory;
- copy a new PTB on top of an older extracted PTB;
- extract `DoomEternalArchipelagoPreAlpha.zip`.

Expected layout after extraction:

```text
DoomEternalArchipelago-v0.1.2/
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

The `client/` directory in `v0.1.2-ptb` must not contain:

- `version.dll`
- `dinput8.dll`
- `dxgi.dll`
- `xinput1_4.dll`

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
C:\Games\DoomEternalArchipelago-v0.1.2\client
```

Incorrect examples:

```text
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal
C:\Games\DoomEternalArchipelago-v0.1.2
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
WINEDLLOVERRIDES="XINPUT1_3=n,b" AP_CLIENT_DELAY=15 "/absolute/path/to/client/run_bridge.sh" %command%
```

Requirements:

- use an absolute path;
- do not use only `~/...`;
- raise `AP_CLIENT_DELAY` to `20` if shader startup is slow;
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
C:\Games\DoomEternalArchipelago-v0.1.2\
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
PTB version: v0.1.2-ptb
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

3. Point it to the extracted PTB `client/` directory.
4. Save the settings.
5. Close `Archipelago Launcher` completely.
6. Start it again.
7. Launch `DOOM Eternal Client`.

If you cannot find the setting in the UI, search for `Open host.yaml` in the
Launcher and edit:

```yaml
doom_eternal_options:
  client_directory: "C:/Games/DoomEternalArchipelago-v0.1.2/client"
```

Correct fallback in `host.yaml`:

```yaml
doom_eternal_options:
  client_directory: "C:/Games/DoomEternalArchipelago-v0.1.2/client"
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

1. Delete the extracted PTB directory.
2. Extract `DoomEternalArchipelagoPlayableTest-v0.1.2-ptb.zip` again into a
   brand-new empty directory.
3. Confirm that `client/` does not contain:
   - `version.dll`
   - `dinput8.dll`
   - `dxgi.dll`
   - `xinput1_4.dll`

### Final checklist

- [ ] PTB extracted into a brand-new directory
- [ ] `doometernal.apworld` installed
- [ ] Launcher restarted
- [ ] `client_directory` ends in `/client`
- [ ] client directory contains `bridge_client.py`
- [ ] client directory contains `ap_client.exe`
- [ ] Game Base Path ends in `DOOMEternal/base`
- [ ] Saved Games Path ends in `DOOMEternal/base`
- [ ] Meathook `v7.2` installed
- [ ] `XINPUT1_3.dll` is beside `DOOMEternalx64vk.exe`
- [ ] mod ZIP is inside `Mods/`
- [ ] mod ZIP was not extracted
- [ ] injector was executed
- [ ] only one `ap_client.exe` is open
- [ ] log shows `v0.1.2-ptb`
- [ ] log shows `Meathook RPC server verified`
- [ ] during gameplay, log shows `RPC memory gate OPEN`

## Current PTB logic

- `randomize_chainsaw` defaults to `false`.
- `randomize_dash` defaults to `false` and remains experimental.
- `randomize_first_battery` defaults to `false` and remains experimental.
- Super Shotgun is currently kept vanilla/scripted in Cultist Base.
- Meat Hook is not a separate PTB item because Super Shotgun grants it by
  default.
- Empyrean Key reward chest is outside PTB scope; Exultia uses the physical
  `Slayer Gate Key` pickup instead.
- Rocket Launcher is in PTB scope, but Cultist Base’s scripted route can still
  require checkpoint recovery if the player already owns it.

## Known issues

- DeathLink can currently burn through all available Extra Lives before the
  run stabilizes.
- Dash and Blood Punch pickups are functional, but their presentation can be
  partially buried in world geometry.
- Part of the Sentinel Crystal pedestal visuals still remains even when the
  pickup is randomized.
- `randomize_dash` and `randomize_first_battery` should still be treated as
  experimental when exposed.
- Runes received mid-map may only become equipable after a map load or
  checkpoint reload.
- Restarting only the game can leave the read-only memory gate unable to
  reopen. Relaunch the full stack if that happens.
- AP pickups currently disappear from the automap.
- If Ice Bomb exists without Frag Grenade, the HUD slot can disappear even
  though the item still functions.
- Sentinel Battery socket feedback is not trustworthy for validation.
- The Cultist Base Super Shotgun sequence is still effectively vanilla and not
  part of the PTB randomization path.
- The Cultist Base Rocket Launcher route can still stall a scripted door if the
  player already owns the weapon early; checkpoint restart is the current
  recovery.
- Secret Encounters and Mission Challenges are not AP checks in the PTB.

## Roadmap

### 0.1.1 PTB — Runtime stabilization

- Freeze the current route through Cultist Base: `78` map checks plus `1`
  runtime goal.
- Pin the validated Meathook and mod-installation versions and improve local
  diagnostic logs.
- Prevent silent item loss on RPC failure, add recovery behavior, and complete
  Windows/Linux smoke testing.

### 0.1.2 PTB — Windows client hotfix

- Removed bundled proxy DLL aliases from the external client directory.
- Fixed the Windows `ap_client.exe` startup failure.
- Added PE dependency and DLL-shadowing validation to release builds.
- Preserved the existing `v0.1.x` gameplay scope and item IDs.

### 0.2.x Pre-Alpha — Campaign expansion

- Add the remaining base-game missions incrementally using the existing
  `.entities` condumps.
- Generalize manifests, mission-completion checks, region rules, and per-map
  validation.
- Expand location and item coverage without treating balance or compatibility
  as final.

### 0.3.x–0.5.x Alpha — Full base campaign

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
- tastyfresh for the original large check list used to bootstrap the project.
- zwip zwap zapony for direct technical guidance and map/runtime research.
- alby for technical help, runtime investigation, and safe-native-behavior
  guidance.
- chrispy for creating
  [Meathook](https://github.com/brongo/m3337ho0o0ok), the RPC foundation this
  project builds on.
- PowerBall253 / brunoanc for
  [EternalResourceExtractor](https://github.com/brunoanc/EternalResourceExtractor)
  and `idFileDeCompressor`.
- FlavorfulGecko5 and the EntitySlayer contributors for
  [EntitySlayer](https://github.com/FlavorfulGecko5/EntitySlayer).
- The DOOM 2016+ Modding community for EternalModInjector, wiki material, and
  reverse-engineering knowledge that made the map patches possible.
