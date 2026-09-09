"""Per-load reconciliation admission and progress; lifecycle remains authoritative."""
from dataclasses import dataclass
from types import MappingProxyType

from doom_eap.contracts.command_publication import valid_materialization_epoch


@dataclass(frozen=True)
class LevelReadyJob:
    epoch: str
    generation: int
    source_path: object
    settle_inventory: bool


@dataclass(frozen=True)
class LevelReadyAdmission:
    job: LevelReadyJob | None = None
    block: str | None = None


class LevelReady:
    def __init__(self, logger):
        self._logger = logger
        self._generation = 0
        self._pending = {}
        self._completed = set()
        self._settled = set()
        self._active = {}
        self._pending_signature = None

    @property
    def pending(self):
        return MappingProxyType(dict(self._pending))

    @property
    def completed(self):
        return frozenset(self._completed)

    def invalidate(self):
        # Preserve original process-local pending/completed/settled lifetimes.
        self._generation += 1
        self._active.clear()

    def queue(self, epoch, path):
        if valid_materialization_epoch(epoch) and epoch not in self._completed:
            self._pending.setdefault(epoch, path)

    def can_start(self, epoch):
        return isinstance(epoch, str) and epoch in self._pending and epoch not in self._active

    def observe_readiness(self, epoch, runtime_ready, evidence_state):
        if runtime_ready:
            return True
        reason = "native_gameplay_unsafe" if evidence_state == "gameplay" else "evidence_not_gameplay"
        signature = (epoch, reason, evidence_state)
        if signature != self._pending_signature:
            self._pending_signature = signature
            self._logger.info("[RPC] LEVEL_READY_PENDING epoch=%s reason=%s state=%s",
                              epoch, reason, evidence_state or "unavailable")
            self._logger.info("[MAP] RUNTIME_EFFECTS_PENDING reason=%s state=%s",
                              reason, evidence_state or "unavailable")
        return False

    def start(self, epoch, context, use_dlc, dlc_evidence, fallback_path):
        if use_dlc and context is not None and context.campaign != "Base" and dlc_evidence.blocks_enabled:
            self._logger.info("[Context] LEVEL_READY_PENDING reason=dlc_missing evidence=%s", dlc_evidence.report())
            return LevelReadyAdmission(block=dlc_evidence.reason)
        job = LevelReadyJob(epoch, self._generation, self._pending.get(epoch) or fallback_path,
                            context is not None and context.campaign != "Base" and epoch not in self._settled)
        self._active[epoch] = job
        self._pending_signature = None
        self._logger.info("[RPC] LEVEL_READY_EXECUTE epoch=%s", epoch)
        return LevelReadyAdmission(job=job)

    def is_current(self, job):
        return job.generation == self._generation and self._active.get(job.epoch) is job

    def observe_resume(self, job, map_identity, runtime_ready, *, settled=False):
        if not self.is_current(job):
            return False
        marker = map_identity.cached_marker
        if not runtime_ready or marker is None or marker.get("gameplay_epoch") != job.epoch:
            reason = "inventory_settle_invalidated" if settled else "level_ready_invalidated"
            self._logger.info("[Context] LEVEL_READY_PENDING reason=%s epoch=%s", reason, job.epoch)
            return False
        if settled:
            self._settled.add(job.epoch)
        return True

    def complete(self, job):
        self._pending.pop(job.epoch, None)
        self._completed.add(job.epoch)

    def finish(self, job):
        if self.is_current(job):
            self._active.pop(job.epoch)
