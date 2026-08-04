"""Atomic APWorld/bridge identity compatibility contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARCHIPELAGO = ROOT.parent / "Archipelago"


def _launcher():
    settings = types.ModuleType("settings")
    settings.get_settings = lambda: {"doometernal_options": {"client_directory": "unused"}}
    utils = types.ModuleType("Utils")
    utils.messagebox = lambda *args, **kwargs: None
    old_settings = sys.modules.get("settings")
    old_utils = sys.modules.get("Utils")
    sys.modules["settings"] = settings
    sys.modules["Utils"] = utils
    try:
        path = ARCHIPELAGO / "worlds" / "doometernal" / "Client.py"
        spec = importlib.util.spec_from_file_location("identity_launcher", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old_settings is None:
            sys.modules.pop("settings", None)
        else:
            sys.modules["settings"] = old_settings
        if old_utils is None:
            sys.modules.pop("Utils", None)
        else:
            sys.modules["Utils"] = old_utils


def test_current_bridge_identity_is_accepted_and_legacy_identity_is_rejected() -> None:
    launcher = _launcher()
    with tempfile.TemporaryDirectory() as directory:
        bridge = Path(directory) / "bridge_client.py"
        bridge.write_text("# identity fixture\n", encoding="utf-8")
        sha256 = hashlib.sha256(bridge.read_bytes()).hexdigest()
        identity = {
            "protocol": 3,
            "game": "DOOM Eternal",
            "sha256": sha256,
            "revision": f"mission-unified-{sha256[:12]}",
        }
        bridge.with_name("bridge_identity.json").write_text(json.dumps(identity), encoding="utf-8")
        assert launcher._bridge_identity(bridge) == (sha256, identity["revision"])

        identity["game"] = "Doom Eternal"
        bridge.with_name("bridge_identity.json").write_text(json.dumps(identity), encoding="utf-8")
        try:
            launcher._bridge_identity(bridge)
        except RuntimeError as error:
            assert "Old 'Doom Eternal' seeds require the prior client/APWorld" in str(error)
        else:
            raise AssertionError("legacy identity was accepted")
