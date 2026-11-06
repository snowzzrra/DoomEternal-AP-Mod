import json
from pathlib import Path

from doom_eap.content.slot_data_contract import catalog_digest, generation_fingerprint, load_slot_catalog


def current_room_fixture():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "tests/fixtures/resolved_room_slot_data.json").read_bytes())
    identity = json.loads((root / "data/content_identity.json").read_bytes())
    # the fixture represents a room without arena locations.
    data["runtime"]["checks"]["arena_checks"] = 4
    data["identity"]["catalog_digest"] = catalog_digest(load_slot_catalog())
    data["compatibility"]["mod_contract_revision"] = identity["session_mod_contract_revision"]
    data["native_generation_fingerprint"] = generation_fingerprint(data)
    return data
