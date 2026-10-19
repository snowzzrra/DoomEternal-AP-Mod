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

Use [Build a release](RELEASING.md) for validation and packaging commands. Offline
harnesses cover protocol and simulated lifecycle behavior. Full gameplay and real
Linux/Proton checks require a separate test session with the supported game.
