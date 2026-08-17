# DOOM Eternal Archipelago

> [!CAUTION]
> This project is a beta, not a finished 1.0 release. Windows and Linux/Proton
> are supported.


## 1. What this mod is

DOOM Eternal Archipelago turns the base campaign into an Archipelago
progression randomizer. Weapons, weapon mods, runes, suit upgrades, equipment,
resources, and other useful items are distributed across hundreds of objectives
throughout the campaign. A secret, challenge, collectible, combat encounter, or
mission milestone can provide the item that opens your next route.

[Archipelago](https://archipelago.gg/) connects randomizers through one shared
multiworld. Every player keeps playing their own game, while items found across
the connected worlds travel to their assigned owners. A DOOM Eternal check may
send an item to someone playing another supported game, and their next check may
send you a weapon, mod, rune, or upgrade. The server routes each item
automatically.

This integration is Archipelago-first. A multiworld can include any mix of
supported games and players. A one-player room works equally well as a solo
DOOM Eternal randomizer: every item stays inside that campaign, while the same
generation rules, progression logic, and runtime systems remain active.

Current campaign coverage includes:

- all 13 base-campaign missions;
- 100 connected regions;
- 307 physical checks inside maps;
- 62 runtime and meta checks;
- 369 locations in total;
- 117 item types;
- randomized starting weapons and starting inventory;
- configurable Chainsaw, Dash, first Sentinel Battery, Start With Automap, traps, and
  DeathLink;
- room-specific map packages for Windows and Linux/Proton.

The launcher handles room connection, YAML creation, room package preparation,
installation guidance, session activity, logs, diagnostics, repair, and explicit
Steam launch. Each generated room carries its own options and identity, so the
game, client, save, and installed map package operate as one consistent session.
From Home, use Join for a room or Resume for saved room identity; Session shows
connected-room state and setup actions, while Help provides setup checks and
troubleshooting actions. Create YAML is for generating a future room.

## 2. Installation

See the [Windows and Linux/Proton installation guide](docs/INSTALL.md).

## 3. How it works

### Room generation

Archipelago reads each player's YAML and creates a room containing options,
access rules, locations, and item placements. The DOOM Eternal APWorld models
100 regions and evaluates which objectives are reachable with the player's
current inventory. Fill logic accounts for weapon progression, Fortress of Doom
Battery costs, challenge requirements, the mission weapon curve, starting
inventory, and selected physical options.

Generation produces `slot_data`, the room-authoritative description of the
player's options and identity. The launcher uses it to prepare the exact map
package needed by that slot. Starting inventory is also compiled into the
mission `DevInvLoadout`, making starting ownership available as the campaign
loads.

### Authoring campaign content

DOOM Eternal missions store gameplay objects in `.entities` data. These objects
include pickups, doors, triggers, relays, encounters, objectives, checkpoints,
Automap stations, mission-completion targets, and the links that connect them.
The mod works with those native objects and their mission relationships.

Every supported map has an authorial content package under:

```text
content/maps/<map_key>/
├── descriptor.json
├── locations.json
├── runtime.json
├── publishers.json
├── assets.json
└── onboarding.json
```

Together, these files describe source-map identity, public location IDs,
physical owners, runtime checks, event publishers, required assets, target
chains, transforms, and mission-specific contracts. This is the readable source
used by the map compiler. Catalogs, registries, manifests, and generated map
files are projections of that content.

Each physical location identifies a native mission entity and its functional
context. The compiler preserves the pieces that make the objective behave like
part of the mission: visual presentation, collision, interaction volume,
Automap presentation, doors, encounter managers, objective relays, checkpoint
continuity, and other mission scripting. It then gives that objective an
Archipelago publisher with the location's public ID.

Different check families retain their natural campaign behavior:

- collectible pickups remain physical objects in their original spaces;
- secrets retain their doors, markers, and exploration flow;
- combat encounters publish through their native completion path;
- mission challenges and weapon masteries use persistent game state;
- Mission Complete follows the native mission transition;
- Fortress rooms keep Battery access requirements and room interactions;
- campaign objectives keep the relays and checkpoints that drive mission flow.

### Map compilation

The map generator reads the supported retail source map, verifies its identity,
and applies deterministic transformations from the authorial content package.
It creates location publishers, item-command targets, notification entities,
physical-option writers, Mission Complete support, Fast Travel support, and
room-specific starting state.

Generated maps receive semantic validation before packaging. A baseline records
the accepted structure of each mission: entity counts, public checks, runtime
checks, regions, important target chains, and content hashes. This makes a map
edit reviewable at the level of gameplay structure rather than only as a large
text diff.

Physical options are projected while the room package is prepared.
**Start With
Automap** defaults to OFF. When enabled, AP precollects **Reveal Automap
Progression Items**.
### Location checks

A completed physical objective activates its `AP_CHECK_*` publisher. The
publisher writes a durable event named with the public location ID. The Python
bridge reads that event and submits a standard Archipelago `LocationChecks`
message. The event remains durable while delivery is in progress, allowing the
check flow to survive reconnects and short network interruptions.

```text
native mission objective
  -> AP_CHECK_* publisher
  -> ap_event_<location_id>.txt
  -> bridge_client.py
  -> Archipelago LocationChecks
  -> server acknowledgement
```

Runtime locations use the same public check protocol with another native source
of truth. Mission challenges, weapon masteries, mission completion, campaign
goal, and other persistent states are reconciled from native events or
structured save data. Each observer is tied to the current room, slot, save,
map, and load epoch.

### Item delivery

The APWorld item table and game-side command table share the same 117 public
item IDs. When the server sends an item, the bridge resolves its command and
activates a map-owned `ap_rpc_v3_*` entity:

```text
ai_ScriptCmdEnt ap_rpc_v3_<item_id> activate
```

These entities use native game commands for weapons, weapon mods, perks, runes,
equipment, currencies, resources, traps, and other effects. Replay policy is
defined per item. Permanent ownership is reconciled after reconnect, while
consumable and one-shot effects follow their delivery receipt state.

The external `ap_client.exe` carries commands between the bridge and Meathook's
in-game RPC server. Command dispatch is gated by supported executable identity,
native memory safety, active map, gameplay state, room identity, and load epoch.
This keeps delivery synchronized with the mission that owns the command
entities.

### Native map systems

The implementation uses native DOOM Eternal systems wherever the campaign
already has a suitable owner or writer:

- replay Fast Travel uses `idTarget_FastTravelUnlock`;
- Mission Complete uses mission transition publishers;
- equipment, perks, weapons, mods, runes, and currencies use their supported
  command or target forms;
- challenge and mastery state comes from the game's persistent save model;
- map checks use durable AP publishers attached to the objective's native flow.

The 79 Fast Travel anchors remain destinations owned by the native system. One
AP-owned unlock target per mission enables that system during eligible replays.
Eligibility is captured at the beginning of a load epoch from the mission's
checked Mission Complete state, then held stable for that load.

### Save, reconnect, and session identity

The bridge maintains a room-bound snapshot containing checked locations,
received items, delivery receipts, observer fingerprints, current map state,
and load epochs. Archipelago is authoritative for received items and completed
locations. Reconnect compares local state with the server and resumes pending
work from that shared history.

Room identity also protects generated content. The launcher, installed package,
bridge, APWorld, slot data, and save association are checked together. Setup check
reports identity or installation drift. Repair/Fix presents a concrete plan, backs
up owned content, applies confirmed actions, and validates installed files.

### Campaign systems

- **Starting Weapon** selects one configured weapon or samples one eligible
  weapon when Random is chosen. Combat Shotgun participates as an opt-in choice.
- **Sentinel Batteries** form an economy of 28 units: two individual Batteries
  and thirteen bundles worth two each. The thirteen Fortress consumers each
  require a balance of two.
- **Start With Automap** defaults to OFF. ON precollects exactly one
  **Reveal Automap Progression Items**. AP progression-item markers become visible
  through each mission's transformed native Automap station.
- **Trap Percentage** replaces that percentage of filler padding with traps. It
  does not replace progression items or other pool items. **Enabled Traps** selects
  trap types eligible for those filler slots; with no enabled types, no traps are
  placed.
- **Fast Travel** activates the mission's native Fast Travel unlock during a
  replay whose Mission Complete state was already checked at load start.
- **DeathLink Soft** dispatches each received death event once, then normal
  respawn/checkpoint flow continues. **Hardcore** keeps the same event pending until death is detected.
- **Marker cleanup** reconciles server-confirmed locations with their map and
  Automap presentation across reloads and revisits.

### Repository and runtime architecture

Implementation is split across two repositories:

- `Archipelago/worlds/doometernal/` owns APWorld options, items, locations,
  regions, logic, fill, slot data, and generation output;
- this repository owns authorial game content, map compilation, runtime
  observers, item delivery, native client, launcher, validation, and packaging.

Source modules are organized under `doom_eap/`: `runtime/` owns bridge and game
observers, `launcher/` owns launcher commands and UI, `content/` owns catalog
and option definitions, and `contracts/` owns shared content contracts.

### Developer build modes

`scripts/pipeline.sh playtest` creates a candidate through cheap preflight,
digest-backed materialization, package assembly and structural artifact smoke.
`scripts/pipeline.sh audit`/`full` run explicit semantic map validation.
`scripts/pipeline.sh release --build` is formal package validation. `affected`
is diagnostic only; it does not determine build correctness. Room payload states
are finite transitional 0.4.x artifacts. Future seed compilation uses
project-controlled `vanillamaps` source, never player installation sources;
Enemizer requires that seed-time compiler and cannot extend payload enumeration.

The active runtime has three cooperating layers:

1. **Generated mission content** publishes checks and exposes native command
   entities.
2. **`doom_eap.runtime.bridge_client`** speaks the Archipelago protocol, owns
   durable session state, and coordinates observers and item delivery.
3. **`ap_client.exe` and Meathook** provide external RPC and native telemetry for
   the running game.

The launcher surrounds those layers. It reads the room schema, creates player
YAML, discovers supported installations, prepares the room package, supervises
the bridge, opens DOOM Eternal through Steam, and provides Help, setup check,
Repair/Fix, and a sanitized support report.

## 4. Roadmap

### 0.1.1 — Runtime stabilization — DONE

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

### 0.4.0 Beta — Installation, logic, and option architecture — CURRENT

- Room-specific Windows and Linux/Proton launcher workflow.
- Starting Weapon Choice, Random, and configurable starting inventory.
- Hard, Soft, and Combat logic profiles.
- Physical Chainsaw, Dash, and first Sentinel Battery options.
- Start With Automap, mission replay Fast Travel and Soft/Hardcore DeathLink.
- Help, setup check, Repair/Fix, support reports, resync, and room identity checks.

### 0.5.x Beta — The Ancient Gods

- Add The Ancient Gods Part One and Part Two campaigns, including missions,
  regions, locations, items, progression rules, and completion flow.
- Extend generation, runtime, and validation for DLC-specific mechanics.

### 0.6.x Beta — New AP-Focused DLL + Random Mission Order

- Create an alternative to meathook to better fit Archipelago needs.
- Remove all meathook-dependent architecture and work on a new, proprietary, DLL.
- Turn mission access into Archipelago progression items.
- Generate a valid starting mission and reachable randomized mission order.

### 0.7.x Beta — Enemizer

- Randomize enemy placements and encounter compositions while respecting arena
  structure, progression-critical encounters, and runtime safety.

### 0.8.x–0.9.x — Content freeze and polish

- Stabilize public IDs, contracts, and data formats for 1.0.
- Focus on balance, installation, compatibility, save/reconnect behavior,
  discoverability, and community testing.
- Complete documentation, diagnostics, support tooling, and release-candidate
  validation.

### 1.0

- Stable public release of the supported campaigns and systems.

### Post-1.0 / 2.0

- Horde Mode and Master Levels.
- Hard Mode and checkpoint removal.

## 5. Credits

- The Archipelago project and contributors for the multiworld framework,
  protocol, server, and `CommonClient`.
- tastyfresh (from the Doom 2016+ Modding Discord server) for the original 
  large check list for project setup.
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

## 6. License

This project is distributed under the [MIT License](docs/LICENSE).
