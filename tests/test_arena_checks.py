import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import pytest

from doom_eap.content.arena_checks import arena_check_name, load_arena_checks
from tools.maps.ap_map_generator import extract_target_names, find_entity_block_bounds
from tools.maps.arena_checks import compile_arena_checks
from tools.maps.encounter_catalog import entity_records, list_items


def _assert_arena_relay(code, generated, relay, relay_record):
    assert relay_record["edit"]["count"] == 1
    assert relay_record["edit"]["reuseable"] is False
    assert extract_target_names(relay) == [f"ap_notify_location_{code}", f"ap_event_{code}"]
    assert generated.count(f"condump ap_event_{code}.txt;") == 1


def _assert_arena_event_chain(check, inserted, new_events, old_events):
    assert inserted["eventCall"]["args"]["item[0]"]["entity"] == check
    assert old_events == new_events


@pytest.mark.integration
@pytest.mark.parametrize("map_key,entry", load_arena_checks().items())
def test_arena_completion_preserves_combat_and_emits_one_standard_chain(map_key, entry, frozen_map):
    raw = frozen_map(entry.get("map_key", map_key)).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["source_sha256"]
    source = raw.decode("utf-8")
    generated, manifest = compile_arena_checks(source, entry["arenas"], {})
    assert compile_arena_checks(generated, entry["arenas"], {}) == (generated, manifest)
    for arena in entry["arenas"]:
        check = arena_check_name(arena)
        code = arena["location_id"]
        assert manifest[check] == code
        relay = generated[slice(*find_entity_block_bounds(generated, check))]
        relay_record, = entity_records(relay, include_names={check})
        _assert_arena_relay(code, generated, relay, relay_record)
        suppress = generated[slice(*find_entity_block_bounds(generated, f"ap_suppress_arena_{code}"))]
        assert extract_target_names(suppress) == [check]
        for completion in arena["completion"]:
            owner = completion.get("manager", completion.get("target"))
            before = source[slice(*find_entity_block_bounds(source, owner))]
            after = generated[slice(*find_entity_block_bounds(generated, owner))]
            if "target" in completion:
                assert extract_target_names(after) == [check, *extract_target_names(before)]
                continue
            old, = entity_records(before, include_names={owner})
            new, = entity_records(after, include_names={owner})
            script = f"item[{completion['script']}]"
            old_events = list_items(old["edit"]["encounterComponent"]["entityEvents"][script]["events"])
            new_events = list_items(new["edit"]["encounterComponent"]["entityEvents"][script]["events"])
            inserted = new_events.pop(completion["source_event_index"])
            _assert_arena_event_chain(check, inserted, new_events, old_events)


def test_confirmed_arena_is_suppressed_after_checkpoint_reload_and_reentry():
    from doom_eap.runtime.bridge_checks import CheckedVisuals

    code = 7773001
    runtime_map = "game/sp/e1m1_intro/e1m1_intro"
    entry = {"location_id": code, "classification": "event_only",
             "arena_suppression_entity": f"ap_suppress_arena_{code}"}
    repair = CheckedVisuals({"e1m1_intro": runtime_map}, {"e1m1_intro": {code: entry}}, "session", Mock())
    send = AsyncMock(return_value=True)
    identity = SimpleNamespace(current_map=runtime_map, cached_marker={})

    async def exercise():
        for epoch in ("1:1", "2:2"):
            identity.cached_marker = {"gameplay_epoch": epoch, "map_key": "e1m1_intro", "runtime_map": runtime_map}
            arguments = dict(map_identity=identity, room_identity="room-a", state_key="room-a",
                             checked_ready=True, runtime_ready=True, rpc_ready=True, send=send)
            assert not await repair.reconcile("level_ready", checked=set(), **arguments)
            assert await repair.reconcile("connected", checked={code}, **arguments)
            assert not await repair.reconcile("tick", checked={code}, **arguments)
            assert send.call_args.kwargs["materialization_lease"] == epoch
        assert send.await_count == 2
        assert send.call_args.args == (f"ai_ScriptCmdEnt ap_suppress_arena_{code} activate",)

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("active", [False, True])
def test_room_compiles_selected_arena_with_enemy_randomizer_disabled(tmp_path, monkeypatch, active, frozen_map):
    from doom_eap.content.native_enemy_plan import native_recipe_fingerprint
    from doom_eap.launcher.launcher_core import RoomCompiler, SeedManifest

    key = "e5m4_boss"
    source = frozen_map(key).read_bytes()
    target = "e5m4_boss_patch1/maps/game/dlc2/e5m4_boss/e5m4_boss.entities"
    compiler = RoomCompiler.__new__(RoomCompiler)
    compiler.payload_resource = tmp_path / "sources.zip"
    with ZipFile(compiler.payload_resource, "w") as archive:
        archive.writestr("sources/e5m4_boss.entities", source)
    compiler.payload_manifest = {
        "schema_version": 2, "model": "dependent_map_payloads",
        "physical_option_keys": ["randomize_chainsaw", "randomize_dash", "randomize_first_battery"],
        "base_members": [target], "context_targets": {"test": target},
        "maps": {key: {"target_member": target, "option_keys": [], "state_policy": "off_only",
                       "states": [{"options": {}, "source": "base", "member": None, "sha256": "0" * 64}]}},
        "source_snapshots": {key: {"member": "sources/e5m4_boss.entities", "size": len(source),
                                    "sha256": hashlib.sha256(source).hexdigest()}},
    }
    plan = {"revision": 20, "maps": {key: {"runtime_map": "game/dlc2/e5m4_boss/e5m4_boss", "recipes": [],
                                         "source_sha256": hashlib.sha256(source).hexdigest()}}}
    plan["fingerprint"] = native_recipe_fingerprint(plan)
    monkeypatch.setattr(compiler, "_resolved_enemy_plan", lambda manifest: plan)
    monkeypatch.setattr(compiler, "_compile_options", lambda options: {})
    monkeypatch.setattr(compiler, "_compress_entities_text", lambda text: text.encode())
    manifest = SeedManifest.create(seed_name="Arena", team=0, slot=1, options={},
                                   active_location_ids=[7774901] if active else [], static_precompile=True)
    assembled = {target: b"base content"}
    compiler._apply_enemy_plan(assembled, manifest)
    assert (b"condump ap_event_7774901.txt;" in assembled[target]) == active
    if not active:
        assert assembled[target] == b"base content"
