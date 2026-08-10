import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from rune_reconciliation import RuneNativeState


def _bridge_module():
    try:
        return importlib.import_module("bridge_client")
    except ModuleNotFoundError:
        colorama = ModuleType("colorama")
        colorama.init = lambda: None
        colorama.deinit = lambda: None
        utils = ModuleType("Utils")
        utils.init_logging = lambda *_args, **_kwargs: None
        common = ModuleType("CommonClient")

        class ClientCommandProcessor:
            pass

        class CommonContext:
            def handle_connection_loss(self, _msg):
                pass

        common.ClientCommandProcessor = ClientCommandProcessor
        common.CommonContext = CommonContext
        common.get_base_parser = lambda: None
        common.gui_enabled = lambda: False
        common.server_loop = lambda *_args, **_kwargs: None
        net = ModuleType("NetUtils")
        net.ClientStatus = SimpleNamespace(CLIENT_GOAL=30)
        sys.modules.update(
            colorama=colorama,
            Utils=utils,
            CommonClient=common,
            NetUtils=net,
        )
        return importlib.import_module("bridge_client")


bridge = _bridge_module()


def _connection_context():
    context = object.__new__(bridge.DoomEternalContext)
    context._launcher_connection_failure_reported = False
    context.disconnected_intentionally = False
    context.exit_event = SimpleNamespace(is_set=lambda: False, set=lambda: None)
    context.cancel_autoreconnect = lambda: False
    return context


def test_launcher_connection_loss_emits_once_and_stops_reconnect(monkeypatch):
    context = _connection_context()
    context.exit_event = SimpleNamespace(
        is_set=lambda: context.exit_set,
        set=lambda: setattr(context, "exit_set", True),
    )
    context.exit_set = False
    cancelled = []
    context.cancel_autoreconnect = lambda: cancelled.append(True)
    events = []
    monkeypatch.setattr(bridge, "LAUNCHER_EVENTS_ENABLED", True)
    monkeypatch.setattr(bridge, "emit_launcher_event", lambda event_type, **payload: events.append({"type": event_type, **payload}))

    with patch.object(bridge.CommonContext, "handle_connection_loss") as delegated:
        context.handle_connection_loss("Failed to connect to the multiworld server")
        context.handle_connection_loss("Failed to connect to the multiworld server")

    assert delegated.call_count == 2
    assert events == [{
        "type": "error",
        "code": "connection_failed",
        "message": "Failed to connect to the multiworld server",
    }]
    assert context.disconnected_intentionally
    assert context.exit_event.is_set()
    assert cancelled == [True]


def test_launcher_unexpected_socket_close_ends_attempt(monkeypatch):
    context = object.__new__(bridge.DoomEternalContext)
    context.server = SimpleNamespace(socket=object())
    context.disconnected_intentionally = False
    context.exit_event = asyncio.Event()
    context._launcher_connection_failure_reported = False
    context.cancel_autoreconnect = lambda: False
    events = []
    monkeypatch.setattr(bridge, "LAUNCHER_EVENTS_ENABLED", True)
    monkeypatch.setattr(
        bridge,
        "emit_launcher_event",
        lambda kind, **payload: events.append({"type": kind, **payload}),
    )

    with patch.object(
        bridge.CommonContext, "connection_closed", new=AsyncMock(), create=True
    ) as delegated:
        asyncio.run(context.connection_closed())

    delegated.assert_awaited_once()
    assert events == [
        {
            "type": "error",
            "code": "connection_failed",
            "message": "Disconnected from the Archipelago server",
        }
    ]
    assert context.disconnected_intentionally
    assert context.exit_event.is_set()


def test_cli_connection_loss_only_delegates(monkeypatch):
    context = _connection_context()
    monkeypatch.setattr(bridge, "LAUNCHER_EVENTS_ENABLED", False)

    with patch.object(bridge.CommonContext, "handle_connection_loss") as delegated:
        context.handle_connection_loss("Failed to connect to the multiworld server")

    delegated.assert_called_once_with("Failed to connect to the multiworld server")
    assert not context.disconnected_intentionally
    assert not context.exit_event.is_set()
    assert not context._launcher_connection_failure_reported


def test_exultia_active_map_marker_uses_canonical_runtime_projection(tmp_path):
    marker = tmp_path / "ap_active_map_e1m2_war_0.txt"
    marker.write_text(
        "AP_ACTIVE_MAP_V1 map_key=e1m2_war "
        "runtime_map=game/sp/e1m2_battle/e1m2_battle "
        "marker=AP_MAP_START_E1M2_WAR\n",
        encoding="utf-8",
    )

    parsed = bridge.parse_active_map_marker(str(marker), 123)

    assert bridge.KNOWN_CATALOG_MAPS["e1m2_war"] == "game/sp/e1m2_battle/e1m2_battle"
    assert parsed == {
        "map_key": "e1m2_war",
        "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
        "marker": "AP_MAP_START_E1M2_WAR",
        "mtime_ns": 123,
        "path": str(marker),
    }


@pytest.mark.parametrize(
    "map_key,runtime_map,marker_name",
    [
        ("e1m2_war", "game/sp/e1m2_war/e1m2_war", "AP_MAP_START_E1M2_WAR"),
        ("unknown", "game/sp/e1m2_battle/e1m2_battle", "AP_MAP_START_UNKNOWN"),
        ("e1m2_war", "game/sp/e1m1_intro/e1m1_intro", "AP_MAP_START_E1M2_WAR"),
    ],
)
def test_active_map_marker_rejects_stale_unknown_and_mismatched_identity(
    tmp_path, map_key, runtime_map, marker_name
):
    marker = tmp_path / "ap_active_map.txt"
    marker.write_text(
        f"AP_ACTIVE_MAP_V1 map_key={map_key} runtime_map={runtime_map} "
        f"marker={marker_name}\n",
        encoding="utf-8",
    )

    assert bridge.parse_active_map_marker(str(marker), 123) is None


@pytest.mark.parametrize(
    "active_maps",
    [
        {},
        {"": "game/sp/e1m1_intro/e1m1_intro"},
        {"e1m1_intro": ""},
        {" e1m1_intro": "game/sp/e1m1_intro/e1m1_intro"},
    ],
)
def test_malformed_canonical_map_projection_fails_closed(active_maps):
    with pytest.raises(ValueError):
        bridge._validated_catalog_maps(active_maps)


def test_automatic_resync_noop_dedupes_signature_with_periodic_repeat():
    context = object.__new__(bridge.DoomEternalContext)
    context._automatic_resync_noop_signature = None
    context._automatic_resync_noop_logged_at = None

    with patch.object(bridge.logger, "info") as log_info, patch.object(
        bridge.time,
        "monotonic",
        side_effect=[0.0, 299.0, 300.0, 301.0, 302.0, 303.0, 304.0],
    ):
        assert context.log_automatic_resync_noop("level_ready", "no_commands", 7, "abc")
        assert not context.log_automatic_resync_noop("level_ready", "no_commands", 7, "abc")
        assert context.log_automatic_resync_noop("level_ready", "no_commands", 7, "abc")
        assert context.log_automatic_resync_noop("level_ready", "blocked", 7, "abc")
        assert context.log_automatic_resync_noop("level_ready", "blocked", 8, "abc")
        assert context.log_automatic_resync_noop("reconnect", "blocked", 8, "abc")
        assert context.log_automatic_resync_noop("reconnect", "blocked", 8, "def")

    assert log_info.call_count == 6


def test_rune_diagnostic_compacts_owned_native_slots_save_and_epoch():
    perk = "perk/player/runes/savagery"
    native = RuneNativeState(
        frozenset({perk}),
        frozenset({perk}),
        frozenset(),
        (perk, None, None),
        True,
        "GAME-AUTOSAVE2",
        77,
    )
    plan = SimpleNamespace(
        entries=(SimpleNamespace(perk=perk),),
        status="blocked_native_writer_unproven",
        noops=(),
        repairs=(SimpleNamespace(),),
    )
    context = object.__new__(bridge.DoomEternalContext)
    context.rune_native_state = lambda: (native, None)
    context.compile_owned_rune_plan = lambda: (plan, None)

    lines = context.rune_diagnostic_lines()

    assert lines[0] == (
        f"AP-owned Rune perks: {perk} | available: {perk} | active: {perk} | registered: -"
    )
    assert lines[1] == (
        f"Rune slots: 0={perk} | 1=- | 2=- | page=True | "
        "active_save=GAME-AUTOSAVE2 | epoch=77"
    )
    assert "repair_candidates=1" in lines[2]


def test_rune_diagnostic_retains_exact_unavailable_reason():
    context = object.__new__(bridge.DoomEternalContext)
    context.rune_native_state = lambda: (None, "authoritative active-save proof required")

    assert context.rune_diagnostic_lines() == [
        "Rune diagnostic unavailable: authoritative active-save proof required"
    ]
