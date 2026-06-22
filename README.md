# Doom Eternal Archipelago Mod

The official game mod, Python client, C++ memory hooking, and Inter-Process Communication (IPC) layer for the Doom Eternal Archipelago Randomizer.

> [!CAUTION]
> I personally work using Linux, so this may not work for everyone.

## Project Status
This project is currently a prototype moving toward a playable pre-alpha test build.

**Current target:** package the first three campaign levels as a controlled Archipelago test release for the AP After Dark Discord.

The goal for the first public test is not full campaign support yet. It is to prove that map checks, item delivery, DeathLink receive, and the APWorld can work reliably across multiple early-game levels.

## Vision & Project Overview
Bring full Archipelago support to DOOM Eternal using native `.entities` map modifications and Meathook RPC integration.
The goal is to create a stable, multiworld-friendly randomizer experience without relying on memory editing or offsets.

**Core philosophy:**
* Native map modifications for location checks and traps (via Python `.entities` patching)
* Meathook RPC for item delivery and telemetry
* No pymem or memory scanning required
* Campaign remains largely vanilla
* Progression is randomized while preserving gameplay

---

## Architecture & Decisions
Doom Eternal runs on idTech 7, which is notoriously difficult to hook into externally using high-level languages like Python. Furthermore, running a Randomizer on Linux (via Proton/Steam) means that Python scripts running on the host OS cannot easily access the memory space of the Windows game running inside the Wine container.

**The Mod Structure:**
This repository serves as the ultimate "Client Package" distributed to the player. It includes:
1. **The APWorld (`doometernal/`):** The Archipelago world definition containing items, locations, regions, options, rules, and generation logic.
2. **The Map Mod:** A custom Python Map Generator that parses and modifies dumped `.entities` files directly to inject AP dummy items and Trap spawners.
> [!WARNING]
> **Why not idStudio?** idStudio saves the ENTIRE map with modifications, resulting in gigabytes of data per level. This is unviable for an Archipelago multiworld distribution. All map modifications MUST be done via `.entities` patching.
3. **Per-Level Manifests:** Planned generated metadata that maps injected `AP_CHECK_*` entities back to APWorld location IDs.
4. **The Injector (`ap_client.exe`):** A minimal C++ application that attaches to the game using **Meathook**. It runs inside the game environment and executes queued commands.
5. **The Python Client (`bridge_client.py`):** The logic handler that acts as the Archipelago `CommonClient`. It receives items from the server, parses telemetry, and pushes commands to the Injector via IPC.

For pre-alpha, the release package is expected to include the APWorld, client, injector, generated manifests, and patched mod files for the supported levels.

---

## Design & Logic

### Map Modification Strategy (V22 Strategy: idTrigger Mutation)
Do NOT modify vanilla pickup inherits directly via text editors, to avoid crashes and savegame corruption.
Instead, using our custom Python Map Generator:
* Hunt for progression items (Toys, Codex, Modbots, Weapons).
* Smash their original classes (e.g., `idProp2`, `idInteractable_WeaponModBot`) and transform them into `idTrigger`.
* Strip all inventory rewards and inject `art/pickups/question_mark_a.lwo` to standardize the AP aesthetic.
* Inject an `idTarget_Count` relay that listens to the trigger.
* **NEW (Zapony's Idea):** Alternatively, we can spawn an `idTarget_Print` or `idTarget_Command` directly in the `.entities` file. When the pickup is touched, it natively triggers `edit > commandText = "echo ^2ARCHIPELAGO: Collected Zombie toy!"`.
* The Python Client detects this log line and sends the check to the AP Server, entirely bypassing the inventory system.

Each level must be dumped and patched separately. The generator is being moved toward a multi-level workflow where each patched `.entities` file also produces a manifest describing the AP checks it contains.

### Command Entity Delegation (Thread-Safe Item Delivery)
* **The Problem:** Executing gameplay-altering console commands (`give`, `chrispy`, `spawn`) over the Meathook async RPC directly causes race conditions, leading to sporadic Access Violation crashes when the main game thread is occupied (e.g., Alt-Tabbing, playing animations).
* **The Solution:** The Python Map Generator automatically appends one `idTarget_Command` entity for EVERY item and trap in the randomized pool at the end of every `.entities` file.
* **The Execution:** Instead of sending `give weapon/player/heavy_cannon` over RPC, the Python client delegates the execution by sending `ai_ScriptCmdEnt ap_cmd_<ID> activate`. The entity then natively executes the command on the Main Game Thread, achieving 100% thread safety and zero crashes!

The next command delivery improvement is a safer queue/state gate. Commands received during loading, menus, cutscenes, or checkpoint transitions should wait until the game appears ready before being executed.

### DeathLink
* **Receive:** Implemented. Incoming DeathLinks can be turned into a queued in-game kill command.
* **Send:** Planned for Alpha. Sending DeathLinks requires reliable death detection from game telemetry, logs, or checkpoint/death-state investigation.

### Enemy Randomizer
Enemy randomization is planned for post-1.0. Since the map generator already mutates `.entities`, the long-term direction is to optionally replace enemy entities by tier-based presets rather than relying on external memory editing.

### Progression Logic
* **Dash:** Unfortunately, mandatory in Exultia. There was an idea of placing jump pads in every place dash was needed, but that isn't feasible rn
* **Chainsaw:** Chainsaw Sanity option available. Start with chainsaw (recommended) or randomize it.
* **Non-Progression:** Everything else.

---

## Item & Location Pools

**Location Pool (300-350 checks):**
Completion, Toys, Codex Pages, Weapon Mods, Sentinel Batteries, Sentinel Crystals, Secret Encounters, Slayer Keys, Empyrean Keys, Extra Lives, Cheat Codes, Albums, Automaps.

**Item Pool:**
* **Progression:** Weapons (Shotgun, Heavy Cannon, Plasma Rifle, Rocket Launcher, Ballista, Chaingun, BFG, Crucible), Abilities (Dash, Meathook, Runes, Weapon Mods), Resources.
* **Filler:** Extra Lives, Codex Pages, Toys, Albums, Cheat Codes, Automaps.
* **Traps & Boons:** Traps now work natively! Using the Command Entity Delegation architecture, the game can spawn any demon safely right where the player is looking using the `chrispy` command triggered via an `idTarget_Command` map entity. Good junk like Soulspheres, Extra Lives, and resources work perfectly as well.

---

## Suggested AP Options
* Include Master Levels / DLC (TAG1 & TAG2)
* Randomize Weapon Mods / Runes / Suit Upgrades / Sentinel Crystals / Slayer Gates
* Include Codex / Albums / Cheat Codes / Extra Lives
* Chainsaw Sanity
* Starting Dash / Starting Chainsaw
* DeathLink
* **Goal options:** Defeat Icon of Sin, Defeat The Dark Lord (Requires DLC), Collect Unmaykr.
* **Slayer Gate Progression:** Use Slayer Gate Keys as a way to gate global progress or access to the Unmaykr.
* **Hard Mode:** Disable mid-level checkpoints (dying restarts the entire level).
* **Horde Mode:** Include Horde Mode checks and rewards.

---

## Limitations
* **Prototype Scope:** The current working target is a three-level pre-alpha, not the full campaign.
* **Polling Delay:** The python client reading `condump` loop adds a slight delay to check detection.
* **Command Timing:** Item delivery is safe through map command entities, but a stronger queue/state gate is planned for loading/menu/cutscene cases.
* **No Memory Reading:** Currently, this injector only *sends* commands. It does not read memory to check the player's inventory, but thanks to V22 telemetry, memory reading and inventory polling are no longer necessary for checks.
* **Platform Support:** Windows testing is required before a public playable test build. Linux/Proton remains experimental until retested.

---

## Roadmap

### Pre-Alpha / Playable Test Build
* Package Hell on Earth, Exultia, and Cultist Base as the first controlled test release.
* Add per-level manifests and make the bridge load checks from those manifests.
* Improve command queuing so received items wait for safe in-game states when necessary.
* Test the package on Windows and collect structured bug reports from Discord testers.

### Alpha
* Full campaign playable.
* Stable item delivery and checks.
* Windows Support Guarantee
* Save/load support.
* DeathLink send investigation and implementation.

### Beta
* DLC support and Master Levels.
* More options and balance passes.

### 1.0
* Installer, Documentation, Public release, and AP community announcement.

---

## To-Do
- [x] Implement Hell on Earth prototype end-to-end.
- [x] Implement safe item delivery through `idTarget_Command`.
- [x] Implement DeathLink receive.
- [ ] Map and package Exultia.
- [ ] Map and package Cultist Base.
- [ ] Add per-level manifests for AP checks.
- [ ] Make the bridge load manifests instead of hardcoded checks.
- [ ] Add safer command queue/state gate for loading/menu/cutscene situations.
- [ ] Test full three-level pre-alpha on Windows.
- [ ] Prepare Discord test package and bug report instructions.

## Post-1.0 / 2.0 Features
- [ ] **AP Dummy Design (Zapony's Update):** Update Map Generator to inject `idTarget_Command` or `idTarget_Print` entities instead of bloated triggers, so the game natively echoes `ARCHIPELAGO: CHECK_ID` without needing `g_debugTriggers 1`.
- [ ] **Starting Inventory:** Changes the starting weapon in E1M1.
- [ ] **Enemy Randomizer:** Add optional `.entities` enemy mutation presets after the core randomizer is stable.
- [ ] **Native UI Notifications:** Investigate `idTarget_Notification` or other native UI popups for AP item delivery.

## Credits & Acknowledgments
This project would not be possible without the incredible support and existing tools from the modding community:
* **tastyfresh** (from the AP After Dark Discord) for providing the comprehensive list of checks.
* **zwip zwap zapony** (from the Doom 2016+ Modding Discord) for their immense help, pinned messages and documentation, which provided immense help in understanding how to interface with the game.
* **alby** (from the Doom 2016+ Modding Discord) for basically making this possible with the g_debugTriggers suggestion and overall help.
* **chrispy** for creating [Meathook](https://github.com/brongo/meathook), the foundational tool that makes modding Doom Eternal's engine possible.
