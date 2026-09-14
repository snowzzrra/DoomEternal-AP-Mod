"""AP visit gates and circulation against the real authored Fortress graph."""
import json
from pathlib import Path
import re
import unittest

from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.maps.fortress_campaign import project_fortress

ROOT = Path(__file__).resolve().parents[1]

def entity(text, name):
    bounds = find_entity_block_bounds(text, name)
    if not bounds:
        raise AssertionError(f'missing entity {name}')
    return text[slice(*bounds)]

class FortressCampaign(unittest.TestCase):
    def test_battery_labels_preview_rewards_before_pickup_visit(self):
        from types import SimpleNamespace
        from doom_eap.launcher.launcher_core import RoomCompiler
        compiler = object.__new__(RoomCompiler)
        records = {code: SimpleNamespace(trap=False, local=True, item_name='Weapon Upgrade Points (3)',
                   recipient_name='Phase6', classification=1) for code in compiler.FORTRESS_BATTERY_LOCATION_IDS}
        _, labels = compiler._fortress_battery_label_entities(records)
        for code in records:
            block = entity(labels, f'ap_fortress_battery_placement_{code}')
            self.assertNotIn('layers {', block)
            self.assertIn('YOUR WEAPON UPGRADE POINTS (3)', block)

    @classmethod
    def setUpClass(cls):
        cls.vanilla = (ROOT/'vanillamaps/hub.map').read_text(encoding='utf-8')
        cls.config = json.loads((ROOT/'content/maps/hub/locations.json').read_text())
        cls.result = project_fortress(cls.vanilla, cls.config)

    def test_common_doors_unlock_in_every_phase_without_spending_or_granting(self):
        doors = {
            'main_deck_target_interact_action_unlock_door_1': ['main_deck_interact_door1'],
            'main_deck_target_interact_action_unlock_door_2': ['main_deck_interact_door2'],
            'main_deck_target_interact_action_unlock_engine_door': ['main_deck_interact_engine_room_door'],
            'target_interact_action_unlock_engine_doors': ['interact_doors_sentinel_round_engine_l',
                 'interact_doors_sentinel_round_engine_r', 'interact_doors_hub_door_a_1'],
        }
        for action, targets in doors.items():
            block = entity(self.result, action)
            self.assertEqual(block, entity(self.vanilla, action))
            self.assertIn('class = "idTarget_InteractionAction";', block)
            self.assertIn('action = "IA_UNLOCKED";', block)
            self.assertEqual(re.findall(r'item\[\d+\] = "([^"]+)";', block), targets)
            for phase in range(8):
                self.assertIn(f'"{action}"', entity(self.result, f'ap_fortress_phase_{phase}'))
        for name in re.findall(r'entityDef (\w+) \{', self.vanilla):
            if 'battery_station' in name or name == 'interact_hub_battery_socket_for_engine':
                # The three AP battery-station checks themselves gain a visit
                # layer; their transactions and native target graph stay exact.
                strip_layers = lambda block: re.sub(r'\s*layers\s*\{[^}]*\}', '', block, count=1)
                self.assertEqual(strip_layers(entity(self.result, name)), strip_layers(entity(self.vanilla, name)))

    def test_story_pickups_keep_cumulative_ap_visits(self):
        for name, phase in (
            ('pickup_equipment_flame_belch_1', 1), ('progress_argent_cell_1_1072112848', 1),
            ('pickup_equipment_ice_bomb', 2), ('progress_praetor_point_hub_1', 2),
            ('pickup_weapon_gauss_rifle_hub_1', 3),
        ):
            block = entity(self.result, name)
            self.assertIn(f'layers {{ "game/sp/hub/ap_phase_{phase}" }}', block)
            self.assertNotIn('game/sp/hub/from_', block)
            for current in range(8):
                layers = entity(self.result, f'ap_fortress_layers_{current}')
                self.assertEqual(f'"game/sp/hub/ap_phase_{phase}"' in layers, current >= phase)

    def test_suit_room_barriers_remain_present_before_reward_visit(self):
        for index in (1, 2, 3):
            name = f'interact_hub_2_battery_station_{index}'
            block = entity(self.result, name)
            self.assertEqual(block, entity(self.vanilla, name))
            self.assertNotIn('layers {', block)
            self.assertIn('collisionPieces = {', block)
            self.assertIn('initalState = "interactables/progress/battery_station/2_battery_required";', block)

if __name__ == '__main__':
    unittest.main()
