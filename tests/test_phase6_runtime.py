from types import SimpleNamespace
from unittest.mock import Mock, patch

from doom_eap.runtime.lifecycle import RuntimeLifecycle
from tests.test_cross_campaign_save_authority import _create_test_context, _active_bridge_client


def test_firstthink_during_menu_binds_first_gameplay_without_false_reload():
    lifecycle = RuntimeLifecycle()
    hub = 'game/hub/hub'
    marker = lifecycle.authored_marker_proposal(
        {'runtime_map': hub, 'map_key': 'hub'}, 500, 450, 1, evidence_state='menu')
    lifecycle.accept_marker(marker, 1)
    evidence = SimpleNamespace(state='gameplay', map_name=hub, epoch=2, provisional=True)
    assert lifecycle.classify_native_load(evidence, {'hub': hub}).action == 'bind'
    lifecycle.bind_materialization_evidence(2)
    assert lifecycle.classify_native_load(evidence, {'hub': hub}) is None
    assert lifecycle.map_identity.cached_marker['gameplay_epoch'] == '500:500'
    evidence.epoch = 4
    assert lifecycle.classify_native_load(evidence, {'hub': hub}).action == 'suspend'


def test_fortress_uses_base_readiness_and_live_lease(tmp_path):
    from doom_eap.runtime.command_spool import CommandSpool
    spool = CommandSpool(tmp_path, arm_rpc=Mock(), log_delivery=Mock(), logger=Mock())
    context = _create_test_context()
    context.runtime_lifecycle.accept_marker({'runtime_map': 'game/hub/hub', 'gameplay_epoch': '500:500'}, 2)
    context.runtime_effects_ready = Mock(return_value=True)
    context.authored_map_runtime_ready = Mock(side_effect=AssertionError('DLC-only predicate'))
    context._active_materialization_lease = Mock(return_value='500:500')
    module = _active_bridge_client()
    with patch.object(module, 'read_gameplay_save_evidence', return_value=object()), \
         patch.object(module, 'send_command', side_effect=spool.publish) as send:
        context.synchronize_fortress_phase({'fortress_phase': 0})
        assert send.call_count == 1
        assert send.call_args.args[0] == 'ai_ScriptCmdEnt ap_fortress_phase_0 activate'
        assert send.call_args.kwargs['materialization_lease'] == '500:500'
        files = list(tmp_path.glob('*.cmd'))
        assert len(files) == 1
        assert ':' not in files[0].name
        assert '500:500' in files[0].read_text()
        assert 'ai_ScriptCmdEnt ap_fortress_phase_0 activate' in files[0].read_text()
        context.synchronize_fortress_phase({'fortress_phase': 0})
        assert send.call_count == 1
        context._active_materialization_lease.return_value = None
        context.synchronize_fortress_phase({'fortress_phase': 1})
        assert send.call_count == 1
        context._active_materialization_lease.return_value = '600:600'
        context.runtime_effects_ready.return_value = False
        context.synchronize_fortress_phase({'fortress_phase': 1})
        assert send.call_count == 1
