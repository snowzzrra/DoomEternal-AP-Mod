from __future__ import annotations

import ctypes
import logging
import subprocess

import observer_lifecycle
from observer_lifecycle import (
    ERROR_NO_MORE_FILES,
    INVALID_HANDLE_VALUE,
    OBSERVER_CONTRACTS,
    RuntimeObservationLease,
    SaveObserverBaselineStore,
    doom_process_running,
)


def _binding(session: str = "seed") -> str:
    return SaveObserverBaselineStore.binding_key(
        session_identity=session,
        team=1,
        slot=2,
        doom_save_slot="GAME-AUTOSAVE2",
        registry_revision="revision",
    )


class _FakeKernel32:
    def __init__(
        self,
        process_names=(),
        *,
        snapshot=123,
        first_succeeds=True,
        iteration_error=ERROR_NO_MORE_FILES,
    ):
        self.process_names = list(process_names)
        self.snapshot = snapshot
        self.first_succeeds = first_succeeds
        self.iteration_error = iteration_error
        self.index = 0
        self.closed = []

    @staticmethod
    def _entry(pointer):
        return ctypes.cast(
            pointer, ctypes.POINTER(observer_lifecycle.PROCESSENTRY32W)
        ).contents

    def CreateToolhelp32Snapshot(self, _flags, _process_id):
        return self.snapshot

    def Process32FirstW(self, _snapshot, entry):
        if not self.first_succeeds or not self.process_names:
            return 0
        self.index = 0
        self._entry(entry).szExeFile = self.process_names[0]
        return 1

    def Process32NextW(self, _snapshot, entry):
        self.index += 1
        if self.index >= len(self.process_names):
            return 0
        self._entry(entry).szExeFile = self.process_names[self.index]
        return 1

    def CloseHandle(self, snapshot):
        self.closed.append(snapshot)
        return 1


def _use_windows_probe(monkeypatch, kernel32):
    monkeypatch.setattr(observer_lifecycle.os, "name", "nt")
    monkeypatch.setattr(observer_lifecycle, "_load_kernel32", lambda: kernel32)
    monkeypatch.setattr(
        observer_lifecycle.ctypes,
        "get_last_error",
        lambda: kernel32.iteration_error,
        raising=False,
    )


def test_windows_finds_doom_process(monkeypatch) -> None:
    kernel32 = _FakeKernel32(["other.exe", "DOOMEternalx64vk.exe"])
    _use_windows_probe(monkeypatch, kernel32)

    assert doom_process_running()
    assert kernel32.closed == [123]


def test_windows_match_is_case_insensitive(monkeypatch) -> None:
    kernel32 = _FakeKernel32(["other.exe", "DOOMEternalX64VK.EXE"])
    _use_windows_probe(monkeypatch, kernel32)

    assert doom_process_running()
    assert kernel32.closed == [123]


def test_windows_missing_process_returns_false_and_closes_handle(monkeypatch) -> None:
    kernel32 = _FakeKernel32(["other.exe"])
    _use_windows_probe(monkeypatch, kernel32)

    assert not doom_process_running()
    assert kernel32.closed == [123]


def test_windows_snapshot_failure_returns_false(monkeypatch) -> None:
    kernel32 = _FakeKernel32(snapshot=INVALID_HANDLE_VALUE)
    _use_windows_probe(monkeypatch, kernel32)

    assert not doom_process_running()
    assert kernel32.closed == []


def test_windows_iteration_failure_returns_false_and_closes_handle(
    monkeypatch,
) -> None:
    kernel32 = _FakeKernel32(["other.exe"], iteration_error=5)
    _use_windows_probe(monkeypatch, kernel32)

    assert not doom_process_running()
    assert kernel32.closed == [123]


def test_windows_first_failure_returns_false_and_closes_handle(monkeypatch) -> None:
    kernel32 = _FakeKernel32(first_succeeds=False)
    _use_windows_probe(monkeypatch, kernel32)

    assert not doom_process_running()
    assert kernel32.closed == [123]


def test_windows_polling_never_calls_subprocess(monkeypatch) -> None:
    kernel32 = _FakeKernel32(["other.exe"])
    _use_windows_probe(monkeypatch, kernel32)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess must not be called")
        ),
    )

    assert not doom_process_running()
    assert not doom_process_running()
    assert kernel32.closed == [123, 123]


def test_linux_process_scan_is_unchanged(monkeypatch, tmp_path) -> None:
    process = tmp_path / "42"
    process.mkdir()
    (process / "comm").write_text("DOOMEternalx64vk.exe\n", encoding="utf-8")
    monkeypatch.setattr(observer_lifecycle.os, "name", "posix")
    monkeypatch.setattr(observer_lifecycle, "Path", lambda _path: tmp_path)
    monkeypatch.setattr(
        observer_lifecycle,
        "_load_kernel32",
        lambda: (_ for _ in ()).throw(AssertionError("Win32 probe used on Linux")),
    )

    assert doom_process_running()


def test_windows_api_warning_is_emitted_once(monkeypatch, caplog) -> None:
    kernel32 = _FakeKernel32(snapshot=INVALID_HANDLE_VALUE)
    _use_windows_probe(monkeypatch, kernel32)
    monkeypatch.setattr(
        observer_lifecycle, "_windows_process_probe_warning_emitted", False
    )

    with caplog.at_level(logging.WARNING):
        assert not doom_process_running()
        assert not doom_process_running()

    messages = [
        record.message
        for record in caplog.records
        if "Windows process detection failed" in record.message
    ]
    assert len(messages) == 1


def test_every_save_observer_declares_the_shared_evidence_policy() -> None:
    policies = OBSERVER_CONTRACTS["policies"]
    for observer in OBSERVER_CONTRACTS["observers"].values():
        policy = policies[observer["policy"]]
        if observer["policy"] == "save_based":
            assert policy == {
                "requires_live_runtime": True,
                "requires_authoritative_save": True,
                "baseline_policy": "first_binding_snapshot",
                "binding_scope": "ap_session_team_slot_doom_save_registry",
                "completion_policy": "false_to_true_edge",
            }


def test_game_closed_blocks_live_runtime_lease() -> None:
    lease = RuntimeObservationLease(process_probe=lambda: False, started_ns=100)
    lease.observe_gameplay_loaded(110)
    assert lease.validate(
        evidence_mtime_ns=120,
        evidence_state="gameplay",
        evidence_map="map",
        current_map="map",
        details_map="map",
    ) == (False, "game_not_running")


def test_live_lease_requires_fresh_gameplay_and_coherent_map() -> None:
    lease = RuntimeObservationLease(process_probe=lambda: True, started_ns=100)
    assert lease.validate(
        evidence_mtime_ns=120,
        evidence_state="gameplay",
        evidence_map="map",
        current_map="map",
        details_map="map",
    ) == (False, "gameplay_not_loaded")
    lease.observe_gameplay_loaded(110)
    assert lease.validate(
        evidence_mtime_ns=120,
        evidence_state="gameplay",
        evidence_map="map",
        current_map="map",
        details_map="map",
    ) == (True, "live")


def test_old_save_becomes_new_session_baseline() -> None:
    state: dict = {}
    store = SaveObserverBaselineStore(state)
    pending, created, new_edges = store.observe(
        binding_key=_binding("new-seed"),
        observer_key="mission_challenges",
        records={"old_complete": True, "future": False},
        acknowledged_records=set(),
    )
    assert created
    assert pending == set()
    assert new_edges == set()
    observer = state["observer_baselines"][_binding("new-seed")]["observers"]["mission_challenges"]
    assert observer["baseline_preexisting"] == ["old_complete"]


def test_reconnect_same_session_does_not_rebaseline_and_later_edge_is_pending() -> None:
    state: dict = {}
    store = SaveObserverBaselineStore(state)
    store.observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": False},
        acknowledged_records=set(),
    )
    pending, created, new_edges = SaveObserverBaselineStore(state).observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": True},
        acknowledged_records=set(),
    )
    assert not created
    assert pending == {"record"}
    assert new_edges == {"record"}
    pending, _, new_edges = SaveObserverBaselineStore(state).observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": True},
        acknowledged_records=set(),
    )
    assert pending == {"record"}
    assert new_edges == set()
    pending, _, _ = SaveObserverBaselineStore(state).observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": True},
        acknowledged_records={"record"},
    )
    assert pending == set()
