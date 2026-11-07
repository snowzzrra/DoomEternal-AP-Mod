import struct
from types import SimpleNamespace

from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.campaign import CampaignMenu


class NativeLink:
    namespace = "a" * 64
    pid = 7

    def __init__(self):
        self.requests = []

    def admitted_save_root(self):
        pass

    def _run(self, args):
        return dict(availability="enabled", target_pid=7, process_created=8,
                    instance_id="b" * 32, lifecycle_generation=1, build_id="fixture")

    def request(self, operation, *, payload):
        encoded = codec.encode_variant(operation, 4, 0, payload)
        _, decoded = codec.decode_variant(operation, 4, encoded)
        self.requests.append((operation, decoded))
        return dict(namespace=self.namespace, build_id="fixture", status=0,
                    committed_revision=payload.get('revision', 0),
                    selected_id=1, loaded_id=1)


def test_static_tiers_and_hidden_mission_privacy():
    link = NativeLink()
    stage = "e1m1_intro"
    runtime_map = "game/sp/e1m1_intro/e1m1_intro"
    row = dict(
        stage=stage,
        map=runtime_map,
        title="Hell on Earth",
        revealed=True,
        unlocked=True,
        completed=False,
        goal=False,
        details_visible=True,
    )
    summary = SimpleNamespace(namespace=link.namespace, runtime_map=runtime_map, found=1, total=2, challenges=())
    snapshot = dict(
        fortress_phase=1,
        hub_map="hub",
        rows=[row],
        selected="hub",
        summaries={stage: summary},
        ratings_version=2,
        ratings={stage: {"skull_tier": 1}},
    )
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


def _assert_menu_selection_and_rewards(commit, hub, mission):
    assert hub['rewards_known'] == 1
    assert mission['rewards_known'] == 0 and mission['rewards'] == []
    assert commit['count'] == 2 and commit['selected_id'] == mission['row']['id']
    assert hub['revision'] == mission['revision'] == commit['revision']


def test_upload_preserves_selection_and_unknown_rewards():
    link = NativeLink()
    stage = "e1m1_intro"
    row = dict(stage=stage, map="game/sp/e1m1_intro/e1m1_intro", title="Hell on Earth",
               revealed=True, unlocked=True)
    snapshot = dict(fortress_phase=0, hub_map="game/hub/hub", rows=[row], selected=stage,
                    challenge_rewards={})
    menu = CampaignMenu()
    menu.synchronize(link, snapshot)
    assert [operation for operation, _ in link.requests] == [24, 22, 22, 23]
    hub, mission, commit = [payload for _, payload in link.requests[1:]]
    _assert_menu_selection_and_rewards(commit, hub, mission)
    link.requests.clear()
    menu.synchronize(link, snapshot)
    assert [operation for operation, _ in link.requests] == [24]
    snapshot['challenge_rewards'][stage] = SimpleNamespace(
        namespace=link.namespace, runtime_map=row['map'], known=True, challenges=(), aggregate=None)
    menu.synchronize(link, snapshot)
    assert link.requests[-2][1]['rewards_known'] == 1
