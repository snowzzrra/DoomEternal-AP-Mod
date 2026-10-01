import json

from doom_eap.launcher.sentinel_diagnostics import collect


def test_diagnostics_correlate_only_selected_process(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "SentinelCore/diagnostics"
    root.mkdir(parents=True)
    session = {"pid": 71, "process_created": 123, "build_id": "build", "namespace_id": "room"}
    record = {"schema": "sentinel-startup-v1", "pid": 71, "process_created": "123", "build_id": "build", "session": {"namespace_id": "room"}}
    wrong = {**record, "pid": 72}
    malformed = {**record, "session": "invalid"}
    (root / "71-123.jsonl").write_text("\n".join(json.dumps(row) for row in (record, wrong, malformed)))
    result = collect({}, session)
    assert len(result["records"]) == 2
    assert result["records"][0]["correlation"] == {"build_matches": True, "namespace_matches": True}
    assert result["records"][1]["correlation"]["namespace_matches"] is False
