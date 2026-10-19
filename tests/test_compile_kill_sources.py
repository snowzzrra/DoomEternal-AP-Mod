import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.content.content_catalog import load_content_catalog
from tools.content.compile_content_catalog import compile_kill_sources


@pytest.mark.parametrize("defect", ["missing_event", "protected_event", "wrong_secret", "duplicate", "count", "source_hash"])
def test_kill_sources_refuse_invalid_event_or_access_before_packaging(tmp_path, defect):
    catalog = load_content_catalog()
    root = Path(__file__).resolve().parents[1]
    estimates = json.loads((root / "content/catalog/killsanity_estimates.json").read_bytes())
    native = json.loads((root / "doom_eap/content/native_enemy_catalog.json").read_bytes())
    secret = next(item for item in catalog.physical_locations if item.strategy == "secret_encounter"
                  and item.name in estimates["maps"][item.map_key].get("event_sources", {}))
    record = estimates["maps"][secret.map_key]
    events = record["event_sources"][secret.name]
    identity = next(iter(events))
    if defect in {"missing_event", "protected_event"}:
        from doom_eap.content.native_enemy_plan import native_event_identity
        origins = native["maps"][secret.map_key]["origins"]
        selected = [row for row in origins if "binding" in row and native_event_identity(row) == identity]
        assert selected
        for row in selected:
            if defect == "missing_event":
                origins.remove(row)
            else:
                row["protected_reason"] = "protected encounter"
    elif defect == "wrong_secret":
        record["event_sources"]["Another fight"] = record["event_sources"].pop(secret.name)
    elif defect == "duplicate":
        record["event_sources"]["Another fight"] = deepcopy(events)
    elif defect == "count":
        events[identity] = 0
    else:
        record["source_sha256"] = "0" * 64
    for key in estimates["maps"]:
        path = tmp_path / "content/maps" / key / "descriptor.json"
        path.parent.mkdir(parents=True)
        path.write_bytes((root / "content/maps" / key / "descriptor.json").read_bytes())
    target = tmp_path / "content/catalog/killsanity_estimates.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(estimates), encoding="utf-8")
    isolated = SimpleNamespace(root=tmp_path, maps=catalog.maps, runtime_locations=catalog.runtime_locations,
                               physical_locations=catalog.physical_locations, region_metadata=catalog.region_metadata)
    with pytest.raises(ValueError):
        compile_kill_sources(isolated, native)
