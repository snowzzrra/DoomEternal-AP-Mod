# DOOM Eternal Archipelago

> [!TIP]
> This is still a beta. Found a bug? Use **Report a Problem** in the launcher or [open a GitHub issue](https://github.com/snowzzrra/DoomEternal-AP-Mod/issues/new/choose).

[What gets randomized](#1-what-gets-randomized) · [Player options](#2-player-options) · [Installation](#3-installation) · [How it works](#4-how-it-works) · [Version scope](#5-version-scope) · [Credits](#6-credits) · [License](#7-license)

> [!NOTE]
> Want to buy me a coffee? No need! But do leave your feedback. It's greatly appreciated.

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
| Goal | Acquire the Unmaykr | Choose what you need to do to win. | Acquire the Unmaykr; Kill the Icon of Sin; Kill the Dark Lord; Complete the Full Saga; Complete N Master Levels |
| Goal Mission as Item | Off | Unlock your goal's boss mission by finding its Access item in Mission Access as Items. | Off; On |
| Full Saga Final Boss | Randomized | Choose the last boss for the Complete the Full Saga goal. | Randomized; Icon of Sin; Davoth |
| Additional Victory Requirements | none | Choose extra tasks to finish before you win. | any of 8 |
| Include Master Levels | Off | Include every qualified standard Master Level, each with its own Access item. | Off; On |
| Master Level Count | 1 | Choose how many distinct Master Levels to finish for Complete N Master Levels. | 1–6 |
| Arena Checks | DLC + Master Levels | Choose which levels give a check for each arena you clear. | All Levels; DLC + Master Levels; Only DLC Levels; Only Master Levels; No Levels |
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
| Useful Fortress Rewards | 75 | Choose how many Fortress battery room rewards hold something useful. | 0–100 |
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
| Kills per Check | 100 | Send one check at each multiple of this many enemy deaths. | 1–1000 |
| Kill Rewards | Auto | Choose how many kill rewards to add to your campaign. | 1–2000; Auto |
| **Difficulty** | | | |
| Campaign Difficulty | Ultra Violence | Choose the game's combat difficulty for your campaign. | I'm Too Young To Die; Hurt Me Plenty; Ultra Violence; Nightmare; Ultra-Nightmare |
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

### Changing a text

Every message the player sees, in the launcher or in the game, lives in
[`data/strings/`](data/strings/). There is one file per language:
[`en.json`](data/strings/en.json) has every message in English, and
[`pt-BR.json`](data/strings/pt-BR.json) has the ones translated to Brazilian Portuguese.
Each line is one message:

```json
"notifications.major_item_received": "{item} RECEIVED",
```

The part before the colon says where the message shows up; the part after is the text.
To change what a message says, edit the text and send a pull request.

- Words in braces, like `{item}` or `{player}`, are filled in by the game. Keep them
  exactly the same in every language.
- If a message is missing from a translation, the English one is shown instead.
- Keep the lines in alphabetical order.
- The English names and descriptions of player options come from the APWorld. To change
  one, change the option there; the check below tells you when the two don't match.
- Item, weapon, enemy and mission names aren't in these files. They show as they are in
  the game.

Before sending, update the generated files and check everything:

```sh
python -m tools.content.compile_player_strings
python -m tools.content.compile_player_strings --check
```

The check tells you if something is wrong, such as a misspelled `{word}` or a text too
long to fit on the game screen. Developers adding new messages: see
[Player text and languages](docs/DEVELOPMENT.md#player-text-and-languages).

### Adding a language

1. Create `data/strings/<code>.json`, for example `fr.json`, and copy in the lines you
   want to translate from `en.json`. You don't need all of them at once; the rest show
   in English.
2. Add your language to [`data/strings/languages.json`](data/strings/languages.json),
   for example `{"code": "fr", "name": "Français"}`.
3. Run the check above.
4. Open the launcher, click the flag button, pick your language and restart it.

A flag is optional: without one, the button shows the language code (`FR`). If you add
one, put the SVG in `assets/launcher/flags/` (for example `fr.svg`), add `"flag": "fr"`
to your language, and credit where it came from. Flags must come from a public source
with a license that allows reuse, like [flag-icons](https://github.com/lipis/flag-icons);
don't use AI-generated images.

The language button changes the launcher only. Text inside the game follows the
language set in DOOM Eternal, and the APWorld's own text stays in English.

## 5. Version scope

### 0.1.x — First version — DONE

- Create the initial route through Cultist Base.
- Add RPC recovery, DeathLink, and Windows/Linux compatibility coverage.

### 0.2.x — Campaign content expansion and systems foundation — DONE

- Add in more base content, such as Mastery and Mission Challenges, Secret Encounters and more.
- Establish a foundational pipeline to create the .entities files needed for the .decl part of modding.
- Create the first version of what this mod will later build upon.

### 0.3.x Alpha — Base campaign map expansion — DONE

- Include remaining maps from base campaign, up until Final Sin.
- Fix bugs during playtests in order to make a viable full AP run.
- First true version of the mod, and prepare for Beta.

### 0.4.0 Beta — Installation, logic, and option architecture — DONE

- A launcher to set up mod installation automatically with one click.
- More AP options, such as Starting Weapon and QoL features.
- Streamlined Help tab, complete with a setup check and support reports to better help development.

### 0.5.0 Beta — The Full Saga — DONE

- Base Campaign, The Ancient Gods Part One, and The Ancient Gods Part Two form
  one 19-mission Archipelago world with global inventory and completion flow.
- DLC-specific gear, mission logic, generation, runtime, and validation are in scope.

### 0.6.x Beta — Random Mission Order + Sentinel Core — DONE

- Develop Sentinel Core as an independent open-source native integration project for Archipelago.
- Replace Meathook with the more AP-focused Sentinel Core, complete with an automatic updater.
- Streamline all three campaigns into one, using a single menu and randomizing mission order in two different modes.
- Validate logic so every single campaign order is valid, considering combat logic and the player's expected loadout.

### 0.7.x Beta — Enemizer — DONE

- Randomize enemy placements and encounters in various different difficulty presets while respecting the player's expected loadout (CR).
- Add Killsanity, which gives a check every X demons killed (configurable).
- Add in some new checks in existing missions.

### 0.8.x–0.9.x — Minor features and polish — Current

- Better documentation for future maintenance.
- Clean up leftover code from previous versions, keeping everything as clean as possible.
- Rework what needs to be reworked, and smooth out any new bugs.
- Add more checks to DLC levels, keeping them on par with base campaign ones.
- Add in vanilla Master Levels, Ultra-Nightmare(lite) difficulty and Arena Checks for every level.

### 1.0 — Final Release

- As soon as stability is reached, release a final candidate.
- Later updates should be hotfixes only.

## 6. Credits

- The Archipelago project and contributors for the multiworld framework,
  protocol, server, and `CommonClient`.
- tastyfresh (from the AP After Dark Discord server) for the original
  large check list for project setup.
- Zwip Zwap Zapony (from the Doom 2016+ Modding Discord server) for direct
  technical guidance, map/runtime research and the [Windows Mod Injector](https://gamebanana.com/tools/7475).
- alby (from the Doom 2016+ Modding Discord server) for technical help, runtime
  investigation, and .decl modding guidance.
- KaitoKid (from the AP After Dark Discord server) for the very detailed logic guidance that
  basically created this mod's idea of combat logic.
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
- Negate The Stars for
  [Melee Damage Restored](https://www.nexusmods.com/doometernal/mods/37), the idea
  and the damage value behind the Enhanced Melee Damage option.
- The DOOM Modding community for the wiki material, and general knowledge
  that made the map patches possible.
- Meta (from the AP After Dark Discord server) for the Archipelago Logo 3D model
  and overall very useful help.
- FridgeDuck (from the AP After Dark Discord server) for the Doom Archipelago
  logo used by the AP client (both old and new) and the repository preview.
- [Lucas](https://www.behance.net/lucasmab) for the start screen and main
  menu logos, and launcher visuals feedback.
- Flag icons from [flag-icons](https://github.com/lipis/flag-icons) by Panayiotis Lipiridis (MIT).
- Doctor Pyro (from the AP After Dark Discord server) for the immense help
  in testing, always catching dozens of bugs in each version. Thanks a lot for that.
- All familiar faces that participated in the original Doom Eternal AP thread
  in the discord, constantly reporting bugs, offering suggestions and always there
  to hype up every single update, even bug fixes. These include NovaAndrewM, Oscat,
  Quartz, Zeforas, LuciusDivine and some other people already mentioned before. Lots
  of other people showed up too! And I thank everyone for that.
- And of course, thank you, stranger, for reading my ramblings and (hopefully) having fun
  with my mod. You matter a lot.

## 7. License

This project is distributed under the [MIT License](docs/LICENSE).

Sentinel Core distributions include their runtime manifest and license
notices. The launcher acquires compatible Core releases from `snowzzrra/Sentinel-Core` or 
uses a verified local distribution. External injectors are acquired from verified providers during setup
and are owned by their creators credited above.