import json
import unittest
from pathlib import Path

from item_reconciliation import (
    NEVER_REPLAY,
    REPLAY_IDEMPOTENT,
    SPECIAL_PROGRESSIVE,
    compile_reconciliation_plan,
    load_policy_registry,
)

ROOT = Path(__file__).parents[2]


class ItemReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = json.loads((ROOT / "data" / "items.json").read_text())
        cls.items = {int(item_id): definition for item_id, definition in raw.items()}
        cls.registry = load_policy_registry(
            ROOT / "data" / "item_replay_policies.json", cls.items
        )

    def test_registry_has_full_exact_active_item_coverage(self):
        self.assertEqual(set(self.registry), set(self.items))
        self.assertEqual(
            {entry.policy for entry in self.registry.values()},
            {REPLAY_IDEMPOTENT, SPECIAL_PROGRESSIVE, NEVER_REPLAY},
        )

    def test_missing_and_unknown_policy_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "missing policy"):
            load_policy_registry({"items": {}}, self.items)
        broken = {
            "items": {
                str(item_id): {"name": str(item_id), "policy": entry.policy}
                for item_id, entry in self.registry.items()
            }
        }
        broken["items"]["7770000"]["policy"] = "guess_from_name"
        with self.assertRaisesRegex(ValueError, "unsupported policy"):
            load_policy_registry(broken, self.items)

    def test_never_replay_produces_zero_commands(self):
        received = [
            item_id
            for item_id, entry in self.registry.items()
            if entry.policy == NEVER_REPLAY
        ]
        plan = compile_reconciliation_plan(
            received, self.items, self.registry, "seed-1-2", 7
        )
        self.assertEqual(plan.commands, ())
        self.assertEqual(plan.replayed, 0)
        self.assertEqual(plan.special_stages, 0)
        self.assertEqual(plan.skipped_never_replay, len(received))

    def test_progressive_totals_compile_exact_unique_stages(self):
        received = [7770017, 7770017, 7770088, 7770092, 7770092, 7770092, 7770021]
        plan = compile_reconciliation_plan(
            received, self.items, self.registry, "seed-1-2", 9
        )
        self.assertEqual(
            [(command.item_id, command.stage) for command in plan.commands],
            [
                (7770017, 0), (7770017, 1),
                (7770021, 0),
                (7770088, 0),
                (7770092, 0), (7770092, 1), (7770092, 2),
            ],
        )
        self.assertEqual(len({command.spool_id for command in plan.commands}), 7)
        self.assertTrue(all("Suit Point" not in command.description for command in plan.commands))
        self.assertTrue(all("CURRENCY" not in command.command for command in plan.commands))
        self.assertTrue(all("ap_rpc_v3_" in command.command for command in plan.commands))
        self.assertTrue(all("ap_rpc_item_" not in command.command for command in plan.commands))

    def test_runes_only_use_existing_give_entities(self):
        rune_ids = (7770085, 7770086, 7770087, 7770089, 7770090, 7770091, 7770093, 7770094, 7770095)
        plan = compile_reconciliation_plan(
            rune_ids, self.items, self.registry, "seed-1-2", 3
        )
        self.assertEqual(len(plan.commands), len(rune_ids))
        self.assertTrue(all(command.command.endswith(" activate") for command in plan.commands))
        self.assertFalse(any("_activate" in command.command for command in plan.commands))
        self.assertFalse(any("slot" in command.command.lower() for command in plan.commands))

    def test_seed_team_slot_and_epoch_isolate_spool_ids(self):
        first = compile_reconciliation_plan(
            [7770005], self.items, self.registry, "seed-a-1-2", 4
        )
        second = compile_reconciliation_plan(
            [7770005], self.items, self.registry, "seed-a-1-3", 4
        )
        third = compile_reconciliation_plan(
            [7770005], self.items, self.registry, "seed-a-1-2", 5
        )
        self.assertNotEqual(first.commands[0].spool_id, second.commands[0].spool_id)
        self.assertNotEqual(first.commands[0].spool_id, third.commands[0].spool_id)
        self.assertEqual(
            first.commands[0].spool_id,
            "reconcile-seed-a-1-2-e4-item7770005-stage0",
        )

    def test_repeated_safe_receipts_replay_ownership_once(self):
        plan = compile_reconciliation_plan(
            [7770005, 7770005, 7770011], self.items, self.registry, "seed-1-2", 1
        )
        self.assertEqual(plan.replayed, 2)
        self.assertEqual(len({command.spool_id for command in plan.commands}), len(plan.commands))


if __name__ == "__main__":
    unittest.main()
