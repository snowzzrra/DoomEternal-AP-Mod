"""Reduce launcher events into Play state and the current UI projection; no I/O."""

from dataclasses import dataclass, replace

from doom_eap.launcher.ui.strings import text as player_text

from ..events import (
    ActionId,
    ConnectionChanged,
    ConnectionState,
    DiagnosticsReady,
    DiscoveryChanged,
    ExternalActionRequired,
    InstallationChanged,
    InstallationState,
    LauncherEvent,
    NativeHealthChanged,
    ResyncProgress,
    RoomAccepted,
    SessionChanged,
    Tone,
)
from ..setup_progress import SetupAction, SetupProgress, SetupProgressView
from ..ui.strings import (
    checkpoint_blocked,
)


@dataclass(frozen=True)
class PlayModel:
    game_found: bool = False
    game_connection_ready: bool = False
    room_connected: bool = False
    installation: InstallationState = InstallationState.CHECKING
    session_prepared: bool = False
    game_confirmed: bool = False
    action: ActionId = ActionId.WAIT
    tone: Tone = Tone.NEUTRAL
    launch_option: str = ""
    progress: SetupProgressView | None = None
    recovery_message: str = ""
    new_save_available: bool = False

    @property
    def ready_to_play(self) -> bool:
        return (self.room_connected and self.installation is InstallationState.VERIFIED
                and self.game_connection_ready and self.session_prepared)


class PlayPresenter:
    def __init__(self, *, windows: bool, installer_wait_seconds: int, **progress_options):
        self.progress = SetupProgress(windows=windows, installer_wait_seconds=installer_wait_seconds, **progress_options)
        self.model = PlayModel()
        self.attempt = None

    def activate(self, generation, room, attempt=None) -> None:
        scope = (generation, room)
        if scope != self.progress.scope or attempt != self.attempt:
            self.model = PlayModel(game_found=self.model.game_found)
        self.attempt = attempt
        self.progress.activate(generation, room)

    def observe(self, event: LauncherEvent) -> dict | None:
        if not self.accepts(event):
            return None
        generation, room = self.progress.scope or (None, None)
        projection = event.projection()
        if event.kind == "ap_save_inspection" and projection["inspection"]["status"] == "save_interrupted":
            projection["message"] = player_text('launcher.save', 'interrupted')
        session = event.session if isinstance(event, NativeHealthChanged) else event
        if isinstance(session, SessionChanged):
            projection["offer_new_save"] = session.new_save_available and not self.model.new_save_available
        if isinstance(event, RoomAccepted):
            projection.update(launcher_job_generation=generation, setup_progress_room=room)
            self.model = replace(self.model, game_found=True, room_connected=True)
        self.progress.observe({**projection, "launcher_job_generation": generation, "setup_progress_room": room})
        self._reduce(event)
        projection["recovery_message"] = self.model.recovery_message
        self.present()
        return projection

    def accepts(self, event: LauncherEvent) -> bool:
        generation, room = self.progress.scope or (None, None)
        context = event.context
        return ((context.generation is None or context.generation == generation)
                and (context.room is None or context.room == room)
                and (context.attempt is None or context.attempt == self.attempt))

    def _reduce(self, event: LauncherEvent) -> None:
        if isinstance(event, ConnectionChanged) and event.state in {ConnectionState.STOPPED, ConnectionState.FAILED}:
            self.model = PlayModel(game_found=self.model.game_found,
                                   tone=Tone.FAILURE if event.state is ConnectionState.FAILED else Tone.NEUTRAL)
        elif isinstance(event, InstallationChanged):
            tone = {InstallationState.FAILED: Tone.FAILURE, InstallationState.BLOCKED: Tone.ATTENTION,
                    InstallationState.VERIFIED: Tone.SUCCESS}.get(event.state, Tone.PROGRESS)
            self.model = replace(self.model, installation=event.state, tone=tone,
                                 launch_option=event.launch_option or self.model.launch_option, action=ActionId.WAIT,
                                 game_connection_ready=self.model.game_connection_ready if event.connection_ready is None else event.connection_ready)
        elif isinstance(event, ExternalActionRequired):
            self.model = replace(self.model, action=event.action, tone=Tone.ATTENTION)
        else:
            self._reduce_observation(event)

    def _reduce_observation(self, event: LauncherEvent) -> None:
        if isinstance(event, SessionChanged):
            self._session(event)
        elif isinstance(event, NativeHealthChanged):
            self._session(event.session)
            self.model = replace(self.model, game_connection_ready=event.connection_ready)
        elif isinstance(event, ResyncProgress):
            self.model = replace(self.model, tone=event.tone, action=event.action)
        elif isinstance(event, DiscoveryChanged):
            self.model = replace(self.model, game_found=event.game_found)
        elif isinstance(event, DiagnosticsReady) and event.ok is not None:
            self.model = replace(self.model, tone=Tone.SUCCESS if event.ok else Tone.ATTENTION)

    def _session(self, event: SessionChanged) -> None:
        message = {"recovering": player_text('launcher.save', 'recovering'), "interrupted_save_recovered": player_text('launcher.save', 'recovered'),
                   "previous_checkpoint_restored": player_text('launcher.save', 'previous_restored')}.get(event.recovery_state, "")
        if event.recovery_state == "blocked" and event.recovery_reason:
            message = checkpoint_blocked(event.recovery_reason)
        self.model = replace(self.model, session_prepared=event.prepared, game_confirmed=event.confirmed,
                             tone=Tone.ATTENTION if event.blocked else self.model.tone,
                             recovery_message=message, new_save_available=event.new_save_available)

    def present(self) -> PlayModel:
        progress = self.progress.present()
        actions = {SetupAction.WAIT: ActionId.WAIT, SetupAction.FINISH_INSTALL: ActionId.FINISH_INSTALL,
                   SetupAction.CONFIRM_INSTALL: ActionId.CONFIRM_INSTALL, SetupAction.DIAGNOSE: ActionId.DIAGNOSE}
        action = self.model.action
        if progress and action is not ActionId.CONSENT:
            action = actions[progress.next_action]
        self.model = replace(self.model, progress=progress, action=action)
        return self.model
