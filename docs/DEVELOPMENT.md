# Developing DOOM Eternal Archipelago

The project lives in three repositories, kept side by side in one folder:

- **`Archipelago/`**: our Archipelago fork. The DOOM world is in
  `worlds/doometernal/`: options, items, locations, regions, logic, and the room
  data handed to the client.
- **`DoomEternal-AP-Mod/`** (this one): the launcher, the bridge that talks to the
  AP server, the native client, the map/room compiler and release packaging.
- **`Sentinel-Core/`**: the DLL loaded into DOOM Eternal. It hooks the game, reads
  its state, writes saves and runs our commands on the game thread.

The tools assume these three folders are siblings. Pass `--archipelago-source`
(compiler) or `-ArchipelagoSource` / `-CoreSource` (`scripts/build/windows_release.ps1`)
if yours aren't.

## Getting started

You need Python 3.10 or newer. From the repository root:

Windows (PowerShell):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ci.txt
.\.venv\Scripts\python.exe -m pytest tests/test_launcher_setup.py::test_room_replacement_failure_preserves_previous -q
```

Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-ci.txt
.venv/bin/python -m pytest tests/test_launcher_setup.py::test_room_replacement_failure_preserves_previous -q
```

If that passes, your environment works. It uses temporary game folders; nothing
touches a real install. `-m pytest tests -q` runs the fast suite; some tests skip
without a compiler, the sibling checkouts or PySide6. For launcher work also
install `requirements-launcher.txt`. The Windows native client needs x64 MSVC, the
Windows SDK and a configured `Sentinel-Core` checkout; Core itself is built and
tested in its own repository.

Format only what you changed: `python -m ruff format path/to/file.py` and
`clang-format -i native/path/to/file.cpp`. `pre-commit install` sets that up as a
hook if you want it.

## How the pieces fit

```text
 player YAML ──► APWorld generates the room ──► slot data (resolved, digested)
                                                     │
 player connects ──► launcher ──► builds the room mod from frozen resources
                         │        └─► EternalModInjector installs it
                         ▼
                      bridge ◄──── AP server (items received, checks sent)
                         │
                 ledger ─┴─ delivery coordinator ──► native transport ──► Sentinel Core
                 (what you own,      (what's missing,       (named pipe,       (runs it on
                  what happened)      when to send)          protocol v2)       the game frame)
```

A few decisions shape everything else. Knowing why they were made saves you from
undoing them by accident.

**The generator decides, the client checks.** The APWorld resolves the whole
campaign at generation time: mission order, enemy recipes, starting items, Kill
Rewards. The client never re-rolls or "fixes" any of it; it validates digests and
refuses a room that doesn't match. Two copies of that logic, one on each side,
would sooner or later disagree, and the disagreement would only show up mid-game.

**The ledger owns item delivery.** Every received item goes into a SQLite ledger
(`runtime/ledger*.py`) before anything happens in the game. The coordinator
(`runtime/delivery.py`) compares what you own with what the game shows
(`runtime/inventory.py`) and sends only what's missing. An effect counts as
delivered only when the game proves it: a currency counter moved by exactly the
right amount, a perk is owned, a key is in the right map. When we can't prove it,
the item stays "uncertain" and the player chooses **Try again** or **Mark as
received**. We never resend on our own, because a resend can duplicate an item
and a refund can take back something the player already used.

**Commands have identities.** Every command sent to Core carries an ID. If the
connection drops after sending, the client asks Core about that same ID instead
of sending it again. Core keeps recent results so that question has an answer.

**One computer spends per room.** The first profile that spends (Ammo Refill, for
example) owns spending for that room. Others still receive items. There's no
safe way to hand that over while the old computer might still be running.

**Saves are written in two generations.** Core never overwrites the last good
checkpoint. It writes the new one next to it, proves it with hashes, then switches.
After a crash it keeps the files and recovers the last proven generation, or
stops and says why.

**The injector isn't ours.** EternalModInjector is a third-party tool. The launcher
writes its settings file, runs it, reports what it did and uses the tools it ships
with; it never modifies them or takes over their job. The install transaction
(`launcher/installation.py`, `install_journal.py`) records and can undo only what
the launcher itself writes.

**Hooks install all or nothing.** In Core, each group of hooks installs through a
`HookSet`: if one hook in a required group fails, the ones already installed are
removed. A half-hooked game is worse than a game with the feature off.

## Where things are

| Area | Module | What it does |
|---|---|---|
| APWorld | `__init__.py`, `options.py`, `logic.py`, `planner.py` | World class, player options, access rules, campaign planning |
| APWorld | `slot_data.py`, `slot_data_contract.py` | Builds and validates the room data sent to the client |
| Content | `content/maps/`, `content/catalog/` | Authored map data: locations, events, enemies, onboarding |
| Content | `tools/content/`, `tools/maps/` | Compilers: catalogs, generated projections, room mods |
| Launcher | `launcher_controller.py`, `events.py`, `presenters/`, `ui/` | App logic, typed events, what each screen shows, Qt views |
| Launcher | `installation.py`, `install_journal.py`, `installation_adapters.py` | Install transaction and the Windows/Linux injector runners |
| Launcher | `campaign_resources.py`, `resource_bank.py` | Enemy resource banks and campaign resources after injection |
| Launcher | `launcher_session.py`, `checkpoint.py` | Room session, AP save, backup and checkpoint recovery states |
| Runtime | `bridge_client.py` | Connection to the AP server; receives items, sends checks |
| Runtime | `ledger.py`, `ledger_records.py`, `ledger_schema.py` | What was received and what happened to it, durable |
| Runtime | `inventory.py`, `inventory_observation.py` | What you own vs. what the game shows |
| Runtime | `delivery.py`, `delivery_runtime.py`, `delivery_plans.py` | Deciding what to send and recording the result |
| Runtime | `native_transport.py` | Persistent pipe to Core (through a relay under Proton) |
| Shared | `doom_eap/contracts/` | Typed data at boundaries: room data, Core distribution, evidence |
| Shared | `data/strings/`, `doom_eap/strings.py` | Every player-facing text |
| Core | `protocol/schema.json`, `tools/generate_protocol.py` | The wire protocol; both sides generate code from it |
| Core | `src/engine/hooks/`, `src/save_*`, `src/features/expanded/` | HookSet, save checkpoints, encounter and spawn features |

## Fixing a bug: an example

Here's a real one, to show how a fix usually goes.

**The symptom.** Rooms with Kill Rewards enabled failed when the launcher tried
to accept them: "placement set is incomplete". Rooms without Kill Rewards were fine.

**Find where the two sides disagree.** The launcher checks that it has a placement
(which item sits where) for every location the server says exists
(`_normalize_placements` in `launcher/launcher_core.py`). The APWorld includes Kill
Reward locations in the room's placement digest. The bridge asks the server for
placements in `runtime/location_setup.py`, and that's where the answer was:
`begin()` removed the Kill Reward IDs from the list it asked about. Two of the three
parties expected them; one left them out.

**Decide which side is wrong.** Kill Rewards are real AP locations with real items,
so the launcher and the APWorld were right. The bridge should ask for them like
any other location. Changing the launcher and the digest instead would have meant
changing a contract on two sides to accommodate one bug.

**Write the test first, through the real entry point.**
`test_kill_reward_placements_are_scouted_with_map_locations` in
`tests/test_protocol_setup.py` starts a `LocationSetup` with a normal location and
a Kill Reward, and checks that both are asked for and both must be answered before
setup completes. It failed before the fix and passes after. It doesn't care how
`begin()` is written inside; it checks what the player would notice.

**Make the small fix, then look around.** The fix was a few lines in `begin()`.
Then check who else uses those placements: the room compiler writes "location
sent" strings and binds notifications by location ID, and with Kill Rewards in the
list it gained entries for them, which is harmless. The review that followed found
one real consequence: receipt notifications for local items found at a Kill Reward
were being hidden as if a map notification had already shown them. That got its
own test and fix.

**Run the gates.** The tests for what you touched, the complexity gate, `ruff`, and
`--check` on any generator whose inputs you changed.

In short: find where two parts disagree, decide which one matches the contract,
prove the bug with a test that goes through the same door the player does, fix it
where it starts, and check who else reads what you changed.

## Writing code

Data from outside the program (the AP server, files on disk, the pipe to Core,
game memory) is checked once, where it comes in, and turned into a typed value,
usually a frozen dataclass or an enum. Code after that point can trust it. Checking
the same fields again deeper in makes every format change touch many places.

Decide with codes and enums, never with display text. A label or a message can
change for a wording fix; behavior must not change with it.

Catch the specific exception you expect, close to where it's raised. Catching
`Exception` is fine only at the edge of a worker thread or a game hook, where an
uncaught error would kill the thread or the game. In native code, game memory is
only touched inside SEH guards, every MinHook result is checked, and a failed
install removes what it already installed.

Name things for what they are (`MenuState`, `content_fingerprint`). Never rename
persisted keys or public IDs: players' saves and rooms depend on them.

Write a comment when the code can't say it: a game quirk, a Proton limit, why an
order matters. Skip comments that repeat the code.

Player-facing text says what went wrong and what to do next. When the fix isn't
simple, point to **Help → SAVE SUPPORT REPORT**; technical details go in the report.

Tests use the same entry points as the program and check what a player would
notice: saves kept, rooms kept apart, items delivered once, spending, recovery
after a crash, progression, install and update. A test that checks a private
attribute, an exact sentence or the order of internal calls breaks on every
refactor and doesn't catch real bugs.

### Player text and languages

Player text lives in `data/strings/`. `en.json` has every message; each other
language has its own file (`pt-BR.json`) with only the messages it translates, and a
missing one falls back to English. `languages.json` lists the languages, their display
names and optional flags, and which one is the default. Code reads the files through
`doom_eap/strings.py`. For contributors, the short version is in the README:
[Changing a text](../README.md#changing-a-text) and
[Adding a language](../README.md#adding-a-language).

The launcher saves the chosen language as `launcher_language` in `launcher.json`
and applies it on the next start. It doesn't change the game's language; in-game
tables are generated for `en`/`pt-BR` and follow the game's own setting.

Only text the player reads goes in the catalog. Support-report details, the technical
details box, logs, status codes, commands and file names stay in English in the code.
So do item, weapon, enemy and mission names: the editor shows them as the schema has
them.

Rules for entries:

- Keys are stable and meaningful; never reuse one for a different message.
- Placeholders are the names in braces in the English text; every translation uses the
  same ones. No conversions, attribute/index access or format specs: format numbers
  before passing them. Write `{{` and `}}` for literal braces.
- Plurals are complete messages under `.one` and `.other` (`.zero` only if needed).
- The same English text with the same meaning gets one key under `common.*`. The check
  rejects repeated English text unless the key is in its short list of exceptions.
- Player options use `options.<option>.name`, `options.<option>.description` and
  `options.<option>.choice.<choice>`. Their English must match `data/options_schema.json`,
  so the APWorld stays the source; the check reports any difference.
- AP item/location/player names, chat and DeathLink causes from other games are
  data, not entries. Keep their escaping where they're shown and never format them twice.

#### Adding a DeathLink message

When you die with DeathLink on, the launcher picks one message at random from the
`deathlink.causes.*` keys and sends it to everyone in the room. To add one, add a line
to `data/strings/en.json`:

```json
"deathlink.causes.ran_out_of_fuel": "{player} ran out of Chainsaw fuel.",
```

Keep `{player}`: it becomes the player's name. The key after `causes.` is any short name
not used yet. Nothing else needs to change; the new message joins the draw on the next
start. Don't translate these messages: other players in the room may not read your
language, so they are sent in English.

The generator writes the APWorld's `player_strings_generated.py`, Sentinel-Core's
`src/player_strings_generated.h` and the game string tables. The Core header gets the
English text. Game table texts must fit their buffers in UTF-8, counting the final NUL:
96-byte menu titles, 32/96-byte HUD fields, 64/80-byte counters, 160-byte controls.
The check enforces it.

`python -m tools.validation.player_literals` looks for English literals where the player
would see them: the launcher UI and presenters, the game text the compiler writes and
`PlayerError` messages. Use catalog keys there and codes for decisions; English in logs
and technical details is fine. The small allow-list covers layout syntax, technical
examples and texts that belong in the code. CI runs this check beside the function
complexity gate.

### Generated files

Many large files here are generated. Edit the source and run the generator; a hand
edit gets overwritten and breaks the provenance hashes. Commit the source change
and the regenerated files together.

| Files | Source | Command |
|---|---|---|
| `doom_eap/content/native_enemy_catalog.json`, `vanilla_enemy_profiles.json`, `campaign_kill_sources.json`; `data/checked_location_visuals.json`, `campaign_stages.json`, `foundation_contracts.json`; the APWorld's `generated_content.py` and its copies of these files | `content/maps/`, `content/catalog/` | `python -m tools.content.compile_content_catalog --source-only` |
| `data/options_schema.json` | option classes in the APWorld | `python -m tools.content.compile_options_schema` |
| APWorld `player_strings_generated.py`, Core `src/player_strings_generated.h`, game string tables | `data/strings/` | `python -m tools.content.compile_player_strings` |
| `doom_eap/runtime/sentinel_protocol_generated.py` | Core `protocol/schema.json` | `tools/generate_protocol.py` in Core, then the Mod's projection |
| `content/catalog/native_enemy_assets.json` | the installed game (Windows) | `python tools/content/capture_native_enemy_assets.py` |
| `packaging/room_resources/<version>/` | compiled room resources | see [Building a release](RELEASING.md) |

Add `--check` to any of the Python generators to verify without writing.

### Function complexity

`python -m tools.validation.function_complexity` measures every Python function:
cognitive complexity up to 15, cyclomatic up to 10. Generated files are skipped.
Functions that were already over the limit are listed with their numbers in
`tools/validation/complexity_exceptions_mod.txt` (and `..._apworld.txt`, checked
with `--apworld-root ../Archipelago/worlds/doometernal`). A listed function may get
simpler, never more complex. When you bring one under the limit, delete its line or
run with `--update`. Core has the same gate with `tools/complexity_exceptions.txt`.

## Glossary

| Term | Meaning |
|---|---|
| Room | One Archipelago multiworld session, identified by seed, team and slot |
| Room mod | The mod ZIP built for one room and one player |
| Receipt | One entry in the server's list of items you received, identified by its index |
| Check | A location you completed, sent to the server |
| Ledger | The local database of what you received and what happened to each item |
| Evidence | Proof read from the game that an effect really happened |
| Uncertain | An item sent to the game without proof it landed; the player decides what to do |
| Core | Sentinel Core, the DLL inside the game |
| Generation | One of the two save checkpoints Core keeps |
