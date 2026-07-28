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
from tools.decls.mission_challenge_decl_builder import REWARD_FIELD, build_mission_challenge_overrides
from tools.decls.rune_decl_builder import GATE_LINE, RUNE_OWNER, build_rune_override

ROOT = Path(__file__).resolve().parents[2]


class NativeChallengeContracts(unittest.TestCase):
    def test_registry_contains_only_proven_runtime_locations(self):
        registry = load_challenge_registry()
        entries = all_location_entries(registry)
        self.assertEqual(
            {entry["location_id"] for entry in entries},
            {
                7770122, 7770123, 7770124, 7770162, 7770210, 7770248,
                *range(7770125, 7770142),
                7770172, 7770173, 7770174, 7770175,
                7770206, 7770207, 7770208, 7770209,
                7770244, 7770245, 7770246, 7770247,
                7770285, 7770286, 7770287, 7770288, 7770289, 7770290,
            },
        )
        self.assertEqual(
            [(entry["name"], entry["location_id"]) for entry in registry["mission_challenges"]],
            [
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
            ],
        )
        self.assertEqual(
            len(registry["all_mission_challenges"]), 5,
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
            self.assertEqual(
                audit["location_ids"],
                [
                    7770138, 7770139, 7770140,
                    7770172, 7770173, 7770174,
                    7770206, 7770207, 7770208,
                    7770244, 7770245, 7770246,
                    7770285, 7770286, 7770287,
                ],
            )
            self.assertEqual(audit["aggregate_reward_suppression"], {
                "strategy": "child_currencyToGive_num_zero",
                "field": "currencyToGive.num",
                "value": 0,
                "suppressed_native_rewards": [
                    "CURRENCY_PRAETOR_UPGRADE",
                ],
                "runtime_evidence": "v0.3.0c.1",
            })
            self.assertEqual(len(audit["written_paths"]), 15)
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
