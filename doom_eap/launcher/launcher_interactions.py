"""Pending launcher questions belong to the application, with explicit job scope."""

import threading
import time
import uuid
from dataclasses import dataclass, field

from doom_eap.launcher.ui.strings import text as player_text

from .launcher_workers import LauncherJob


@dataclass
class _Pending:
    kind: str
    event: threading.Event = field(default_factory=threading.Event)
    answer: bool | None = None


class LauncherInteractions:
    def __init__(self, emit):
        self._emit = emit
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def for_job(self, job: LauncherJob):
        return ScopedInteractions(self, job)

    def request(self, kind, payload, job=None):
        request_id = uuid.uuid4().hex
        pending = _Pending(kind)
        with self._lock:
            self._pending[request_id] = pending
        try:
            payload = {**payload, "request_id": request_id}
            self._emit(kind, job.event(payload) if job is not None else payload)
            deadline = time.monotonic() + 300.0
            while not pending.event.wait(timeout=0.1):
                if job is not None:
                    job.check()
                if time.monotonic() >= deadline:
                    break
            if job is not None:
                job.check()
            return pending.answer is True
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def resolve(self, kind, request_id, accepted):
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is not None and pending.kind == kind:
                if pending.answer is None:
                    pending.answer = bool(accepted)
                pending.event.set()

    def cancel_all(self):
        with self._lock:
            for pending in self._pending.values():
                if pending.answer is None:
                    pending.answer = False
                pending.event.set()

    def consent(self, spec, job=None):
        if spec.name == "Meathook":
            purpose, source = player_text('launcher.interactions', 'game_link_runtime_library'), player_text('launcher.interactions', 'github_brongo')
        elif spec.name == "EternalModInjector":
            purpose, source = player_text('launcher.interactions', 'windows_mod_installation_tools'), player_text('launcher.interactions', 'gamebanana_doom_2016_modding_community')
        else:
            purpose, source = player_text('launcher.interactions', 'mod_installation_tool'), "GitHub"
        return self.request("dependency_consent_required", {
            "name": spec.name, "version": spec.version, "url": spec.url, "sha256": spec.sha256,
            "purpose": purpose, "source": source,
        }, job)

    def confirmation(self, job=None):
        return self.request("installation_confirmation_required", {
            "message": player_text('launcher.interactions', 'did_eternalmodinjector_install_the_mod_without_errors'),
        }, job)

    def uninstall_confirmation(self, job=None):
        return self.request("uninstall_confirmation_required", {
            "operation": "uninstall",
            "message": player_text('launcher.ui', 'did_eternalmodinjector_remove_this_room_s_mod_without_errors'),
        }, job)


@dataclass(frozen=True)
class ScopedInteractions:
    """A real adapter capability: every question is bound to this job."""
    owner: LauncherInteractions
    job: LauncherJob

    def consent(self, spec):
        return self.owner.consent(spec, self.job)

    def confirmation(self):
        return self.owner.confirmation(self.job)

    def uninstall_confirmation(self):
        return self.owner.uninstall_confirmation(self.job)
