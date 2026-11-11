"""Read-only presentation of room setup events."""

from dataclasses import dataclass
from enum import Enum, auto
from time import monotonic

from .ui.strings import text


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
    step: text("launcher.setup", "step_" + step.name.lower()) for step in SetupStep
}
ACTION_TEXT = {
    action: text("launcher.setup", "action_" + action.name.lower()) for action in SetupAction
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
    detail: str = ""

    @property
    def activity_text(self) -> str:
        step = STEP_TEXT[self.step].rstrip("….") + "…"
        return step + "\n" + self.detail if self.detail else step

    @property
    def text(self) -> str:
        minutes, seconds = divmod(self.elapsed_seconds, 60)
        lines = [text("launcher.setup", "elapsed", step=STEP_TEXT[self.step], minutes=minutes, seconds=f"{seconds:02d}")]
        if self.detail:
            lines.append(self.detail)
        if self.last_completed is not None:
            lines.append(text("launcher.setup", "last_completed", step=STEP_TEXT[self.last_completed]))
        if self.long_wait:
            lines.append(text("launcher.setup", "long_wait"))
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
        self.build_progress = None
        self.build_started = self.started
        self.build_detail = ""

    def activate(self, generation, room) -> None:
        scope = (generation, room)
        if scope != self.scope:
            self.scope = scope
            self.step = None
            self.last_completed = None
            self.build_progress = None
            self.build_detail = ""

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
        if kind == "mod_building":
            self._observe_build(event)

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
            self.build_progress = None
            self.build_detail = ""

    def _observe_build(self, event):
        stage = event.get("build_stage")
        done, total = event.get("build_done"), event.get("build_total")
        if stage not in {"reading", "enemies", "placements", "packing"}:
            return
        if type(done) is not int or type(total) is not int or not 0 <= done <= total:
            return
        if self.build_progress is None or stage != self.build_progress[0]:
            self.build_started = self.clock()
        self.build_progress = stage, done, total

    def _build_detail(self):
        if self.step is not SetupStep.BUILDING or self.build_progress is None:
            return ""
        if self.clock() - self.build_started >= 2:
            stage, done, total = self.build_progress
            values = {"done": done, "total": total} if stage in {"enemies", "placements"} else {}
            self.build_detail = text("launcher.setup", "build_" + stage, **values)
        return self.build_detail

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
        return SetupProgressView(self.step, elapsed, self.last_completed, action, long_wait, self._build_detail())
