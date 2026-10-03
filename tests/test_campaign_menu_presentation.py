import struct
from types import SimpleNamespace

from doom_eap.runtime.campaign_menu import CampaignMenu


class NativeLink:
    namespace = "a" * 64
    pid = 7

    def admitted_save_root(self):
        pass

    def _run(self, args, message=None):
        if message is None:
            return dict(availability="enabled", target_pid=7, process_created=8,
                        instance_id="b" * 32, lifecycle_generation=1, build_id="fixture")
        revision = struct.unpack_from("<Q", message, 153)[0]
        return dict(namespace=self.namespace, request_id=struct.unpack_from("<Q", message, 60)[0],
                    build_id="fixture", status=0, committed_revision=revision,
                    selected_id=1, loaded_id=1)


def test_static_tiers_old_relative_contract_and_hidden_privacy():
    link = NativeLink()
    stage = "e1m1_intro"
    runtime_map = "game/sp/e1m1_intro/e1m1_intro"
    row = dict(stage=stage, map=runtime_map, title="Hell on Earth", revealed=True,
               unlocked=True, completed=False, goal=False, details_visible=True)
    summary = SimpleNamespace(namespace=link.namespace, runtime_map=runtime_map,
                              found=1, total=2, challenges=())
    snapshot = dict(fortress_phase=1, hub_map="hub", rows=[row], selected="hub",
                    summaries={stage: summary}, ratings_version=2,
                    ratings={stage: {"skull_tier": 1}})
    menu = CampaignMenu()
    menu.synchronize(link, snapshot)
    assert struct.unpack_from("<I", menu.rows[1][1], 12)[0] == 1
    snapshot["ratings_version"] = 1
    snapshot["ratings"][stage] = dict(base_cr=86, expected_player_cr=10, skill_allowance=10)
    menu.synchronize(link, snapshot)
    assert struct.unpack_from("<I", menu.rows[1][1], 12)[0] == 4 | (1001 << 8)
    row["revealed"] = False
    snapshot["ratings_version"] = 2
    snapshot["ratings"][stage] = {"skull_tier": 99}
    menu.synchronize(link, snapshot)
    assert struct.unpack_from("<I", menu.rows[1][1], 12)[0] == 0
