"""Read-only presentation of room setup events."""

from dataclasses import dataclass
from enum import Enum, auto
from time import monotonic


class SetupStep(Enum):
    CHECKING = auto()
    PREPARING = auto()
    CONNECTION = auto()
    BUILDING = auto()
    STAGING = auto()
    INSTALLING = auto()
    INSTALLER_INPUT = auto()
    CONFIRMING = auto()
    SAVE = auto()


class SetupAction(Enum):
    WAIT = auto()
    FINISH_INSTALL = auto()
    CONFIRM_INSTALL = auto()
    DIAGNOSE = auto()


STEP_TEXT = {
    SetupStep.CHECKING: "Checking the room mod",
    SetupStep.PREPARING: "Preparing the room mod",
    SetupStep.CONNECTION: "Preparing game connection files",
    SetupStep.BUILDING: "Building the room mod",
    SetupStep.STAGING: "Copying the room mod",
    SetupStep.INSTALLING: "Installing the room mod",
    SetupStep.INSTALLER_INPUT: "Waiting for installation in EternalModInjector",
    SetupStep.CONFIRMING: "Waiting for installation confirmation",
    SetupStep.SAVE: "Preparing the save",
}
ACTION_TEXT = {
    SetupAction.WAIT: "Keep the launcher open.",
    SetupAction.FINISH_INSTALL: "Finish installation in EternalModInjector, then close its window. Keep this launcher open.",
    SetupAction.CONFIRM_INSTALL: "Check EternalModInjector, then answer the installation confirmation.",
    SetupAction.DIAGNOSE: "Open Help and select Check Setup. Keep the launcher open.",
}
EVENT_STEPS = {
    "connected": SetupStep.CHECKING,
    "setup_started": SetupStep.PREPARING,
    "core_runtime_selected": SetupStep.PREPARING,
    "mod_building": SetupStep.BUILDING,
    "runtime_config_ready": SetupStep.STAGING,
    "mod_staged": SetupStep.PREPARING,
    "installation_confirmation_required": SetupStep.CONFIRMING,
}
COMPLETED_STEPS = {
    "room_validated": SetupStep.CHECKING,
    "core_runtime_selected": SetupStep.CONNECTION,
    "runtime_config_ready": SetupStep.BUILDING,
    "mod_staged": SetupStep.STAGING,
}


@dataclass(frozen=True)
class SetupProgressView:
    step: SetupStep
    elapsed_seconds: int
    last_completed: SetupStep | None
    next_action: SetupAction
    long_wait: bool

    @property
    def text(self) -> str:
        minutes, seconds = divmod(self.elapsed_seconds, 60)
        lines = [f"{STEP_TEXT[self.step]}. Elapsed: {minutes}:{seconds:02d}."]
        if self.last_completed is not None:
            lines.append(f"Last completed step: {STEP_TEXT[self.last_completed]}.")
        if self.long_wait:
            lines.append("This step has been waiting a long time.")
        lines.append(ACTION_TEXT[self.next_action])
        return "\n".join(lines)


class SetupProgress:
    def __init__(self, *, windows: bool, installer_wait_seconds: int, clock=monotonic):
        self.windows = windows
        self.installer_wait_seconds = installer_wait_seconds
        self.clock = clock
        self.scope = None
        self.step = None
        self.last_completed = None
        self.started = clock()

    def activate(self, generation, room) -> None:
        scope = (generation, room)
        if scope != self.scope:
            self.scope = scope
            self.step = None
            self.last_completed = None

    def observe(self, event) -> None:
        scope = (event.get("launcher_job_generation"), event.get("setup_progress_room"))
        if self.scope is None or scope != self.scope or event.get("operation") == "uninstall":
            return
        kind = event.get("type")
        if kind in {"setup_failed", "error", "disconnected", "manual_install_required", "setup_ready"}:
            self.step = None
            return
        if kind == "setup_started":
            self.step = None
            self.last_completed = None
        completed = COMPLETED_STEPS.get(kind)
        if completed is self.step or kind in {"room_validated", "core_runtime_selected", "mod_staged"}:
            self.last_completed = completed
        self._advance(event)

    def _advance(self, event) -> None:
        kind = event.get("type")
        step = self._event_step(event)
        if kind in {"injector_finished", "manager_finished"}:
            if event.get("state") != "applied":
                self.step = None
                return
            self.last_completed = self.step
            step = SetupStep.SAVE
        elif kind in {"ap_session_status", "integration_status"}:
            self._session(event.get("session", event))
        if step is not None and step is not self.step:
            self.step = step
            self.started = self.clock()

    def _event_step(self, event):
        kind = event.get("type")
        if kind in {"injector_started", "manager_started"}:
            return SetupStep.INSTALLER_INPUT if self.windows else SetupStep.INSTALLING
        if kind == "core_update_status" and event.get("state") in {"checking", "downloading"}:
            return SetupStep.CONNECTION
        if kind == "runtime_config_ready" and self.step is not SetupStep.BUILDING:
            return SetupStep.SAVE
        return event.get("setup_progress_step", EVENT_STEPS.get(kind))

    def _session(self, session) -> None:
        if self.step is SetupStep.SAVE and session.get("state") in {"prelaunch_ready", "admitted"}:
            self.last_completed = SetupStep.SAVE
            self.step = None

    def present(self) -> SetupProgressView | None:
        if self.step is None:
            return None
        elapsed = max(0, int(self.clock() - self.started))
        # Save preparation uses the supervisor's 300-second request limit.
        threshold = {SetupStep.SAVE: 300, SetupStep.INSTALLING: self.installer_wait_seconds}.get(self.step)
        long_wait = threshold is not None and elapsed >= threshold
        action = {
            SetupStep.INSTALLER_INPUT: SetupAction.FINISH_INSTALL,
            SetupStep.CONFIRMING: SetupAction.CONFIRM_INSTALL,
        }.get(self.step, SetupAction.WAIT)
        if long_wait:
            action = SetupAction.DIAGNOSE
        return SetupProgressView(self.step, elapsed, self.last_completed, action, long_wait)
