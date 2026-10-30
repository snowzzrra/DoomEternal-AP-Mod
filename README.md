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

Rooms can also be generated and hosted on [multiworld.gg](https://multiworld.gg/), which
DOOM Eternal supports.

## Choose your game

Set mission selection, starting equipment, victory objectives and enemy settings
before generating the room. The launcher shows those choices under **CREATE PLAYER**.
Once connected, **VIEW ALL SETTINGS** shows the room's choices.

See [Gameplay and options](docs/GAMEPLAY.md) for mission access, enemy settings,
kill rewards, DeathLink and inventory recovery. The options table below might help too.

## 2. Player options

| Option | Default | Summary | Values |
|---|---|---|---|
| **Campaign and Missions** | | | |
| Mission Pool | Full Saga | Choose which missions can appear in your campaign. | Base; Full Saga; DLC Only; Custom |
| Custom Missions | 19 selected | Pick the missions you want when Mission Pool is Custom. | any mission |
| Custom Dark Lord | On | Add The Dark Lord to your choices when Mission Pool is Custom. | Off; On |
| Mission Count | All | Choose how many missions to play in Random Mission Order or Mission Access as Items. | 6–19; All |
| Mission Order | Random Mission Order | Choose how you unlock missions. | Vanilla Order; Random Mission Order; Mission Access as Items |
| Starting Missions | 1 | Choose how many missions are open at the start in Mission Access as Items. | 1–3 |
| Goal | Acquire the Unmaykr | Choose what you need to do to win. | Acquire the Unmaykr; Kill the Icon of Sin; Kill the Dark Lord; Complete the Full Saga |
| Goal Mission as Item | Off | Unlock your goal's boss mission by finding its Access item in Mission Access as Items. | Off; On |
| Full Saga Final Boss | Randomized | Choose the last boss for the Complete the Full Saga goal. | Randomized; Icon of Sin; Davoth |
| Additional Victory Requirements | none | Choose extra tasks to finish before you win. | any of 7 |
| Include Slayer Gates | On | Earn Archipelago items by completing Slayer Gates and find their keys among shuffled items. | Off; On |
| Include Secret Encounters | On | Earn Archipelago items by completing the optional timed Secret Encounters. | Off; On |
| Include Weapon Mastery Challenges | On | Earn Archipelago items by completing the 13 weapon mastery challenges. | Off; On |
| **Items and Pool** | | | |
| Use DLC Content | On | Add The Ancient Gods weapons, Support Runes and enemy types to your campaign. | Off; On |
| Starting Weapon | Combat Shotgun | Choose the weapon you have when you start. | Random Weapon; Heavy Cannon; Plasma Rifle; Rocket Launcher; Ballista; Chaingun; Combat Shotgun; Super Shotgun |
| Special Weapon | Progressive Special Weapon | Choose which special weapon upgrades you can find. | Progressive Special Weapon; Progressive Sentinel Hammer; The Crucible |
| Randomize Chainsaw | Off | Choose where you find the Chainsaw. | Off; On |
| Randomize Dash | Off | Choose where you find Dash. | Off; On |
| Resource Profile | Balanced | Choose how many upgrades, spare resources and recovery items you can find. | Balanced; Generous; Lean |
| Item Pool Overrides | none | Choose exact amounts of individual upgrades and permanent items to place in your campaign. | per-item values |
| Praetor Suit Upgrades in Pool | Auto | Choose how many individual Praetor Suit upgrades you can find. | 0–17; Random; Auto |
| Filler Weights | none | Choose how often you find each helpful consumable in extra reward slots. | per-item values |
| Trap Percentage | 10 | Choose the percentage of extra item rewards that become traps. | 0–100 |
| Enabled Traps | 14 selected | Pick the traps that other players can send you. | any of 14 |
| Trap Weights | none | Choose how often each selected trap appears. | per-item values |
| **Enemies** | | | |
| Enemy Randomizer | On | Randomize the enemies you fight and vary their health, damage and size. | Off; On |
| Enemy Preset | Slayer's Path | Choose a starting setup for enemy randomization. | UAC Patrol; Sentinel Training; Slayer's Path; Hell's Vanguard; Icon's Wrath; Gore Nest; Gladiator's Gauntlet; Cultist Chaos; Oops! All Imps; Piñata Party; Custom |
| Early Intensity | Auto | Choose how tough randomized fights should be near the start when Enemy Count is Auto. | 1–10000; Auto |
| Late Intensity | Auto | Choose how tough randomized fights should become near the end when Enemy Count is Auto. | 1–10000; Auto |
| Density | Auto | Choose how many enemies can fight at the same time when Enemy Count is Auto. | 1–10000; Auto |
| Enemy Length | Auto | Choose the total enemies across each arena when Enemy Count is Auto. | 100–10000; Auto |
| Variety | Auto | Choose how many different enemy types you tend to face in the same fight. | 0–100; Auto |
| Enemy Count | Preset | Choose how to decide the number of randomized enemies in a fight. | Preset; Auto; Multiplier |
| Enemy Count Multiplier | Auto | Choose how many enemies to request when Enemy Count is Multiplier. | 1–10000; Auto |
| Size Variation Chance | Auto | Choose the chance for an enemy to appear at half size or double size. | 0–100; Auto |
| Bestiary Weights | none | Choose how often each enemy type appears. | per-item values |
| Health Ranges | none | Choose enemy health as a percentage of its usual value: 50% is half health, 100% is normal, 200% is double. | per-item values |
| Damage Ranges | none | Choose enemy damage as a percentage of its usual value: 50% is half damage, 100% is normal, 200% is double. | per-item values |
| Speed Ranges | none | Choose how fast enemies move and attack: 100% is normal speed. | per-item values |
| Size Ranges | none | Choose enemy size as a percentage: 50% is half size, 100% is normal, 200% is double. | per-item values |
| Special Enemy Limits | none | Choose how many special enemies can fight you at the same time. | per-item values |
| **Killsanity** | | | |
| Killsanity | Off | Earn an Archipelago reward after every Kills per Check enemy deaths. | Off; On |
| Kills per Check | 100 | Send one check at each multiple of this many enemy deaths. | 1–2000 |
| Kill Rewards | Auto | Choose how many kill rewards to add to your campaign. | 1–100; Auto |
| **Difficulty** | | | |
| Campaign Difficulty | Ultra Violence | Choose the game's combat difficulty for your campaign. | I'm Too Young To Die; Hurt Me Plenty; Ultra Violence; Nightmare |
| Logic Difficulty | Standard | Choose how much equipment Archipelago expects you to have before a fight. | Relaxed; Standard; Expert |
| Progressive Difficulty | On | Make randomized fights grow harder as you progress through the campaign. | Off; On |
| Enhanced Melee Damage | Off | Make normal punches deal more damage, giving you another way to fight at close range. | Off; On |
| Easier Secret Encounters | Off | Give yourself 50% more time to finish Secret Encounters. | Off; On |
| Mastery Requirements | Half | Choose how many times you need to repeat an action to finish a weapon mastery challenge. | Original; Half; Quarter |
| **Extras** | | | |
| Accessibility | Full | Choose how much of your world Archipelago makes reachable. | Full; Minimal |
| Progression Balancing | Normal | Help players in a multiworld find useful items at a similar pace. | 0–99; Disabled; Normal; Extreme |
| Death Link | Off | Share deaths with other players who enable Death Link. | Off; On |
| DeathLink Mode | Soft | Choose how deaths shared through Death Link affect you. | Soft; Hardcore |
| Reveal AP Locations on Automap | Off | Show Archipelago pickup locations on the DOOM Eternal Automap, making checks easier to find while exploring or replaying missions. | Off; On |
| **Starting items** | | | |
| Start Inventory | none | Start with extra copies of these items; use start_inventory_from_pool to take copies from the pool instead. | per-item values |
| Start Inventory from Pool | none | Start with the specified amount of these items and don't place them in the world. | per-item values |

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
  [Meathook](https://github.com/brongo/m3337ho0o0ok), the RPC foundation DoomEAP
  was first built on.
- PowerBall253 / brunoanc for
  [EternalResourceExtractor](https://github.com/brunoanc/EternalResourceExtractor)
  and `idFileDeCompressor`.
- FlavorfulGecko5 and the EntitySlayer contributors for
  [EntitySlayer](https://github.com/FlavorfulGecko5/EntitySlayer).
- leveste for [Eternal Mod Injector Shell](https://github.com/leveste/EternalBasher/).
- Kaiser_will and Friends for
  [KaiserCampaign](https://www.nexusmods.com/doometernal/mods/69), consulted as a
  research reference for pickup model rendering and map entity setup.
- The DOOM Modding community for the wiki material, and general knowledge
  that made the map patches possible.
- Meta (from the AP After Dark Discord server) for the Archipelago Logo model
  and overall very useful help.
- FridgeDuck (from the AP After Dark Discord server) for the Doom Archipelago
  logo used by the AP client (both old and new) and the repository preview.
- Doctor Pyro (from the AP After Dark Discord server) for overall immense help
  in testing, catching dozens of bugs in each version. Thanks a lot for that.
- [Lucas](https://www.behance.net/lucasmab) for the start screen and main
  menu logos, and launcher visuals feedback.

## 7. License

This project is distributed under the [MIT License](docs/LICENSE).

Sentinel Core distributions include their runtime manifest and license
notices. The launcher acquires compatible Core releases from `snowzzrra/Sentinel-Core` or 
uses a verified local distribution. External injectors are acquired from verified providers during setup
and are owned by their creators credited above.
