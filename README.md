# DOOM Eternal Archipelago

> [!CAUTION]
> This project is a beta, not a finished 1.0 release. Windows and Linux/Proton
> are supported.

Found a bug? Use **Report a Problem** in the launcher or [open a GitHub issue](https://github.com/snowzzrra/DoomEternal-AP-Mod/issues/new/choose).

[What gets randomized](#1-what-gets-randomized) · [Player options](#2-player-options) · [Installation](#3-installation) · [How it works](#4-how-it-works) · [Version scope](#5-version-scope) · [Credits](#6-credits) · [License](#7-license)

## 1. What gets randomized

Play DOOM Eternal in an [Archipelago](https://archipelago.gg/) multiworld.
Collectibles, fights and campaign objectives give items to players across the room;
their discoveries can send weapons, abilities and upgrades back to you. You can
also generate a solo room.

Choose a campaign from the base game and The Ancient Gods, shuffle mission order
or unlock missions with Access items. Enemy Randomizer changes the enemies you
fight. Killsanity adds rewards as your kill counter reaches each target.

## Start playing

You need a Steam copy of DOOM Eternal, the DLC used by your room, and the matching
DOOM Eternal Archipelago release. Keep the launcher and its `client` folder together.

1. Download and extract the release for your operating system.
2. If you are generating the room, install its `doometernal.apworld` in Archipelago
   and use **CREATE PLAYER** in the DOOM launcher to save your player YAML.
3. In the DOOM launcher, choose **JOIN ROOM** and enter the server and player name.
4. Install the room mod when prompted, then start DOOM Eternal through Steam.
   Keep the launcher open while playing.

Follow [Install and connect](docs/INSTALL.md) for the complete Windows or Linux/Proton
instructions. Joining an existing room does not require a YAML file.

## Choose your game

Set mission selection, starting equipment, victory objectives and enemy settings
before generating the room. The launcher shows those choices under **CREATE PLAYER**.
Once connected, **VIEW ALL SETTINGS** shows the room's choices.

See [Gameplay and options](docs/GAMEPLAY.md) for mission access, enemy settings,
kill rewards, DeathLink and inventory recovery. The options table below might help too.

## 2. Player options

| Option name | Default | Summary | Other values |
|---|---|---|---|
| Progression Balancing | normal | Moves progression earlier when generation needs it. | 0–99; Disabled; Extreme; Normal |
| Accessibility | Full | Sets which generated objectives must be reachable. | Full; Minimal |
| Death Link | False | Shares deaths with other DeathLink players. | Off; On |
| Use DLC Content | True | Adds TAG equipment and Support Runes to the item catalog. | Off; On |
| Mission Pool | Full Saga | Selects eligible missions. | Base; Full Saga; DLC Only; Custom |
| Custom Missions | ARC Complex, Cultist Base, Doom Hunter Base, Exultia, Final Sin, Hell on Earth, Immora, Mars Core, Nekravol, Nekravol Part II, Reclaimed Earth, Sentinel Prime, Super Gore Nest, Taras Nabad, The Blood Swamps, The Holt, The World Spear, UAC Atlantica Facility, Urdak | Missions included when Mission Pool is set to Custom. | ARC Complex; Cultist Base; Doom Hunter Base; Exultia; Final Sin; Hell on Earth; Immora; Mars Core; Nekravol; Nekravol Part II; Reclaimed Earth; Sentinel Prime; Super Gore Nest; Taras Nabad; The Blood Swamps; The Holt; The World Spear; UAC Atlantica Facility; Urdak |
| Custom Dark Lord | True | Include The Dark Lord when Mission Pool is set to Custom. | Off; On |
| Mission Count | all | Sets the number of included missions. | 3–19; All |
| DLC Logic Timing | Late Game | Choose when The Ancient Gods can enter your Archipelago progression. | Late Game; From the Beginning |
| Mission Order | Random Mission Order | Selects progression through included missions. | Vanilla Order; Random Mission Order; Mission Access as Items |
| Campaign Difficulty | Ultra Violence | Sets the campaign difficulty for this player. | Im Too Young To Die; Hurt Me Plenty; Ultra Violence; Nightmare |
| Starting Missions | 1 | Sets the number of initially accessible missions. | 1–3 |
| Full Saga Final Boss | Random | Reserve the final Full Saga encounter. Vanilla Order always ends at Davoth. | Random; Icon of Sin; Davoth / The Dark Lord |
| Goal Mission as Item | False | In Access mode, place the selected boss stage's concrete Access item. | Off; On |
| Goal | Acquire the Unmaykr | Selects the campaign victory objective. | Acquire the Unmaykr; Kill the Icon of Sin; Kill the Dark Lord; Complete the Full Saga |
| Additional Victory Requirements | Complete All Escalation Encounters, Complete All Included Missions, Complete All Slayer Gates | Adds selected objectives to victory. | Acquire the Unmaykr; Complete All Escalation Encounters; Complete All Included Missions; Complete All Mission Challenges; Complete All Secret Encounters; Complete All Slayer Gates; Complete All Weapon Mastery Challenges |
| Special Weapon | Progressive Special Weapon | Selects the Crucible or progressive special weapons. | Progressive Special Weapon; Progressive Sentinel Hammer; The Crucible |
| DeathLink Mode | Soft | Selects which deaths count for DeathLink. | Soft; Hardcore |
| Enhanced Melee Damage | False | Strengthens normal punches. | Off; On |
| Randomize Chainsaw | False | Places Chainsaw in the item pool. | Off; On |
| Randomize Dash | False | Places Dash in the item pool and enables Dash access logic. | Off; On |
| Randomize First Sentinel Battery | False | When enabled, the mandatory first Sentinel Battery is shuffled into the | Off; On |
| Include Weapon Mastery Challenges | True | Includes weapon mastery locations. | Off; On |
| Reveal AP Locations on Automap | False | Shows AP location markers on Automap. | Off; On |
| Starting Weapon | Combat Shotgun | Selects the starting weapon. | Random; Heavy Cannon; Plasma Rifle; Rocket Launcher; Ballista; Chaingun; Combat Shotgun; Super Shotgun |
| Praetor Suit Upgrades in Pool | 21 | Sets the number of individual suit upgrades in the pool. | 0–21; Random |
| Trap Percentage | 10 | Replaces filler with enabled traps. | 0–100 |
| Enabled Traps | Ammo Drain Trap, Arachnotron Trap, Archvile Trap, BFG Drain Trap, Baron Trap, Carcass Trap, Cueball Trap, Dread Knight Trap, Fuel Drain Trap, Hell Knight Trap, Imp Trap, Marauder Trap, Revenant Trap, Tyrant Trap | Selects the eligible trap types. | Ammo Drain Trap; Arachnotron Trap; Archvile Trap; BFG Drain Trap; Baron Trap; Carcass Trap; Cueball Trap; Dread Knight Trap; Fuel Drain Trap; Hell Knight Trap; Imp Trap; Marauder Trap; Revenant Trap; Tyrant Trap |

## 3. Installation

See the [Windows and Linux/Proton installation guide](docs/INSTALL.md).

## 4. How it works

For development, read [Content and builds](docs/DEVELOPMENT.md) and
[Build a release](docs/RELEASING.md).

## 5. Version scope

## 0.1.1 — Runtime stabilization — DONE

- Froze the initial route through Cultist Base.
- Added RPC recovery, DeathLink, and Windows/Linux smoke coverage.

### 0.1.2 — Windows client hotfix — DONE

- Removed bundled proxy DLL aliases from the external client directory.
- Fixed Windows native-client startup and release dependency validation.

### 0.2.x — Campaign expansion and systems foundation — DONE

- Expanded the playable campaign route mission by mission while generalizing
  manifests, regions, mission completion, map checks, and per-map validation.
- Established the native `.entities` generation pipeline and shared runtime
  foundations used by locations, item delivery, and campaign options.

### 0.3.x Alpha — Base campaign map expansion — DONE

- `0.3.8`: Final Sin.
- `0.3.9`: complete base-campaign regression, cleanup, documentation, and
  release stabilization.

### 0.4.0 Beta — Installation, logic, and option architecture — DONE

- Room-specific Windows and Linux/Proton launcher workflow.
- Starting Weapon Choice, Random, and configurable starting inventory.
- Hard, Soft, and Combat logic profiles.
- Physical Chainsaw, Dash, and first Sentinel Battery options.
- Reveal AP Locations on Automap, mission replay Fast Travel and single-burst DeathLink.
- Help, setup check, Repair/Fix, support reports, resync, and room identity checks.

### 0.5.0 Beta — The Full Saga — DONE

- Base Campaign, The Ancient Gods Part One, and The Ancient Gods Part Two form
  one 19-mission Archipelago world with global inventory and completion flow.
- DLC-specific gear, mission logic, generation, runtime, and validation are in scope.

### 0.5.1-3 Beta — Release Correctives

- Base Campaign with DLC content packages Base locations only; room scope
  follows `include_dlc_missions` exactly.
- Windows pins EternalModInjector `2026-09-17` (GameBanana download `1819101`)
  with direct sandbox support and no sandbox hold workaround.

### 0.6.x Beta — Random Mission Order + Sentinel Core — DONE

- Develop Sentinel Core as an independent open-source native integration project for Archipelago.
- Use Sentinel Core for native RPC and telemetry, with a separate distribution containing the verified Windows Core/bootstrap pair. Broader platform integration requires validation.
- Turn mission access into Archipelago progression items.
- Generate a valid starting mission and reachable randomized mission order.

### 0.7.x Beta — Enemizer — Current

- Randomize enemy placements and encounters while keeping the CR formula.
- Add Killsanity, which gives checks per each X configurable demons killed.

### 0.8.x–0.9.x — Content freeze and polish

- Stabilize public IDs, contracts, and data formats for 1.0.
- Focus on balance, installation, compatibility, save/reconnect behavior,
  discoverability, and community testing.
- Complete documentation, diagnostics, support tooling, and release-candidate
  validation.
- Hard Mode and Master Levels.

### 1.0

- Stable public release of the supported campaigns and systems.

## 6. Credits

- The Archipelago project and contributors for the multiworld framework,
  protocol, server, and `CommonClient`.
- tastyfresh (from the AP After Dark Discord server) for the original
  large check list for project setup.
- Zwip Zwap Zapony (from the Doom 2016+ Modding Discord server) for direct
  technical guidance, map/runtime research and the [Windows Mod Injector](https://gamebanana.com/tools/7475).
- alby (from the Doom 2016+ Modding Discord server) for technical help, runtime
  investigation, and safe-native-behavior
  guidance.
- KaitoKid (from the AP After Dark Discord server) for the very detailed logic guidance.
- chrispy for creating
  [Meathook](https://github.com/brongo/m3337ho0o0ok), the RPC foundation this
  project builds on.
- PowerBall253 / brunoanc for
  [EternalResourceExtractor](https://github.com/brunoanc/EternalResourceExtractor)
  and `idFileDeCompressor`.
- FlavorfulGecko5 and the EntitySlayer contributors for
  [EntitySlayer](https://github.com/FlavorfulGecko5/EntitySlayer).
- leveste for [Eternal Mod Injector Shell](https://github.com/leveste/EternalBasher/).
- The DOOM Modding community for the wiki material, and general knowledge
  that made the map patches possible.
- Meta (from the AP After Dark Discord server) for the Archipelago Logo model
  and overall very useful help.
- FridgeDuck (from the AP After Dark Discord server) for the Doom Archipelago
  logo used by the AP client (both old and new) and the repository preview.

## 7. License

This project is distributed under the [MIT License](docs/LICENSE).

Sentinel Core distributions include their runtime manifest and license
notices. The launcher acquires compatible Core releases from `snowzzrra/Sentinel-Core` or 
uses a verified local distribution. External injectors are acquired from verified providers during setup
and are owned by their creators credited above.
