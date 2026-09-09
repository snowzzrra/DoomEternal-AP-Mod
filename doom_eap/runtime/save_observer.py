"""Independent save-readiness state; no lifecycle mutation or file discovery."""

from dataclasses import replace
from collections.abc import Mapping
import re

from doom_eap.contracts.save_observation import (
    MissionSelectObservation, SaveCandidateDecision, SaveProofDecision,
    SaveReadinessSnapshot, SaveSelectionSnapshot,
)


class SaveObserver:
    def __init__(self, readiness=None, *, baselines=None):
        self._readiness = readiness if readiness is not None else SaveReadinessSnapshot()
        self._selection = SaveSelectionSnapshot()
        self._candidate_tokens = {}
        self._slot_observations = {}
        self._observation_slot = None
        self._duration_cache_key = None
        self._records = {}
        self._proof_revision = 0
        self._baselines = baselines if baselines is not None else SaveObserverBaselineStore({})
        self._mission_select = MissionSelectObservation()
        self._last_mission_select_epoch = None

    @property
    def mission_select(self):
        return self._mission_select

    def clear_mission_select(self):
        self._mission_select = MissionSelectObservation()

    def accept_mission_select(self, map_name, epoch):
        self._mission_select = MissionSelectObservation(map_name, epoch)
        changed = self._last_mission_select_epoch != epoch
        self._last_mission_select_epoch = epoch
        return changed

    def plan_proof(self, selected, *, proof_evidence_epoch, evidence_epoch):
        same_slot = self._selection.slot == selected.slot_directory and self._selection.path == str(selected.path)
        if not same_slot:
            if self._selection.native_evidence_epoch == proof_evidence_epoch and self._selection.slot is not None:
                return SaveProofDecision("reject", reason="unproven_epoch")
            return SaveProofDecision("activate", new_evidence=True)
        new_evidence = evidence_epoch is not None and self._selection.native_evidence_epoch != evidence_epoch
        return SaveProofDecision(
            "refresh", new_evidence=new_evidence,
            reset_observation_slot=new_evidence and self._selection.token != selected.mtime_ns,
        )

    @staticmethod
    def requires_mission_select(active_map, continue_target_map, *, cross_campaign_details_lag, challenge_maps):
        if cross_campaign_details_lag:
            continue_target_map = active_map
        return bool(active_map in challenge_maps and continue_target_map and continue_target_map != active_map)

    def bind_baselines(self, store):
        self._baselines = store

    def capture_observation(self):
        return self._proof_revision, self._selection, self._readiness, self._mission_select

    def observation_is_current(self, token):
        return token == self.capture_observation()

    def duration_is_current(self, selected):
        return selected.cache_key == self._duration_cache_key

    def accept_duration(self, selected):
        self._duration_cache_key = selected.cache_key

    def observe_record(self, kind, slot, unlockable, record):
        """Remember a decoded record and report whether its diagnostic changed."""
        key = (kind, slot, unlockable)
        if self._records.get(key) == record:
            return False
        self._records[key] = record
        return True

    def observe_edges(self, *, session_identity, team, slot, registry_revision,
                      doom_save_slot, observer_key, records, acknowledged_records):
        """Select the durable baseline independently of feature publication."""
        binding_key = self._baselines.binding_key(
            session_identity=session_identity, team=team, slot=slot,
            doom_save_slot=doom_save_slot, registry_revision=registry_revision,
        )
        return self._baselines.observe(
            binding_key=binding_key, observer_key=observer_key,
            records=records, acknowledged_records=acknowledged_records,
        )

    @property
    def observation_slot(self):
        return self._observation_slot

    @property
    def observation_document(self):
        """Live persistence projection; slot entries are mutated only by this owner."""
        return self._slot_observations

    def restore_slot_observations(self, raw):
        if not isinstance(raw, dict):
            raw = {}
        self._slot_observations = {
            slot: state for slot, state in raw.items()
            if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", str(slot))
            and isinstance(state, dict)
        }
        self._observation_slot = None
        return self._slot_observations

    def select_observation_slot(self, slot):
        if self._observation_slot == slot:
            return False
        self._observation_slot = slot
        state = self._slot_observations.setdefault(slot, {})
        state.pop("weapon_masteries", None)
        state.pop("mission_challenges", None)
        return True

    def invalidate_observation_slot(self, slot):
        self._slot_observations[slot] = {}
        self._observation_slot = None

    def observe_candidate(self, selected):
        token = (str(selected.path), selected.mtime_ns)
        if self._candidate_tokens.get(selected.slot_directory) == token:
            return False
        self._candidate_tokens[selected.slot_directory] = token
        return True

    @property
    def selection(self):
        return self._selection

    def update_selection(self, **changes):
        self._selection = replace(self._selection, **changes)

    @property
    def readiness(self):
        return self._readiness

    def invalidate_proof(self):
        self._proof_revision += 1
        self._readiness = SaveReadinessSnapshot()

    def activate_slot(self, slot):
        self._readiness = replace(self._readiness, authoritative=True, slot=slot)

    def accept_proof(self, slot, evidence_epoch, load_epoch):
        self._readiness = SaveReadinessSnapshot(True, slot, evidence_epoch, load_epoch, False)

    def set_frozen(self, frozen):
        self._readiness = replace(self._readiness, frozen=frozen)

    def observe_expected_family(self, expected_prefix):
        mismatch = bool(
            expected_prefix and self._selection.slot
            and not self._selection.slot.startswith(expected_prefix)
        )
        prior_epoch = self._readiness.evidence_epoch
        if mismatch:
            self.invalidate_proof()
            self.update_selection(native_evidence_epoch=None)
        return mismatch, prior_epoch

    def select_candidate(self, *, evidence, marker_present, evidence_slot,
                         expected_prefix, newest, active, process_running,
                         load_epoch, mission_select_map, provisional_family_switch):
        """Resolve observation admission from discovered facts, without IO/effects."""
        newer_unproven = bool(
            newest and active and newest.slot_directory != active.slot_directory
            and newest.mtime_ns > active.mtime_ns
        )

        def reject(reason):
            self.set_frozen(True)
            return SaveCandidateDecision("reject", reason=reason)

        def continue_active(reason):
            proof = self._readiness
            if (
                mission_select_map or not proof.authoritative
                or proof.slot != self._selection.slot or proof.load_epoch != load_epoch
                or active is None or newer_unproven
            ):
                return reject(reason)
            reset = self._selection.token != active.mtime_ns
            # Coordinator applies any feature-view reset before committing the token.
            return SaveCandidateDecision(
                "continue", continued=active, reset_observation_slot=reset,
            )

        if not process_running:
            self.invalidate_proof()
            return reject("game_not_running")
        if evidence is None and not marker_present:
            return continue_active("no_gameplay_evidence")
        if evidence and evidence.state != "gameplay":
            self.invalidate_proof()
            return reject("menu")
        if evidence and evidence.provisional and not marker_present and not provisional_family_switch:
            return continue_active("provisional")
        candidate_slot = newest.slot_directory if newest else None
        target = evidence_slot or (active.slot_directory if active else None) or candidate_slot
        if expected_prefix and target and not target.startswith(expected_prefix):
            target = candidate_slot
        if not target or not re.match(r"^(?:GAME|DLC[12]|HORDE)-AUTOSAVE[0-9]+$", target):
            return reject("invalid_evidence_slot")
        return SaveCandidateDecision("read_details", target_slot=target)

    def continue_selection(self, selected):
        self.update_selection(path=str(selected.path), token=selected.mtime_ns)
        self.set_frozen(False)

    @staticmethod
    def permits_provisional_family_switch(
        evidence, *, marker_absent, context_known, expected_prefix,
        active_family_mismatch, newest, active, evidence_epoch, prior_evidence_epoch,
    ):
        return bool(
            evidence
            and evidence.state == "gameplay"
            and evidence.provisional
            and evidence.native_safe
            and marker_absent
            and context_known
            and expected_prefix
            and active_family_mismatch
            and newest
            and newest.slot_directory.startswith(expected_prefix)
            and active
            and newest.mtime_ns > active.mtime_ns
            and evidence_epoch is not None
            and evidence_epoch != prior_evidence_epoch
        )

    def has_authoritative_proof(self, *, lease_present, gameplay_loaded_ns):
        proof = self._readiness
        if proof.frozen:
            return False
        if proof.authoritative is False:
            return False
        if lease_present and proof.load_epoch != gameplay_loaded_ns:
            return False
        if self._selection.slot is not None:
            return bool(proof.slot == self._selection.slot)
        return True


class SaveObserverBaselineStore:
    """Persistent false→true edges bound to AP identity and Doom save slot."""

    def __init__(self, state: dict):
        self.state = state.setdefault("observer_baselines", {})

    @staticmethod
    def binding_key(
        *,
        session_identity: str,
        team: int,
        slot: int,
        doom_save_slot: str,
        registry_revision: str,
    ) -> str:
        return "|".join(
            (
                session_identity,
                str(team),
                str(slot),
                doom_save_slot,
                registry_revision,
            )
        )

    def observe(
        self,
        *,
        binding_key: str,
        observer_key: str,
        records: Mapping[str, bool],
        acknowledged_records: set[str],
    ) -> tuple[set[str], bool, set[str]]:
        binding = self.state.get(binding_key)
        created = binding is None
        if binding is None:
            binding = self.state[binding_key] = {
                "observers": {},
                "registry_revision": binding_key.rsplit("|", 1)[-1],
            }
        observers = binding["observers"]
        observer = observers.get(observer_key)
        if observer is None:
            observer = observers[observer_key] = {
                "baseline_preexisting": sorted(
                    key for key, complete in records.items() if complete
                ),
                "last_observed": {
                    key: bool(complete) for key, complete in records.items()
                },
                "pending_edges": [],
            }
            return set(), True, set()

        previous = observer.setdefault("last_observed", {})
        pending = set(observer.setdefault("pending_edges", []))
        pending.difference_update(acknowledged_records)
        new_edges: set[str] = set()
        for key, complete in records.items():
            current = bool(complete)
            if key not in previous:
                previous[key] = current
                continue
            if current and not bool(previous[key]):
                pending.add(key)
                new_edges.add(key)
            previous[key] = current
        observer["pending_edges"] = sorted(pending)
        return pending, created, new_edges
