# Doom Eternal Archipelago Mod

The official game mod, Python client, C++ memory hooking, and Inter-Process Communication (IPC) layer for the Doom Eternal Archipelago Randomizer.

> [!CAUTION]
> I personally work using Linux, so this may not work for everyone.

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
1. **The Map Mod:** A custom Python Map Generator that parses and modifies `.entities` files directly to inject AP dummy items and Trap spawners.
> [!WARNING]
> **Why not idStudio?** idStudio saves the ENTIRE map with modifications, resulting in gigabytes of data per level. This is unviable for an Archipelago multiworld distribution. All map modifications MUST be done via `.entities` patching.
2. **The Injector (`ap_client.exe`):** A minimal C++ application that attaches to the game using **Meathook**. It runs *inside* the Proton prefix/container.
3. **The Python Client (`bridge_client.py`):** The logic handler that acts as the Archipelago `CommonClient`. It receives items from the server and pushes commands to the Injector via IPC.

*(Note: The server-side generation logic resides in the separate Archipelago repository).*

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

### Command Entity Delegation (Thread-Safe Item Delivery)
* **The Problem:** Executing gameplay-altering console commands (`give`, `chrispy`, `spawn`) over the Meathook async RPC directly causes race conditions, leading to sporadic Access Violation crashes when the main game thread is occupied (e.g., Alt-Tabbing, playing animations).
* **The Solution:** The Python Map Generator automatically appends one `idTarget_Command` entity for EVERY item and trap in the randomized pool at the end of every `.entities` file.
* **The Execution:** Instead of sending `give weapon/player/heavy_cannon` over RPC, the Python client delegates the execution by sending `ai_ScriptCmdEnt ap_cmd_<ID> activate`. The entity then natively executes the command on the Main Game Thread, achieving 100% thread safety and zero crashes!

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
* **Goal options:** Defeat Icon of Sin, Defeat The Dark Lord (Requires DLC), Collect Unmaykr.
* **Slayer Gate Progression:** Use Slayer Gate Keys as a way to gate global progress or access to the Unmaykr.
* **Hard Mode:** Disable mid-level checkpoints (dying restarts the entire level).
* **Enemy Randomizer:** Shuffle enemy spawns within levels (planned to integrate with existing Enemy Randomizer mod).
* **Horde Mode:** Include Horde Mode checks and rewards.

---

## Limitations
* **Polling Delay:** The python client reading `condump` loop adds a slight delay to item reception.
* **No Memory Reading:** Currently, this injector only *sends* commands. It does not read memory to check the player's inventory, but thanks to V22 telemetry, memory reading and inventory polling are no longer necessary for checks.

---

## Roadmap

### MVP (Current W.I.P)
* Finalize items, regions, and rules in AP World.
* Implement Hell on Earth, Exultia, and Cultist Base map mods.
* Validate entire campaign logic.

### Alpha
* Full campaign playable.
* Stable item delivery and checks.
* Windows Support Guarantee
* Save/load support.

### Beta
* DLC support and Master Levels.
* More options and balance passes.

### 1.0
* Installer, Documentation, Public release, and AP community announcement.

---

## To-Do
- [x] **AP Dummy Design:** Implemented V22 (idTrigger Mutation) to strip vanilla items and emit clean `Activate` telemetry.
- [x] **Trap Entity Injection:** Python `.entities` Map Generator now successfully injects `idTarget_Command` trap entities into maps, so they can be triggered safely via RPC without crashing the engine.
- [x] **Python Client Refactor:** Refactored the Python client to stop using the `clear; listInventory; condump` loop, instead parsing telemetry safely.
- [x] **Race Condition Fix:** Moved the queue file wiping logic in `ap_client_exe.cpp` to before the `ExecuteConsoleCommand` execution to prevent dropped items.
- [ ] Upgrade the IPC from a text file to a local Socket or Named Pipe for instantaneous item delivery.
- [ ] Investigate `idTarget_Notification` or other native UI popups for AP item delivery (e.g., "Archipelago: Received Super Shotgun!").
- [ ] Package the injector cleanly into a single payload or auto-injector script.

## Post-1.0 / 2.0 Features
- [ ] **AP Dummy Design (Zapony's Update):** Update Map Generator to inject `idTarget_Command` or `idTarget_Print` entities instead of bloated triggers, so the game natively echoes `ARCHIPELAGO: CHECK_ID` without needing `g_debugTriggers 1`.
- [ ] **Starting Inventory:** Changes the starting weapon in E1M1.

## Credits & Acknowledgments
This project would not be possible without the incredible support and existing tools from the modding community:
* **tastyfresh** (from the AP After Dark Discord) for providing the comprehensive list of checks.
* **zwip zwap zapony** (from the Doom 2016+ Modding Discord) for their immense help, pinned messages and documentation, which provided immense help in understanding how to interface with the game.
* **alby** (from the Doom 2016+ Modding Discord) for basically making this possible with the g_debugTriggers suggestion and overall help.
* **chrispy** for creating [Meathook](https://github.com/brongo/meathook), the foundational tool that makes modding Doom Eternal's engine possible.