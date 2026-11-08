import asyncio
import copy
from pathlib import Path

import pytest

from doom_eap.runtime.inventory import (
    StartingOwnershipError,
    load_starting_ownership,
)
from tools.decls.devinv_builder import (
    load_devinv_mapping,
)

ROOT = Path(__file__).resolve().parents[1]
STARTING = {"items": [[7770004, 1], [7770016, 2], [7770142, 1], [7770902, 1]], "weapon_item_id": 7770004}


def test_legacy_connected_is_rejected_before_session_binding(bridge, tmp_path, monkeypatch):
    from unittest.mock import Mock

    from doom_eap.runtime import bridge_config

    async def run():
        ctx = bridge.DoomEternalContext(None, None)
        bind = Mock(side_effect=AssertionError("Rejected rooms cannot bind a session"))
        failure = Mock()
        monkeypatch.setattr(ctx, "_handle_package", bind)
        monkeypatch.setattr(ctx, "_report_launcher_connection_failure", failure)
        room = {"slot_data_schema_version": 6}
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        await ctx._validate_connected_package({"slot_data": room})
        bind.assert_not_called()
        assert failure.call_args.kwargs["code"] == "slot_schema"
        assert "DoomEAP " + bridge_config._CONTENT_IDENTITY["release_version"] in failure.call_args.args[0]
        assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before

    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"items": [[7770004, True]]}, {"items": [[True, 1]]}, {"items": [[7770004, 1], [7770004, 1]]},
    {"items": [[7770004, 0]]}, {"items": [[7770004, 1], [7770025, 1]]},
    {"weapon_item_id": 7770001}, {"weapon_item_id": True},
])
def test_invalid_starting_plan_has_a_typed_error(change):
    starting = {**copy.deepcopy(STARTING), **change}
    with pytest.raises(StartingOwnershipError):
        load_starting_ownership(starting, load_devinv_mapping())
