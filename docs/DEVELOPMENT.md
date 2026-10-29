# Content and builds

Edit each system in its owning repository:

- `Archipelago/worlds/doometernal`: world generation, options, items, regions and access rules.
- `DoomEternal-AP-Mod`: Python bridge, launcher, map compiler, native AP client and release packaging.
- `Sentinel-Core`: native game API, hooks, save integration and its independent runtime distribution.

Keep `DoomEternal-AP-Mod/`, `Archipelago/` and `Sentinel-Core/` as sibling directories.
`Archipelago/` is the Archipelago fork that contains `worlds/doometernal/`. The content
compiler defaults to that sibling; pass `--archipelago-source` for another location.
`scripts/build/windows_release.ps1` takes `-ArchipelagoSource` and `-CoreSource`.

## First contribution

Use Python 3.10 or newer for development and CI. From the repository root,
create a local environment and install the test dependencies:

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

Expect `5 passed`. These checks use temporary game folders. For the full fast
suite, run the same Python with `-m pytest tests -q`; skips depend on installed
tools and sibling checkouts. Qt tests skip without PySide6.

For launcher or Qt work, also install `requirements-launcher.txt` with the venv's
Python. The first test needs no native build. Building the Windows native client
needs x64 MSVC, the Windows SDK and a configured sibling `Sentinel-Core` checkout;
some native tests skip without a compiler or Core headers. Core builds and tests
belong in `Sentinel-Core`; see [Build a release](RELEASING.md) for packaging.

Format only changed files: `python -m ruff format path/to/changed.py` and
`clang-format -i native/path/to/changed.cpp` (include changed headers).
Hooks are optional: with pre-commit installed and the venv active, run `pre-commit install`.

## Content sources

`content/maps/<map>/` describes map identity, locations, runtime events, publishers,
assets and onboarding. `content/catalog/` holds shared authored settings and qualified
enemy templates. The compiler verifies player-supplied retail maps in `vanillamaps/`
and builds the mod from these inputs. Generated maps and files under `data/` are
outputs where the compiler provides a source; do not maintain another copy by hand.

Enemy planning uses `native_enemy_catalog.json`, generated from the native map
inventory and qualified species templates. The planner freezes body choices while
the game schedules encounters. The compiler changes referenced bodies and preserves
native event arguments, manager flow and save integration.

`killsanity_estimates.json` holds conservative map estimates, reviewed event counts,
access points and the separate finite progression guarantee. Event counts are not
a complete enemy census. The content compiler checks referenced events against the
native catalog and generates `campaign_kill_sources.json` for both Python packages.
`secret_encounter_timers.json` supplies timer baselines. Reviewed event sources
supply Secret Encounter access for mastery logic; compilation checks those names
against the physical location records.

## Shared code and packaged resources

The content compiler projects planners from `doom_eap/content` into the APWorld.
Each package contains its required JSON resources and runs without the other checkout.
Run `python -m tools.content.compile_content_catalog --source-only` after changing
those sources, then use the same command with `--check` to verify the projections.

APWorld `options.py` supplies option names and help. Run
`python -m tools.content.compile_options_schema` to update the launcher's option
schema; add `--check` to verify it. Keep configuration keys and protocol IDs stable
when editing player-facing text.

The room compiler reads raw source snapshots from `room_payloads.zip`, not a
developer's map directory. It validates source hashes, plans and archive membership
before preparing a room mod. Enemy resources come from the native donor-map banks.

## Runtime boundaries

The bridge connects to Archipelago and persists item/check progress per room and
player. The native AP client calls Core's local API. Core verifies the game build
and provides observations, save access and queued gameplay operations. A successful
queued call is not evidence that its gameplay effect has already occurred.

Keep room identity checks, atomic persistence, item receipt indices, reconnect
handling and save ownership protections when simplifying these paths.

## Item delivery

```text
AP server ─ReceivedItems─► bridge (CommonContext, asyncio)
                             │ checks the ordered receipt prefix
                             │ compiles the effect (contracts/foundation.py)
        ┌────────────────────┴───────────────────┐
        ▼                                        ▼
 disk spool base/ap_queue                  Sentinel probe (subprocess)
 .cmd → .processing → .attempt → deleted   weapon points, masteries,
                             └─► .failed   runes, special weapon
        ▼                                        ▼
 ap_client.exe (50 ms loop) ──────────────► Sentinel Core (game frame)
```

- `runtime/bridge_client.py` accepts receipts only when the received list extends the
  stored history for the session (seed, team, slot, generation fingerprint), and keeps
  the receipt cursor in `client_state.json` (`runtime/client_state_store.py`).
- Ordinary effects and notifications are spool files (`runtime/command_spool.py`).
  `native/client/command_queue.cpp` imports, runs and deletes them. An interrupted or
  ambiguous attempt moves to `.failed` and is never replayed automatically.
- Typed effects call Sentinel through the probe (`runtime/weapon_points.py`). Weapon
  points keep their own receipt ledger and rebuild it from AP history and the native
  earned total, never refunding spent points.
- Reconciliation (`runtime/materialization.py`, `runtime/rune_reconciliation.py` and
  typed reconciliation in the bridge) compares ownership with observed inventory and
  plans what is missing.
- Location checks stay on disk until the server acknowledges them.

The receipt cursor advances when a command reaches the spool, not when the game
confirms the effect. Publishing a command and advancing the cursor are separate
writes. Treat both as known limits when changing this path.

## Slot data

`fill_slot_data` in `Archipelago/worlds/doometernal/__init__.py` produces the room
data; the launcher normalizes it (`launcher/launcher_core.py`, `SeedManifest.from_room`)
and the runtime validates the campaign (`runtime/unified_campaign.py`). Enemy slot data
contains a seed, recipe revision, frozen campaign and fingerprint. Clients reconstruct
the recipes locally with the matching source catalog and verify the fingerprint.
Complete recipe plans are also accepted. Universal Tracker restores the room's campaign,
game options and enemy recipes before constructing regions and rules; its room data
must include `tracker_options`.
Slot data is versioned by `slot_schema` and revision fields; the generation fingerprint
excludes `enemy_*`, `progressive_difficulty` and `tracker_options`. Consumers validate
the frozen campaign rather than selecting a different campaign locally.

## Native protocol

Sentinel serves a local named pipe per game process, restricted to the logon session.
Messages carry a 16-byte little-endian header (magic, version, operation, payload
length, result) and are limited to 16 KiB (`Sentinel-Core/include/sentinel_inspection.h`,
`src/protocol.cpp`). Requests are admitted against process, context, session and feature
scope, run on the game frame, and keep their result for 30 seconds. After an uncertain
submit the client polls the same request identity instead of resubmitting
(`native/client/sentinel_command_client.cpp`).

Automap ABI 2 carries 576 check bits, covering location IDs through 7770575. The
launcher requires a Core/probe distribution with that ABI before gameplay setup.

Use [Build a release](RELEASING.md) for validation and packaging commands. Offline
harnesses cover protocol and simulated lifecycle behavior. Full gameplay and real
Linux/Proton checks require a separate test session with the supported game.

## Writing code

Data from outside the program — the AP server, files on disk, the named pipe to
Core, game memory — is checked once, where it comes in, and turned into a typed
value (usually a frozen dataclass or an enum). Code past that point can rely on it.
Checking the same fields again deeper in the code makes changes harder, because
every check has to be updated when the format changes.

Decide with codes and enums, not with display text. A button's label or an
exception message can change for a wording fix; that must not change behavior.

Catch the specific exception you expect, near the call that raises it. Catching
`Exception` is fine only at the edge of a worker thread or a game hook, where an
uncaught error would kill the thread or the game. In native code, game-memory
reads and writes stay inside SEH guards, every MinHook call's result is checked,
and a hook install that fails halfway removes the hooks it already created.

Give things the name of what they are (`MenuState`, `content_fingerprint`). Never
rename persisted keys or public IDs: saves and rooms from players depend on them.

A comment is worth writing when the code can't say it: a game quirk, a limit of
Proton, a reason an order matters. Leave out comments that restate the code.

Text shown to players says what went wrong and what to do next. When the fix isn't
simple, point to Help → Save Support Report; the technical details go in the report.

Tests go through the same entry points the program uses and check what a player
would notice: saves kept, rooms kept apart, items delivered once, spending,
recovery after a crash, progression, install and update. A test that checks a
private attribute, an exact sentence or the order of internal calls breaks on every
refactor without catching real bugs.

### Generated files

Most of the large JSON files in this repository are not written by hand. Edit the
source and run the generator; a hand edit is overwritten on the next run and makes
the provenance hashes disagree.

| Files | Source | Command |
|---|---|---|
| `doom_eap/content/native_enemy_catalog.json`, `vanilla_enemy_profiles.json`, `campaign_kill_sources.json`; `data/checked_location_visuals.json`, `campaign_stages.json`, `foundation_contracts.json`; the APWorld's `generated_content.py` and its copies of these files | `content/maps/`, `content/catalog/` | `python -m tools.content.compile_content_catalog` |
| `data/options_schema.json` | option classes in the APWorld | `python -m tools.content.compile_options_schema` |
| `content/catalog/native_enemy_assets.json` | the installed game (Windows) | `python tools/content/capture_native_enemy_assets.py` |
| `packaging/room_resources/<version>/` | compiled room resources | release tooling, see [Build a release](RELEASING.md) |

The content compiler prints the paths it changed. Commit the source change and the
regenerated files together.

### Function complexity

`python -m tools.validation.function_complexity` measures each Python function.
A function may have cognitive complexity up to 15 and cyclomatic complexity up to
10. Functions that were already above that are listed with their current numbers
in `tools/validation/complexity_exceptions_mod.txt` (and `..._apworld.txt` for the
APWorld, checked with `--apworld-root ../Archipelago/worlds/doometernal`). A listed
function may get simpler but not more complex. When you simplify one below the
limit, delete its line, or run with `--update`.

## Glossary

| Term | Meaning |
|---|---|
| Room | An Archipelago multiworld session; identified by seed, team and slot |
| Room mod | The mod ZIP compiled for one room and player |
| Receipt | One entry of the server's received-items list, identified by its index |
| Check | A location the player completed, sent to the server |
| Core | Sentinel Core, the native DLL loaded into the game |
| Lease | Identifier of the game context (campaign, map, load) a command was planned for |
| Spool | The on-disk queue of commands between the bridge and the native client |
