import json

from doom_eap.content.content_catalog import load_content_catalog
from tests.delivery_scenario_support import Scenario


def assert_server_delivery(snapshot, pickup, goal):
    checks = [code for packet in snapshot["messages"] if packet["cmd"] == "LocationChecks"
              for code in packet["locations"]]
    assert checks == [pickup, goal]
    assert [packet for packet in snapshot["messages"] if packet["cmd"] == "StatusUpdate"] == [
        {"cmd": "StatusUpdate", "status": 30}]


def test_game_check_and_goal_reach_server_once(generated, tmp_path):
    document = generated("dlc")
    active = set(document["slots"][0]["world"]["active_ids"])
    pickup, goal = min(code for code in active if 7772000 <= code < 7772100), 7770419
    baseline = active - {pickup, goal}
    peer = Scenario(tmp_path)
    (peer.root / "check-room.json").write_text(json.dumps(document), encoding="utf-8")
    peer.ap.send({"action": "checked", "locations": sorted(baseline)})
    peer.ap.expect("checked")
    try:
        peer.start_checks()
        path = peer.root / "saves" / f"ap_event_{pickup}.txt"
        path.write_text(f"AP_CHECK_EVENT_{pickup}\n", encoding="utf-8")
        peer.action("flush")
        peer.action("flush")
        peer.ap.send({"action": "snapshot"})
        assert set(peer.ap.expect("snapshot")["checked"]) == baseline | {pickup}
        assert path.exists()

        peer.kill_bridge()
        assert set(peer.start_checks()["checked"]) == baseline | {pickup}
        path.write_text(f"AP_CHECK_EVENT_{pickup}\n", encoding="utf-8")
        peer.action("flush")
        assert not path.exists()

        publisher = next(p for p in load_content_catalog().publishers if p.key == "dark_lord_defeated")
        trigger, = publisher.triggers_for("map_event_file")
        (peer.root / "saves" / trigger["filename"]).write_text(trigger["marker"] + "\n", encoding="utf-8")
        peer.action("goal")
        peer.action("ack")
        peer.action("goal")
        peer.action("evaluate")
        peer.action("evaluate")

        peer.ap.send({"action": "snapshot"})
        assert_server_delivery(peer.ap.expect("snapshot"), pickup, goal)
    finally:
        peer.close()
