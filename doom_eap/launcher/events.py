"""Public launcher events and the dictionary adapter; no orchestration or Qt."""

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any

from doom_eap.runtime.ledger_records import ItemAction, RoomKey

from .setup_progress import EVENT_STEPS, SetupStep


class EventOrigin(str, Enum):
    BRIDGE = "bridge"
    APPLICATION = "application"


class ConnectionState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


class InstallationState(str, Enum):
    CHECKING = "checking"
    REQUIRED = "required"
    INSTALLING = "installing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"


class ActionId(str, Enum):
    WAIT = "wait"
    FINISH_INSTALL = "finish_install"
    CONFIRM_INSTALL = "confirm_install"
    CONSENT = "consent"
    DIAGNOSE = "diagnose"
    VIEW_SESSION = "view_session"
    REPAIR_INVENTORY = "repair_inventory"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    RESUME = "resume"
    PREPARE = "prepare"
    REINSTALL = "reinstall"
    MANUAL_INSTALL = "manual_install"
    REPAIR_CONNECTION = "repair_connection"
    SAVE = "save"
    SELECT_PROTON = "select_proton"
    SHOW_PLAY = "show_play"
    CHOOSE_GAME_FOLDER = "choose_game_folder"
    FIX_SETUP = "fix_setup"
    SAVE_REPORT = "save_report"
    OPEN_ISSUE = "open_issue"
    UNINSTALL = "uninstall"
    LOCAL_BUILD = "local_build"
    AUTOMATIC_CORE = "automatic_core"
    REPAIR_CORE = "repair_core"
    REPAIR_SESSION = "repair_session"
    ROLLBACK_CORE = "rollback_core"


class Tone(str, Enum):
    NEUTRAL = "neutral"
    PROGRESS = "progress"
    SUCCESS = "success"
    ATTENTION = "attention"
    FAILURE = "failure"


@dataclass(frozen=True)
class EventContext:
    origin: EventOrigin
    operation: str | None = None
    attempt: int | None = None
    generation: int | None = None
    room: tuple[object, ...] | None = None


@dataclass(frozen=True, eq=False)
class LauncherEvent(Mapping[str, Any]):
    context: EventContext
    kind: str
    _data: Mapping[str, Any] = field(repr=False)

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._data[key])

    def __deepcopy__(self, memo):
        return self

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def projection(self) -> dict[str, Any]:
        return deepcopy(dict(self._data))


@dataclass(frozen=True, eq=False)
class ConnectionChanged(LauncherEvent):
    state: ConnectionState


@dataclass(frozen=True, eq=False)
class RoomAccepted(LauncherEvent):
    pass


@dataclass(frozen=True, eq=False)
class DiscoveryChanged(LauncherEvent):
    game_found: bool


@dataclass(frozen=True, eq=False)
class ProcessStateChanged(LauncherEvent):
    running: bool | None


@dataclass(frozen=True, eq=False)
class InstallationChanged(LauncherEvent):
    state: InstallationState
    step: SetupStep | None
    launch_option: str
    code: str | None
    connection_ready: bool | None


@dataclass(frozen=True, eq=False)
class ExternalActionRequired(LauncherEvent):
    action: ActionId
    request_id: str | None


@dataclass(frozen=True, eq=False)
class DependencyConsentRequested(ExternalActionRequired):
    pass


@dataclass(frozen=True, eq=False)
class InstallationConfirmationRequested(ExternalActionRequired):
    pass


@dataclass(frozen=True, eq=False)
class ConfigurationSaved(LauncherEvent):
    keys: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class DiagnosticsReady(LauncherEvent):
    ok: bool | None
    path: str | None


@dataclass(frozen=True, eq=False)
class RepairFinished(LauncherEvent):
    succeeded: bool


@dataclass(frozen=True, eq=False)
class ReportPrepared(LauncherEvent):
    path: str | None


@dataclass(frozen=True, eq=False)
class ReportSubmissionChanged(LauncherEvent):
    confirmed: bool
    url: str | None


@dataclass(frozen=True, eq=False)
class SessionChanged(LauncherEvent):
    prepared: bool
    confirmed: bool
    blocked: bool
    recovery_state: str
    recovery_reason: str
    new_save_available: bool


@dataclass(frozen=True, eq=False)
class NativeHealthChanged(LauncherEvent):
    connection_ready: bool
    session: SessionChanged


@dataclass(frozen=True, eq=False)
class ResyncProgress(LauncherEvent):
    tone: Tone
    action: ActionId
    room: RoomKey | None = None
    command_ids: tuple[str, ...] = ()


@dataclass(frozen=True, eq=False)
class AccountingCapabilityChanged(LauncherEvent):
    room: RoomKey
    request_id: str
    can_resolve: bool
    can_spend: bool = False


@dataclass(frozen=True, eq=False)
class ItemResolutionResult(LauncherEvent):
    room: RoomKey
    request_id: str
    succeeded: bool


@dataclass(frozen=True, eq=False)
class DeliveryProgress(LauncherEvent):
    room: RoomKey
    command_id: str
    revision: int
    receipt_index: int | None


@dataclass(frozen=True, eq=False)
class LocalDeliveryStatus(LauncherEvent):
    room: RoomKey
    item_id: int | None
    actions: tuple[ItemAction, ...]


CONNECTION_STATES = {
    "client_started": ConnectionState.CONNECTING, "connecting": ConnectionState.CONNECTING,
    "disconnected": ConnectionState.STOPPED, "client_stopping": ConnectionState.STOPPED,
    "error": ConnectionState.FAILED,
}
INSTALLATION_STATES = {
    "core_runtime_selected": InstallationState.INSTALLING,
    "setup_started": InstallationState.INSTALLING, "mod_building": InstallationState.INSTALLING,
    "runtime_config_ready": InstallationState.INSTALLING, "mod_staged": InstallationState.INSTALLING,
    "injector_started": InstallationState.INSTALLING, "manager_started": InstallationState.INSTALLING,
    "setup_failed": InstallationState.FAILED, "manual_install_required": InstallationState.BLOCKED,
    "installation_declined": InstallationState.BLOCKED,
}
EXTERNAL_ACTIONS = {
    "installation_confirmation_required": ActionId.CONFIRM_INSTALL,
    "dependency_consent_required": ActionId.CONSENT,
    "uninstall_confirmation_required": ActionId.CONFIRM_INSTALL,
}


def _installation_state(event: Mapping[str, Any]) -> InstallationState:
    if event.get("readiness") == "blocked":
        return InstallationState.BLOCKED
    if event["type"] == "room_install_state":
        return (InstallationState.VERIFIED if event.get("state") == "already_installed"
                else InstallationState.REQUIRED)
    if event["type"] == "setup_ready":
        return (InstallationState.VERIFIED if event.get("adapter_state") == "applied"
                else InstallationState.BLOCKED)
    return INSTALLATION_STATES[event["type"]]


def _session_event(context, kind, data, session) -> SessionChanged:
    state = session.get("state")
    resume_verified = session.get("intent") not in {"resume", "recover"} or session.get("resume_verified") is True
    confirmed = state == "admitted" and session.get("ready") is True and resume_verified
    return SessionChanged(context, kind, data,
        prepared=(state == "prelaunch_ready" and session.get("intent") not in {"resume", "recover"})
            or confirmed,
        confirmed=confirmed,
        blocked=state in {"process_unknown", "process_ambiguous", "process_changed",
                          "supervisor_unavailable", "admission_refused", "save_interrupted", "recovery_blocked"},
        recovery_state=session.get("recovery_state", "none"),
        recovery_reason=session.get("recovery_reason", session.get("checkpoint_state", "")),
        new_save_available=session.get("new_save_available") is True)


def adapt_event(event: Mapping[str, Any], *, origin=EventOrigin.APPLICATION,
                attempt=None, generation=None, room=None) -> LauncherEvent:
    """Capture current dictionaries once; preserve their compatibility projection."""
    if isinstance(event, LauncherEvent):
        context = event.context
        return replace(event, context=replace(context,
            attempt=context.attempt if context.attempt is not None else attempt,
            generation=context.generation if context.generation is not None else generation,
            room=context.room if context.room is not None else room))
    kind = event["type"]
    context = EventContext(origin, event.get("operation"), event.get("attempt_id", attempt),
                           event.get("launcher_job_generation", generation), event.get("setup_progress_room", room))
    data = MappingProxyType(deepcopy(dict(event)))
    if kind == "connected":
        return RoomAccepted(context, kind, data)
    if kind in CONNECTION_STATES:
        return ConnectionChanged(context, kind, data, CONNECTION_STATES[kind])
    if kind in INSTALLATION_STATES or kind in {"room_install_state", "setup_ready"}:
        return InstallationChanged(context, kind, data, _installation_state(event),
            event.get("setup_progress_step", EVENT_STEPS.get(kind)), event.get("steam_launch_option", ""),
            event.get("code"), event.get("meathook_ok"))
    return _adapt_other_event(context, kind, data)


def _adapt_other_event(context, kind, data) -> LauncherEvent:
    if kind == "delivery_local_status":
        return _local_delivery_status(context, kind, data)
    if kind == "delivery_progress":
        return _delivery_progress(context, kind, data)
    if kind in {"accounting_capability_changed", "item_resolution_result"}:
        return _adapt_delivery_event(context, kind, data)
    if kind in EXTERNAL_ACTIONS:
        prompt = {"dependency_consent_required": DependencyConsentRequested,
                  "installation_confirmation_required": InstallationConfirmationRequested}.get(kind, ExternalActionRequired)
        return prompt(context, kind, data, EXTERNAL_ACTIONS[kind], data.get("request_id"))
    if kind == "ap_session_status":
        return _session_event(context, kind, data, data)
    if kind == "integration_status":
        session = _session_event(context, kind, data, data.get("session", {}))
        return NativeHealthChanged(context, kind, data, data.get("meathook_ok") is True, session)
    if kind == "inventory_resync":
        tone = {"error": Tone.FAILURE, "observed": Tone.SUCCESS, "noop": Tone.SUCCESS}.get(data.get("status"), Tone.PROGRESS)
        action = ActionId.WAIT if data.get("status") == "waiting" else ActionId.REPAIR_INVENTORY
        return _resync_progress(context, kind, data, tone, action)
    return _adapt_application_event(context, kind, data)


def _resync_progress(context, kind, data, tone, action):
    from doom_eap.runtime.delivery_control import control_room

    room = control_room(data["room"]) if "room" in data else None
    commands = data.get("command_ids", ())
    if not isinstance(commands, list | tuple) or any(not isinstance(c, str) or not c for c in commands):
        raise ValueError('Invalid resync command identities')
    return ResyncProgress(context, kind, data, tone, action, room, tuple(commands))


def _delivery_progress(context, kind, data):
    from doom_eap.runtime.delivery_control import control_room
    from doom_eap.runtime.ledger_records import CommandState

    room = control_room(data.get("room"))
    command_id, revision, index = data.get("command_id"), data.get("revision"), data.get("receipt_index")
    if not isinstance(command_id, str) or not command_id or type(revision) is not int or revision < 0:
        raise ValueError('Invalid delivery progress identity')
    if index is not None and (type(index) is not int or index < 0):
        raise ValueError('Invalid delivery progress receipt')
    CommandState(data.get("state"))
    return DeliveryProgress(context, kind, data, room, command_id, revision, index)


def _local_delivery_status(context, kind, data):
    from doom_eap.runtime.delivery_control import control_room
    from doom_eap.runtime.ledger_records import ItemAction, ResolutionAction

    room = control_room(data.get("room"))
    command_id = data.get("command_id")
    if command_id is None:
        return LocalDeliveryStatus(context, kind, data, room, None, ())
    revision, item_id = data.get("expected_revision"), data.get("item_id")
    if not isinstance(command_id, str) or not command_id or type(revision) is not int or revision < 0:
        raise ValueError('Invalid local delivery command')
    if type(item_id) is not int:
        raise ValueError('Invalid local delivery item')
    actions = tuple(ItemAction(action, command_id, revision) for action in ResolutionAction)
    return LocalDeliveryStatus(context, kind, data, room, item_id, actions)


def _adapt_delivery_event(context, kind, data):
    from doom_eap.runtime.delivery_control import control_room

    room = control_room(data.get("room"))
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or type(data.get("ok")) is not bool:
        raise ValueError('invalid delivery control result')
    if kind == "item_resolution_result":
        return ItemResolutionResult(context, kind, data, room, request_id, data["ok"])
    if type(data.get("can_resolve")) is not bool:
        raise ValueError('invalid delivery capability')
    return AccountingCapabilityChanged(context, kind, data, room, request_id, data["can_resolve"] and data["ok"],
                                       data.get("can_spend") is True and data["ok"])


def _adapt_application_event(context, kind, data) -> LauncherEvent:
    if kind == "discovery_changed":
        return DiscoveryChanged(context, kind, data, bool(data.get("game_root")))
    if kind == "process_state_changed":
        return ProcessStateChanged(context, kind, data, data["running"])
    if kind == "configuration_saved":
        return ConfigurationSaved(context, kind, data, tuple(data["keys"]))
    if kind in {"doctor_report", "support_bundle_ready"}:
        return DiagnosticsReady(context, kind, data, data.get("report", {}).get("ok"), data.get("path"))
    if kind in {"integration_repair_result", "ui_repair_result"}:
        return RepairFinished(context, kind, data, data.get("success") is True)
    if kind == "problem_report_ready":
        return ReportPrepared(context, kind, data, data.get("path"))
    if kind in {"problem_report_submitted", "problem_report_retry"}:
        return ReportSubmissionChanged(context, kind, data, kind == "problem_report_submitted", data.get("url"))
    return LauncherEvent(context, kind, data)
