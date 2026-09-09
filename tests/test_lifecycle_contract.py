"""The lifecycle domain imports and runs without registry/configuration discovery."""

from pathlib import Path
import subprocess
import sys


def test_lifecycle_is_independent_of_registry_discovery():
    code = r'''
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

with patch.object(Path, "read_text", side_effect=AssertionError("unexpected discovery")):
    from doom_eap.runtime.lifecycle import RuntimeLifecycle
    from doom_eap.contracts.runtime_context import RuntimeContext, canonical_map_name
    lifecycle = RuntimeLifecycle()
    context = RuntimeContext("base", "Base", ("game/hub/hub",), ("hub",), frozenset())
    assert lifecycle.bind_context(context) == ("Unknown", "Base")
    resolution = lifecycle.resolve_context(
        "game/hub/hub", "unknown", materialization_suspended=False,
        contexts_by_map={"game/hub/hub": context},
    )
    assert resolution.context is context
    assert resolution.rejection[0] == "accepted_marker_over_unrecognized_save"
    evidence = SimpleNamespace(state="gameplay", epoch=1, map_name="game/sp/hub/hub", provisional=False)
    decision = lifecycle.classify_native_load(evidence, {"hub": "game/hub/hub"})
    assert lifecycle.native_marker_proposal(decision, 100)["gameplay_epoch"] == "1:100"
    assert canonical_map_name(None) is None
    assert canonical_map_name(False) is False
    assert canonical_map_name(" game\\sp\\hub\\hub/ ") == "game/hub/hub"
assert "doom_eap.runtime.context_registry" not in sys.modules
assert "doom_eap.contracts.challenge_registry" not in sys.modules
assert "doom_eap.runtime.bridge_client" not in sys.modules
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
