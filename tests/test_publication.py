import logging
import os
from pathlib import Path

import pytest

from doom_eap.runtime.command_spool import CommandSpool


@pytest.fixture
def io_boundary(tmp_path):
    gate = []
    spool = CommandSpool(tmp_path / "queue", arm_rpc=lambda enabled: gate.append(enabled),
                         log_delivery=lambda *a, **k: None, logger=logging.getLogger(__name__))
    return tmp_path, spool, gate


@pytest.mark.parametrize("fields,headers", [
    ({"execution_class": "MAP_ENTITY_SAFE", "operation": "FAST_TRAVEL_UNLOCK",
      "materialization_lease": "1:2"},
     "AP_EXECUTION_CLASS_V1 MAP_ENTITY_SAFE\nAP_MAP_ENTITY_OPERATION_V1 FAST_TRAVEL_UNLOCK\nAP_MATERIALIZATION_LEASE_V1 1:2\n"),
    ({"execution_class": "TRANSIENT_EFFECT", "transient_scope": "epoch-a"},
     "AP_EXECUTION_CLASS_V1 TRANSIENT_EFFECT\nAP_TRANSIENT_SCOPE_V1 epoch-a\n"),
])
def test_execution_class_wire_format(io_boundary, fields, headers):
    root, spool, gate = io_boundary
    assert spool.publish("activate", coalesce_key="job", arm_rpc=False, room_scoped=False, **fields).accepted
    assert (root / "queue/job.cmd").read_bytes() == (headers + "activate\n").encode()
    assert gate == []


def test_processing_race_does_not_leave_replay(io_boundary, monkeypatch):
    root, spool, gate = io_boundary
    link = os.link

    def claim_during_publish(source, destination):
        link(source, destination)
        Path(destination).with_suffix(".processing").write_bytes(b"already claimed")

    monkeypatch.setattr(os, "link", claim_during_publish)
    assert spool.publish("give ammo", coalesce_key="job", room_scoped=False, already_queued_ok=True).accepted
    assert not (root / "queue/job.cmd").exists()
    assert (root / "queue/job.processing").read_bytes() == b"already claimed"
    assert gate == [True]


def test_fsync_failure_does_not_publish(io_boundary, monkeypatch):
    root, spool, gate = io_boundary

    def fail(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", fail)
    assert not spool.publish("give ammo", coalesce_key="job", room_scoped=False).accepted
    assert not (root / "queue/job.cmd").exists()
    assert gate == []
