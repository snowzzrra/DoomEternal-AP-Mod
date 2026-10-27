# Content and builds

Edit each system in its owning repository:

- `Archipelago/worlds/doometernal`: world generation, options, items, regions and access rules.
- `DoomEternal-AP-Mod`: Python bridge, launcher, map compiler, native AP client and release packaging.
- `Sentinel-Core`: native game API, hooks, save integration and its independent runtime distribution.

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
