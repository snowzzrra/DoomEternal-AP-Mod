"""Canonical Codex-derived presentation contract for generated AP visuals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT
CONTRACT_PATH = REPO_ROOT / "data" / "ap_visual_bundle.json"


def load_ap_visual_contract(root: Path = ROOT) -> dict[str, Any]:
    path = root / "data" / "ap_visual_bundle.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "key", "strategy", "model", "dependencies",
        "dependency_policy", "source_resource_base", "replacement_slot_policy",
        "replacement_slot", "usage_policy", "preserve",
        "visual_presentation_policy", "runtime_references",
        "forbidden_geometry", "forbidden_streamdb_payload",
    }
    if document.get("schema_version") != 1 or set(document) != required:
        raise ValueError("canonical AP visual contract schema drift")
    if document["strategy"] != "packaged_bundle":
        raise ValueError("canonical AP visual must be a packaged bundle")
    if document["model"] == document["forbidden_geometry"]:
        raise ValueError("canonical AP visual cannot use question-mark geometry")
    slot = document["replacement_slot"]
    if slot.get("model_path") != document["model"]:
        raise ValueError("canonical AP visual model/slot mismatch")
    if slot.get("streamdb_payload") == document["forbidden_streamdb_payload"]:
        raise ValueError("canonical AP visual cannot use question-mark StreamDB")
    return document
