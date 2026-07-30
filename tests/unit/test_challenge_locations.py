import copy
import json
import tempfile
import unittest
from pathlib import Path

from challenge_registry import all_location_entries, canonical_map_name, load_challenge_registry
from tools.decls.mastery_decl_builder import (
    STICKY_DECLS,
    build_mastery_overrides,
)
from tools.decls.mission_challenge_decl_builder import (
    AGGREGATE_LIST_PATH,
    NO_REWARD_CONTAINER,
    NO_REWARD_CONTAINER_DECL,
    NO_REWARD_CONTAINER_PATH,
    REWARD_FIELD,
    _challenge_paths,
    _level_blocks,
    _suppress_aggregate_reward,
    build_mission_challenge_overrides,
)
from tools.decls.rune_decl_builder import GATE_LINE, RUNE_OWNER, build_rune_override
from tools.validation.validate_challenge_overrides import validate_overrides_from_mod_root

ROOT = Path(__file__).resolve().parents[2]


def _package_runtime_locations(category: str) -> list[dict]:
    entries = []
    for path in sorted((ROOT / "content" / "maps").glob("*/runtime.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        entries.extend(
            entry for entry in document["locations"]
            if entry["category"] == category
        )
    return entries


class NativeChallengeContracts(unittest.TestCase):
    def test_urdak_toy_challenge_uses_one_of_three_physical_checks(self):
        registry = load_challenge_registry()
        entry = next(
            item for item in registry["mission_challenges"]
            if item["location_id"] == 7770407
        )
        self.assertEqual(
            {
                "name": entry["name"],
                "description": entry["description"],
                "strategy": entry["strategy"],
                "kind": entry["signal"]["kind"],
                "physical_location_ids": entry["signal"]["physical_location_ids"],
                "required_count": entry["signal"]["required_count"],
            },
            {
                "name": "Urdak - Mission Challenge - Accessories Not Included",
                "description": "Acquire 1 Collectible Toy",
                "strategy": "physical_event_equivalent",
                "kind": "physical_event_equivalent",
                "physical_location_ids": [7770392, 7770394, 7770400],
                "required_count": 1,
            },
        )
        names = {
            item["location_id"]: (item["name"], item["description"])
            for item in registry["mission_challenges"]
            if item["location_id"] in {7770408, 7770409}
        }
        self.assertEqual(names, {
            7770408: (
                "Urdak - Mission Challenge - Inflight Devastation",
                "Kill 12 Demons while in mid-air",
            ),
            7770409: (
                "Urdak - Mission Challenge - Angel of Death",
                "Kill 5 Maykr Drones with Precision Bolt headshots",
            ),
        })

    def test_registry_contains_only_proven_runtime_locations(self):
        registry = load_challenge_registry()
        entries = all_location_entries(registry)
        legacy = load_challenge_registry(
            ROOT / "data" / "challenge_location_registry.json"
        )
        expected_ids = {
            entry["location_id"]
            for entry in all_location_entries(legacy)
        } | {
            entry["location_id"]
            for category in (
                "mission_challenges",
                "all_mission_challenges",
                "mission_complete",
            )
            for entry in _package_runtime_locations(category)
        }
        self.assertEqual(
            {entry["location_id"] for entry in entries},
            expected_ids,
        )
        legacy_challenges = [
                ("Cultist Base - Mission Challenge - Pull the Crystal", 7770138),
                ("Cultist Base - Mission Challenge - Armored Rain", 7770139),
                ("Cultist Base - Mission Challenge - Master of Turrets", 7770140),
                ("Doom Hunter Base - Mission Challenge - Musical Interlude", 7770172),
                ("Doom Hunter Base - Mission Challenge - Big Reveal", 7770173),
                ("Doom Hunter Base - Mission Challenge - Fire in the Hole", 7770174),
                ("Super Gore Nest - Mission Challenge - Weaponslave", 7770206),
                ("Super Gore Nest - Mission Challenge - A Bloody Secret", 7770207),
                ("Super Gore Nest - Mission Challenge - War Pinkies", 7770208),
                ("ARC Complex - Mission Challenge - Rune Finder", 7770244),
                ("ARC Complex - Mission Challenge - External Combustion", 7770245),
                ("ARC Complex - Mission Challenge - Solitary Confinement", 7770246),
                ("Mars Core - Mission Challenge - Big Ba-Da Boom", 7770285),
                ("Mars Core - Mission Challenge - Disarmament", 7770286),
                ("Mars Core - Mission Challenge - Lock and Key", 7770287),
        ]
        challenge_pairs = [
            (entry["name"], entry["location_id"])
            for entry in registry["mission_challenges"]
        ]
        self.assertEqual(challenge_pairs[:len(legacy_challenges)], legacy_challenges)
        package_challenge_ids = {
            entry["location_id"]
            for entry in _package_runtime_locations("mission_challenges")
        }
        self.assertEqual(
            len(challenge_pairs),
            len(legacy_challenges) + len(package_challenge_ids),
        )
        self.assertEqual(
            {
                location_id for _name, location_id in challenge_pairs
                if location_id in package_challenge_ids
            },
            package_challenge_ids,
        )
        package_aggregate_ids = {
            entry["location_id"]
            for entry in _package_runtime_locations("all_mission_challenges")
        }
        self.assertEqual(
            len(registry["all_mission_challenges"]),
            5 + len(package_aggregate_ids),
        )
        self.assertEqual(
            registry["all_mission_challenges"][0],
            {
                "name": "Cultist Base - All Mission Challenges Completed",
                "location_id": 7770141,
                "mission_key": "e1m3",
                "signal": {
                    "kind": "aggregate",
                    "children": [7770138, 7770139, 7770140],
                    "required_count": 3,
                    "authority": "server_checked_locations",
                },
            },
        )
        self.assertEqual(
            registry["all_mission_challenges"][1],
            {
                "name": "Doom Hunter Base - All Mission Challenges Completed",
                "location_id": 7770175,
                "mission_key": "e1m4",
                "signal": {
                    "kind": "aggregate",
                    "children": [7770172, 7770173, 7770174],
                    "required_count": 3,
                    "authority": "server_checked_locations",
                },
            },
        )
        self.assertEqual(
            registry["all_mission_challenges"][2],
            {
                "name": "Super Gore Nest - All Mission Challenges Completed",
                "location_id": 7770209,
                "mission_key": "e2m1",
                "signal": {
                    "kind": "aggregate",
                    "children": [7770206, 7770207, 7770208],
                    "required_count": 3,
                    "authority": "server_checked_locations",
                },
            },
        )
        self.assertEqual(
            registry["all_mission_challenges"][3],
            {
                "name": "ARC Complex - All Mission Challenges Completed",
                "location_id": 7770247,
                "mission_key": "e2m2",
                "signal": {
                    "kind": "aggregate",
                    "children": [7770244, 7770245, 7770246],
                    "required_count": 3,
                    "authority": "server_checked_locations",
                },
            },
        )
        self.assertEqual(
            registry["all_mission_challenges"][4],
            {
                "name": "Mars Core - All Mission Challenges Completed",
                "location_id": 7770288,
                "mission_key": "e2m3",
                "signal": {
                    "kind": "aggregate",
                    "children": [7770285, 7770286, 7770287],
                    "required_count": 3,
                    "authority": "server_checked_locations",
                },
            },
        )
        package_challenges = [
            entry for entry in registry["mission_challenges"]
            if entry["location_id"] in package_challenge_ids
        ]
        package_aggregates = [
            entry for entry in registry["all_mission_challenges"]
            if entry["location_id"] in package_aggregate_ids
        ]
        self.assertEqual(
            {entry["location_id"] for entry in package_aggregates},
            package_aggregate_ids,
        )
        for aggregate in package_aggregates:
            with self.subTest(aggregate=aggregate["name"]):
                children = [
                    entry["location_id"]
                    for entry in package_challenges
                    if entry["mission_key"] == aggregate["mission_key"]
                ]
                self.assertEqual(aggregate["strategy"], "aggregate")
                self.assertEqual(aggregate["signal"], {
                    "kind": "aggregate",
                    "children": children,
                    "required_count": len(children),
                    "authority": "server_checked_locations",
                })
        self.assertEqual(len(registry["weapon_masteries"]), 13)
        self.assertEqual(
            [entry["name"] for entry in registry["weapon_masteries"]],
            [
                "Sticky Bombs - Weapon Mastery Challenge",
                "Full Auto - Weapon Mastery Challenge",
                "Precision Bolt - Weapon Mastery Challenge",
                "Micro Missiles - Weapon Mastery Challenge",
                "Heat Blast - Weapon Mastery Challenge",
                "Microwave Beam - Weapon Mastery Challenge",
                "Lock-on Burst - Weapon Mastery Challenge",
                "Remote Detonate - Weapon Mastery Challenge",
                "Destroyer Blade - Weapon Mastery Challenge",
                "Arbalest - Weapon Mastery Challenge",
                "Mobile Turret - Weapon Mastery Challenge",
                "Energy Shield - Weapon Mastery Challenge",
                "Meat Hook - Weapon Mastery Challenge",
            ],
        )
        self.assertTrue(all(entry["typed_perk_delivery_valid"] for entry in registry["weapon_masteries"]))
        runtime = json.loads((ROOT / "data" / "runtime_locations.json").read_text())
        self.assertEqual(runtime, {entry["name"]: entry["location_id"] for entry in entries})

    def test_hub_aliases_compare_as_one_canonical_map(self):
        self.assertEqual(canonical_map_name("game/hub/hub"), "game/hub/hub")
        self.assertEqual(canonical_map_name("game/sp/hub/hub"), "game/hub/hub")

    def test_aggregate_mission_keys_are_exclusive_and_complete(self):
        registry = load_challenge_registry()
        broken = copy.deepcopy(registry)
        broken["all_mission_challenges"][1]["mission_key"] = "e1m3"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate mission_key"):
                load_challenge_registry(path)

    def test_rejected_watcher_and_runtime_location_architecture_cannot_return(self):
        source = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("bridge_client.py", "tools/maps/ap_map_generator.py", "challenge_registry.py")
        )
        for forbidden in (
            "append_graph_entries", "watchers_for_map", "AP_RUNTIME_CHECK_",
            "3_900_000_000", "3_800_000_000", "perk/ap/", "logicentity/ap/",
            "AggregateVal", "GameDurStats", "MASTERY_EARNED",
        ):
            self.assertNotIn(forbidden, source)

    def test_all_mastery_overrides_split_natural_and_ap_paths(self):
        registry = load_challenge_registry()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = build_mastery_overrides(root)
            self.assertEqual(audit["mastery_count"], 13)
            self.assertEqual(len(audit["written_paths"]), 26)
            for entry in registry["weapon_masteries"]:
                unlockable_source = (
                    ROOT / "vanilla_decls" / "owners" / "gameresources" /
                    "generated" / "decls" / entry["decls"]["unlockable"]["path"]
                ).read_text(encoding="utf-8")
                unlockable = (
                    root / "gameresources" / "generated" / "decls" /
                    entry["decls"]["unlockable"]["path"]
                ).read_text(encoding="utf-8")
                perk = (
                    root / "gameresources" / "generated" / "decls" /
                    entry["decls"]["perk"]["path"]
                ).read_text(encoding="utf-8")
                self.assertIn(f'perkToGive = "{entry["gameplay_perk"]}";', unlockable_source)
                self.assertNotIn("perkToGive", unlockable)
                self.assertIn("addStats", (
                    ROOT / "vanilla_decls" / "owners" / "gameresources" /
                    "generated" / "decls" / entry["decls"]["perk"]["path"]
                ).read_text(encoding="utf-8"))
                self.assertNotIn("addStats", perk)
                self.assertNotIn("MASTERY_EARNED", perk)
                self.assertNotIn("STAT_CURRENT_MASTERIES_AQUIRED", perk)
                self.assertIn("upgrades", perk)

        self.assertEqual(STICKY_DECLS["unlockable"]["path"], "unlockable/weapon_mastery/shotgun/sticky_bomb.decl")

    def test_challenge_children_suppress_inherited_and_aggregate_rewards(self):
        registry = load_challenge_registry()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = build_mission_challenge_overrides(root)
            legacy_ids = [
                    7770138, 7770139, 7770140,
                    7770172, 7770173, 7770174,
                    7770206, 7770207, 7770208,
                    7770244, 7770245, 7770246,
                    7770285, 7770286, 7770287,
            ]
            self.assertEqual(audit["location_ids"][:len(legacy_ids)], legacy_ids)
            self.assertEqual(
                set(audit["location_ids"][len(legacy_ids):]),
                {
                    entry["location_id"]
                    for entry in _package_runtime_locations(
                        "mission_challenges"
                    )
                },
            )
            aggregate_audit = audit["aggregate_reward_suppression"]
            self.assertEqual(
                {
                    key: aggregate_audit[key]
                    for key in (
                        "strategy", "source_path", "completion_unlock",
                        "container_path", "aggregate_count",
                    )
                },
                {
                    "strategy": "completion_unlock_empty_container",
                    "source_path": AGGREGATE_LIST_PATH,
                    "completion_unlock": NO_REWARD_CONTAINER,
                    "container_path": NO_REWARD_CONTAINER_PATH,
                    "aggregate_count": len(registry["all_mission_challenges"]),
                },
            )
            self.assertEqual(
                {contract["mission_key"] for contract in aggregate_audit["contracts"]},
                {entry["mission_key"] for entry in registry["all_mission_challenges"]},
            )
            self.assertEqual(
                len(audit["written_paths"]),
                len(registry["mission_challenges"]) + 2,
            )
            for entry in registry["mission_challenges"]:
                source = (
                    ROOT / "vanilla_decls" / "owners" / "gameresources" /
                    "generated" / "decls" / entry["completion_owner"]["path"]
                ).read_text(encoding="utf-8")
                override = (
                    root / "gameresources" / "generated" / "decls" /
                    entry["completion_owner"]["path"]
                ).read_text(encoding="utf-8")
                self.assertEqual(override.replace(REWARD_FIELD, "", 1), source)
                self.assertIn("currencyToGive", override)
                self.assertIn("num = 0;", override)
                self.assertNotIn("CURRENCY_PRAETOR_UPGRADE", override)
                self.assertIn(entry["completion_owner"]["completion_stat"], override)
            self.assertFalse((
                root / "gameresources" / "generated" / "decls" /
                "unlockable/mission_challenge/challenge_base.decl"
            ).exists())
            aggregate_override = (
                root / "gameresources" / "generated" / "decls" /
                AGGREGATE_LIST_PATH
            ).read_text(encoding="utf-8")
            aggregate_source = (
                ROOT / "vanilla_decls" / "owners" / "gameresources" /
                "generated" / "decls" / AGGREGATE_LIST_PATH
            ).read_text(encoding="utf-8")
            self.assertEqual(
                aggregate_override.replace(
                    f'\n\t\t\t\tcompletionUnlock = "{NO_REWARD_CONTAINER}";',
                    "",
                ),
                aggregate_source,
            )
            production_blocks = {
                index: block
                for index, _, _, block in _level_blocks(aggregate_override)
                if "_dev_" not in block and _challenge_paths(block)
            }
            self.assertEqual(
                sum(
                    block.count(f'completionUnlock = "{NO_REWARD_CONTAINER}";')
                    for block in production_blocks.values()
                ),
                len(registry["all_mission_challenges"]),
            )
            for contract in aggregate_audit["contracts"]:
                block = production_blocks[contract["level_index"]]
                self.assertEqual(
                    set(_challenge_paths(block)),
                    set(contract["unlockables"]),
                )
                self.assertNotRegex(
                    block,
                    r"CURRENCY_|inventoryItemReward|currencyToGive",
                )
            self.assertEqual(
                (
                    root / "gameresources" / "generated" / "decls" /
                    NO_REWARD_CONTAINER_PATH
                ).read_text(encoding="utf-8"),
                NO_REWARD_CONTAINER_DECL,
            )
            self.assertFalse((
                root / "gameresources" / "generated" / "decls" /
                "propitem/propitem/batteries/sentinel_battery.decl"
            ).exists())
            self.assertFalse((
                root / "gameresources" / "generated" / "decls" /
                "entitydef/interact/hub/battery_socket_for_engine.decl"
            ).exists())
            registry_path = root / "challenge_location_registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            self.assertEqual(
                validate_overrides_from_mod_root(root, registry_path),
                [],
            )

    def test_synthetic_aggregate_reward_is_stripped_without_touching_predicate_or_presentation(self):
        source = """item[42] = {
\tlevelName = "#str_synthetic";
\tcompletionUnlock = "currency/CURRENCY_SENTINEL_BATTERY";
\tchallenges = {
\t\tnum = 1;
\t\titem[0] = "mission_challenge/synthetic/challenge_1";
\t}
\tfeats = {
\t\tnum = 1;
\t\titem[0] = "shell_skybox/synthetic";
\t}
}"""
        override = _suppress_aggregate_reward(source)
        self.assertNotIn("CURRENCY_SENTINEL_BATTERY", override)
        self.assertIn(f'completionUnlock = "{NO_REWARD_CONTAINER}";', override)
        self.assertEqual(_challenge_paths(override), _challenge_paths(source))
        self.assertIn('levelName = "#str_synthetic";', override)
        self.assertIn('item[0] = "shell_skybox/synthetic";', override)

    def test_packaged_audit_rejects_residual_aggregate_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_mission_challenge_overrides(root)
            registry_path = root / "challenge_location_registry.json"
            registry_path.write_text(
                json.dumps(load_challenge_registry()), encoding="utf-8"
            )
            aggregate = (
                root / "gameresources" / "generated" / "decls" /
                AGGREGATE_LIST_PATH
            )
            source = aggregate.read_text(encoding="utf-8")
            aggregate.write_text(
                source.replace(
                    f'completionUnlock = "{NO_REWARD_CONTAINER}";',
                    'completionUnlock = "currency/CURRENCY_SENTINEL_BATTERY";',
                    1,
                ),
                encoding="utf-8",
            )
            errors = validate_overrides_from_mod_root(root, registry_path)
        self.assertTrue(any("aggregate suppression is missing" in error for error in errors))
        self.assertTrue(any("aggregate retains a vanilla reward" in error for error in errors))

    def test_rune_menu_override_preserves_existing_rune_behavior(self):
        source = (
            ROOT / "vanilla_decls" / "owners" / RUNE_OWNER["container"] /
            "generated" / "decls" / RUNE_OWNER["path"]
        ).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build_rune_override(output)
            override = (output / RUNE_OWNER["container"] / "generated" / "decls" / RUNE_OWNER["path"]).read_text(encoding="utf-8")
        self.assertEqual(source.count(GATE_LINE), 1)
        self.assertEqual(override, source.replace(GATE_LINE, "", 1))


if __name__ == "__main__":
    unittest.main()
