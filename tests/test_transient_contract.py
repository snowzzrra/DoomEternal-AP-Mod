import hashlib
import logging
from types import SimpleNamespace

from doom_eap.runtime import transient_effects as domain
from doom_eap.runtime import transient_files as adapter
from doom_eap.runtime.command_spool import CommandSpool


def test_effect_duration_composition_scope_and_reset_use_explicit_runtime_facts(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(domain, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    emitted = []
    spool = CommandSpool(tmp_path / "ap_queue", arm_rpc=lambda *_: None,
                         log_delivery=lambda *_, **__: None, logger=logging.getLogger("transient"))

    def send(command, **fields):
        emitted.append((command, fields))
        return spool.publish(command, **fields).accepted

    owner = domain.TransientEffectManager(adapter.TransientPublication(tmp_path, send), process_id=42)
    runtime = domain.TransientRuntime("room-A", ("100", "2"), True)
    assert owner.apply_receipt(7770156, runtime) == (True, "damage_boost", False)
    namespace = hashlib.sha256(b"room-A").hexdigest()[:16]
    expected = "effectscope-" + hashlib.sha256(f"room-A|{namespace}|42|100|2|0".encode()).hexdigest()[:16]
    assert (tmp_path / "ap_queue/active_transient_scope").read_text().strip() == expected
    assert emitted[-3][0] == "g_damageScaleAllToAI 1.50"
    assert owner.apply_receipt(7770159, runtime)[0]
    assert emitted[-3][0] == "g_damageScaleAllToAI 1.05"
    clock[0] = 5
    assert owner.apply_receipt(7770156, runtime)[0]
    clock[0] = 13
    assert owner.tick(runtime)
    assert emitted[-3][0] == "g_damageScaleAllToAI 1.50"
    count = len(emitted)
    clock[0] = 39
    assert owner.tick(runtime) and len(emitted) == count
    clock[0] = 41
    assert owner.tick(runtime)
    assert emitted[-3][0] == "g_damageScaleAllToAI 1.00"
    assert all(fields["execution_class"] == "TRANSIENT_EFFECT" for _, fields in emitted)
    assert all(fields["state_key"] == "room-A" for _, fields in emitted)
    owner.reset("room rebind", domain.TransientRuntime("room-B", ("100", "3"), True))
    assert emitted[-1][1]["room_scoped"] is False
    assert emitted[-1][1]["transient_scope"] != expected
    assert owner.apply_receipt(7770001, runtime) == (False, "not a transient effect", False)
    assert owner.apply_receipt(7770158, domain.TransientRuntime("room-B", None, False))[0] is False


def test_native_baseline_adapter_preserves_pid_freshness_and_inclusive_deadline(tmp_path, monkeypatch):
    clock = [1.5]
    monkeypatch.setattr(adapter, "time", SimpleNamespace(time=lambda: clock[0]))
    (tmp_path / "ap_effect_baseline.state").write_text(
        "state=ready\npid=100\nattachment_epoch=2\ntimestamp_ms=1000\nfreshness_ms=500\n", encoding="ascii")
    health = tmp_path / "ap_rpc_health.state"
    health.write_text("state=ready\npid=100\n", encoding="ascii")
    assert adapter.read_transient_runtime(tmp_path, "room", True) == domain.TransientRuntime("room", ("100", "2"), True)
    clock[0] = 1.501
    assert not adapter.read_transient_runtime(tmp_path, "room", True).ready
    clock[0] = 1.5
    health.write_text("state=ready\npid=101\n", encoding="ascii")
    assert adapter.read_transient_runtime(tmp_path, "room", True).binding is None
    assert not adapter.read_transient_runtime(tmp_path, "room", True).ready
