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
DoomEternalArchipelagoPlayableTest-v0.1.1-ptb.zip
├── README.md
├── RELEASE_MANIFEST.json
├── DoomEternalArchipelagoPreAlpha.zip
├── doometernal.apworld
└── client/
    ├── ap_client.exe
    ├── ap_logger.exe
    ├── bridge_client.py
    ├── dinput8.dll
    ├── dxgi.dll
    ├── version.dll
    ├── xinput1_4.dll
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

The bundled Meathook runtime DLLs are included only for convenience in this PTB
package. Upstream credit remains with the original Meathook project listed in
the credits.

## Install from the PTB ZIP

1. Extract `DoomEternalArchipelagoPlayableTest-v0.1.1-ptb.zip` to a permanent
   directory.
2. Open `doometernal.apworld` with `ArchipelagoLauncher`, then restart the
   launcher.
3. Open `DOOM Eternal Client` from Archipelago Launcher and configure the game
   base path plus the save-games path on first launch.
4. If `ap_client.exe` or `run_bridge.sh` was started before that config
   existed, close the external client after setup and restart it later with the
   game.
5. Certify you installed the correct Meathook DLL version [here](https://github.com/brongo/m3337ho0o0ok/releases/tag/v7.2).
6. Copy `DoomEternalArchipelagoPreAlpha.zip` into DOOM Eternal's `Mods`
   directory and install it with EternalModInjector [Windows](https://gamebanana.com/tools/7475) or [Linux](https://github.com/leveste/EternalBasher/releases/tag/v6.66-rev3.12).
7. Keep the ZIP intact. Do not install loose `.entities` files.

Do not reuse someone else's `ap_config.json`. Use the setup wizard or copy
`client/ap_config.example.json` to `client/ap_config.json` and fill your own
paths.

If the native client starts before `ap_config.json` exists, the log now emits
an actionable warning instead of treating that first run as a permanent
failure:

```text
Config not found yet. Run/setup the DOOM Eternal Client once, then restart ap_client.exe if needed.
```

### Linux / Proton

Set DOOM Eternal's Steam launch options to:

```text
WINEDLLOVERRIDES="XINPUT1_3=n,b" AP_CLIENT_DELAY=15 "/path/to/run_bridge.sh" %command%
```

Use the absolute path to the extracted `client/run_bridge.sh`. Place the
validated `XINPUT1_3.dll` beside `run_bridge.sh` and `ap_client.exe` in the
extracted `client/` directory. If Proton shader compilation is slow on your
machine, raise the delay to `20`.

For Linux/Proton, the validated layout is:

```text
<extracted PTB>/client/run_bridge.sh
<extracted PTB>/client/ap_client.exe
<extracted PTB>/client/XINPUT1_3.dll
```

The Proton-compatible preflight now logs both DLL candidates:

- game-root candidate: `<DOOM root>/XINPUT1_3.dll`
- client-local candidate: `<PTB client dir>/XINPUT1_3.dll`

If the game-root DLL is missing, a client-local DLL is accepted when Proton
mode is detected from `WINEDLLOVERRIDES` or related Wine/Proton signals.

Typical first-run paths:

```text
Game Base Path: /path/to/steamapps/common/DOOMEternal/base
Saved Games Path: /path/to/steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base
```

### Windows

For Windows/native installs, keep `XINPUT1_3.dll` beside
`DOOMEternalx64vk.exe` in the real DOOM Eternal game directory.

Typical first-run paths:

```text
Game Base Path: C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base
Saved Games Path: C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base
```

Start DOOM normally through Steam, then run:

```text
client\start_injector_windows.bat
```

That helper starts the external RPC client `ap_client.exe` with the correct
working directory. Only one `ap_client.exe` should exist at a time.

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
