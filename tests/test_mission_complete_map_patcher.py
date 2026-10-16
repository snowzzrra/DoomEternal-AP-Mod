import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.contracts.publisher_contracts import PublisherContract
from tools.maps import mission_complete_map_patcher as patcher
from tools.maps import unified_campaign


@pytest.mark.parametrize("defect", [None, "hell_missing", "hell_hash", "hell_targets",
                                  "map_missing", "owner_hash", "terminal_hash"])
def test_mission_publishers_use_packaged_sources_and_reject_drift(tmp_path, monkeypatch, defect):
    digest = lambda raw: hashlib.sha256(raw).hexdigest()
    exultia = ('entity {\nentityDef exit {\nclass = "idTarget_Count";\n'
               'edit = {\ncount = 1;\ntargets = {\nnum = 1;\nitem[0] = "native_exit";\n}\n}\n}\n}')
    prime = ('entity {\nentityDef prime_exit {\ninherit = "target/level_transition";\n'
             'class = "idTarget_LevelTransition";\nedit = {\n'
             'nextMapName = "maps/game/hub/hub.map";\n}\n}\n}')
    hell = (b'{ item[0] = { id = 7; className = "idLogicNodeModelEntityActivate"; '
            b'value = { num = 2; item[0] = "AP_HELL"; item[1] = "native_exit"; } } }\r\n')
    contracts = {
        "schema_version": 1,
        "hell": {"map_key": "e1m1_intro", "patch_strategy": "logic_node_targets", "node_id": 7,
                 "source_path": "../Tools/VanillaEntities/hell.decl", "source_sha256": "pinned-source",
                 "override_path": "hell_patch3/generated/hell.decl", "override_sha256": digest(hell),
                 "ap_check": "AP_HELL", "original_targets": ["native_exit"], "location_id": 7770122},
        "exultia": {"map_key": "e1m2_war", "patch_strategy": "entity_targets", "owner": "exit",
                    "source_path": "vanillamaps/exultia.map", "source_sha256": digest(exultia.encode()),
                    "ap_check": "AP_EXULTIA", "original_targets": ["native_exit"], "location_id": 7770123,
                    "required_snippets": ['class = "idTarget_Count";', "count = 1;"]},
        "prime": {"map_key": "e2m4_boss", "patch_strategy": "terminal_publishers", "owner": "prime_exit",
                  "native_owner": "prime_exit_native", "source_path": "vanillamaps/prime.map",
                  "source_sha256": digest(prime.encode()), "original_targets": [],
                  "required_snippets": ['inherit = "target/level_transition";'], "location_id": 7770290},
    }
    publisher = PublisherContract(
        key="sentinel_prime_mission_complete", map_key="e2m4_boss",
        triggers=({"strategy": "map_event_file", "owner": "prime_exit", "filename": "prime.txt", "marker": "PRIME"},),
        effects=({"strategy": "preserved_native_target", "target": "prime_exit_native"},),
        dedupe_scope="room", fallback_policy="none",
    )
    catalog = SimpleNamespace(maps={key: None for key in ("e1m1_intro", "e1m2_war", "e2m4_boss")},
                              publishers=(publisher,))
    monkeypatch.setattr(patcher, "load_content_catalog", lambda _root: catalog)
    monkeypatch.setattr(unified_campaign, "project_stage_return", lambda _catalog, _key, text: (text, []))
    read_bytes, read_text = Path.read_bytes, Path.read_text

    def guard(reader):
        def read(path, *args, **kwargs):
            assert not {"vanillamaps", "VanillaEntities"} & set(path.parts)
            return reader(path, *args, **kwargs)
        return read

    monkeypatch.setattr(Path, "read_bytes", guard(read_bytes))
    monkeypatch.setattr(Path, "read_text", guard(read_text))
    generated = {}
    for key, text in {"e1m1_intro": "World\n", "e1m2_war": exultia, "e2m4_boss": prime}.items():
        generated[key] = tmp_path / f"{key}.entities"
        generated[key].write_text(text, encoding="utf-8", newline="")
    snapshots = {"e1m2_war": exultia.replace("\n", "\r\n"), "e2m4_boss": prime.replace("\n", "\r\n")}
    retained = {contracts["hell"]["override_path"]: hell}
    if defect == "hell_missing":
        retained.clear()
    elif defect == "hell_hash":
        retained[contracts["hell"]["override_path"]] += b" "
    elif defect == "hell_targets":
        changed = hell.replace(b'item[1] = "native_exit"', b'item[1] = "wrong_exit"')
        retained[contracts["hell"]["override_path"]] = changed
        contracts["hell"]["override_sha256"] = digest(changed)
    elif defect == "map_missing":
        del snapshots["e1m2_war"]
    elif defect == "owner_hash":
        snapshots["e1m2_war"] = snapshots["e1m2_war"].replace("count = 1;", "count = 2;")
    elif defect == "terminal_hash":
        snapshots["e2m4_boss"] = snapshots["e2m4_boss"].replace("target/level_transition", "target/changed")
    contract_path = tmp_path / "data/contracts.json"
    contract_path.parent.mkdir()
    contract_path.write_text(json.dumps(contracts), encoding="utf-8")
    output = tmp_path / "output"
    if defect:
        message = {"hell_missing": "missing retained Hell override", "hell_hash": "Hell override hash mismatch",
                   "hell_targets": "Hell retained override target drift", "map_missing": "missing Mission Complete source map",
                   "owner_hash": "Exultia source owner hash mismatch", "terminal_hash": "Sentinel Prime terminal owner hash mismatch"}[defect]
        with pytest.raises(ValueError, match=message):
            patcher.patch_mission_complete_maps(contract_path, generated, output,
                                               source_maps=snapshots, retained_overrides=retained)
        return
    audit = patcher.patch_mission_complete_maps(contract_path, generated, output,
                                               source_maps=snapshots, retained_overrides=retained)
    assert (output / contracts["hell"]["override_path"]).read_bytes() == hell
    assert audit["hell"]["after_targets"] == ["AP_HELL", "native_exit"]
    assert audit["hell"]["source_sha256"] is None
    assert audit["hell"]["expected_source_sha256"] == contracts["hell"]["source_sha256"]
    assert audit["hell"]["changed_lists"] == 0
    assert audit["exultia"]["after_targets"] == ["AP_EXULTIA", "native_exit"]
    assert 'entityDef prime_exit_native' in generated["e2m4_boss"].read_text()
    for key, location in (("e1m1_intro", 7770122), ("e1m2_war", 7770123)):
        assert generated[key].read_text().count(f"AP_CHECK_EVENT_{location}") == 1
    assert audit["unrelated_generated_entity_diff_count"] == 0
