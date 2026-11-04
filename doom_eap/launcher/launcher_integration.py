"""Room-bound setup services for standalone DOOM Eternal launcher."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from doom_eap.launcher.ui.strings import text as player_text
from doom_eap.runtime.context_registry import SlotAdmissionError, admit_slot_data

from .install_journal import InstallationRecoveryRequired, InstallJournal, InstallPhase, file_digest
from .installation import InstallationActions, InstallationOutcome, InstallationPlan, InstallationService
from .installation_adapters import LinuxModManagerAdapter, WindowsModInjectorAdapter
from .launcher_core import CLIENT_CONFIG_FIELDS, LaunchWorkflow, RoomCompiler, RoomSnapshot, SeedManifest
from .launcher_core_runtime import CoreRuntime
from .launcher_interactions import LauncherInteractions, ScopedInteractions
from .launcher_platform import (
    IDFILE_DECOMPRESSOR_LINUX,
    IDFILE_DECOMPRESSOR_WINDOWS,
    LINUX_MOD_INJECTOR,
    WINDOWS_MANUAL_INSTALL_GUIDE_URL,
    WINDOWS_MOD_INJECTOR,
    AdapterResult,
    DependencyManager,
    GameLinkResult,
    SteamLaunchOptionsManager,
    cleanup_stale_doom_config_bind,
    detect_doom_processes,
    finalize_mod_installation,
    idfile_decompressor_spec,
    install_meathook,
    installation_write,
    probe_meathook,
    probe_runtime_prerequisites,
    publish_file,
    stage_room_mod,
    verify_linux_mod_installation,
    verify_mod_installation,
)
from .launcher_session import APSessionOwner
from .launcher_workers import LauncherJob, LauncherWorkCancelled, LauncherWorkers

EventSink = Callable[[str, dict[str, object]], None]
ConsentCallback = Callable[[object], bool]
ConfirmationCallback = Callable[[], bool]


class LauncherPackageError(ValueError):
    code = "application_package_invalid"
    failure_domain = "application_package"


class ModBuildingToolError(RuntimeError):
    code = "mod_building_tool_unavailable"
    failure_domain = "room_package"

ROOM_PACKAGE_FAILURE_TITLE = player_text('launcher.ui', 'room_mod_could_not_be_built')
ROOM_PACKAGE_FAILURE_MESSAGE = (
    player_text('launcher.ui', 'open_session_and_select_rebuild_room_package_if_it_fails_again_s')
)
ROOM_PACKAGE_FAILURE_ACTION = player_text('launcher.integration', 'rebuild_room_package')
APPLICATION_PACKAGE_FAILURE_TITLE = player_text('launcher.integration', 'launcher_files_missing_or_damaged')
APPLICATION_PACKAGE_FAILURE_MESSAGE = (
    player_text('launcher.integration', 'extract_the_complete_doomeap_download_into_a_new_folder_and_run')
)
APPLICATION_PACKAGE_FAILURE_ACTION = player_text('launcher.integration', 'extract_the_complete_download')
MOD_BUILDING_TOOL_UNAVAILABLE_MESSAGE = (
    player_text('launcher.integration', 'a_required_mod_building_tool_could_not_be_prepared_check_your_co')
)
GAME_SETUP_FAILURE_TITLE = player_text('launcher.ui', 'game_setup_needs_attention')
GAME_SETUP_FAILURE_MESSAGE = (
    player_text('launcher.integration', 'open_help_and_select_fix_setup_if_it_fails_again_select_help_sav')
)
GAME_SETUP_FAILURE_ACTION = player_text('launcher.ui', 'repair_game_connection_d53210')
INSTALLED_PACKAGE_FAILURE_TITLE = player_text('launcher.ui', 'room_mod_update_required')
INSTALLED_PACKAGE_FAILURE_MESSAGE = (
    player_text('launcher.integration', 'select_update_room_package_before_playing')
)
INSTALLED_PACKAGE_FAILURE_ACTION = player_text('launcher.integration', 'update_room_package')
ROOM_PACKAGE_INCOMPATIBLE_TITLE = player_text('launcher.ui', 'room_mod_needs_rebuilding')
ROOM_PACKAGE_INCOMPATIBLE_MESSAGE = (
    player_text('launcher.integration', 'this_room_is_incompatible_with_this_launcher_select_help_save_su')
)
ROOM_PACKAGE_INCOMPATIBLE_ACTION = player_text('launcher.integration', 'rebuild_room_package')

UNINSTALL_OWNED_STATES = frozenset({
    "uninstall_in_progress",
    "uninstall_attention",
    "uninstall_failed",
    "uninstalled",
})


def _declared_failure_domain(error, phase):
    if isinstance(error, SlotAdmissionError):
        return error.failure_domain
    if getattr(error, "failure_domain", None) in {"proton_runtime", "application_package", "room_package", "installed_room_package"}:
        return error.failure_domain
    if phase == "campaign_session" or getattr(error, "failure_domain", None) == "campaign_session":
        return "campaign_session"
    return None


def classify_setup_failure(error: BaseException, *, phase: str = "") -> str:
    """Classify setup failures without weakening any validation boundary."""
    declared = _declared_failure_domain(error, phase)
    if declared:
        return declared
    if phase in {"room_snapshot", "manifest", "room_package"}:
        return "room_package"
    return "game_setup"


def _campaign_failure_message(error, raw_message):
    if getattr(error, "code", None) == "save_interrupted":
        return player_text('launcher.save', 'interrupted')
    processes = getattr(error, "processes", None)
    channel_failure = isinstance(error, BrokenPipeError | EOFError) or getattr(error, "code", None) == "session_supervisor_failed"
    if processes:
        return player_text('launcher.ui', 'close_doom_eternal_open_ap_save_to_inspect_backups_then_select_h')
    elif channel_failure:
        return (player_text('launcher.integration', 'the_game_connection_helper_stopped_check_that_the_selected_proto'))
    elif processes is not None:
        return player_text('launcher.integration', 'open_ap_save_to_inspect_backups_then_select_help_save_support_re')
    else:
        return (player_text('launcher.integration', 'save_changes_are_blocked_until_doom_eternal_s_process_identity_c'))


def setup_failure_payload(error: BaseException, *, phase: str = "") -> dict[str, object]:
    """Return user routing metadata alongside untouched technical failure data."""
    if isinstance(error, SlotAdmissionError):
        return _slot_admission_failure_payload(error, phase)
    raw_message = _error_message(error)
    failure_domain = classify_setup_failure(error, phase=phase)
    code = getattr(error, "code", None)
    tool_unavailable = code == "mod_building_tool_unavailable"
    if failure_domain == "application_package":
        recovery_action = "reinstall_application"
        user_title = player_text('launcher.integration', 'launcher_files_missing_or_damaged')
        user_message = player_text('launcher.integration', 'extract_the_complete_doomeap_download_into_a_new_folder_and_run')
        user_action = player_text('launcher.integration', 'extract_the_complete_download')
    elif failure_domain == "room_package":
        recovery_action = "rebuild_room_package"
        if code in {"unsupported_contract", "unsupported_capabilities", "unsupported_schema", "unsupported_revision", "incompatible_bridge_protocol"}:
            user_title = player_text('launcher.ui', 'room_mod_needs_rebuilding')
            user_message = player_text('launcher.integration', 'this_room_is_incompatible_with_this_launcher_select_help_save_su')
            user_action = player_text('launcher.integration', 'rebuild_room_package')
        elif tool_unavailable:
            user_title = player_text('launcher.ui', 'room_mod_could_not_be_built')
            user_message = player_text('launcher.integration', 'a_required_mod_building_tool_could_not_be_prepared_check_your_co')
            user_action = player_text('launcher.integration', 'rebuild_room_package')
        else:
            user_title = player_text('launcher.ui', 'room_mod_could_not_be_built')
            user_message = player_text('launcher.ui', 'open_session_and_select_rebuild_room_package_if_it_fails_again_s')
            user_action = player_text('launcher.integration', 'rebuild_room_package')
    elif failure_domain == "proton_runtime":
        recovery_action = "select_proton"
        user_title = player_text('launcher.integration', 'select_doom_eternal_s_proton_version')
        user_message = raw_message
        user_action = player_text('launcher.ui', 'select_proton')
    elif failure_domain == "campaign_session":
        recovery_action = "inspect_campaign"
        user_title = player_text('launcher.ui', 'save_setup_failed')
        user_message = _campaign_failure_message(error, raw_message)
        user_action = player_text('launcher.ui', 'ap_save_bbd136')
    else:
        recovery_action = "repair_game_integration"
        user_title = player_text('launcher.ui', 'game_setup_needs_attention')
        user_message = player_text('launcher.integration', 'open_help_and_select_fix_setup_if_it_fails_again_select_help_sav')
        user_action = player_text('launcher.ui', 'repair_game_connection_d53210')
    return {
        "error_type": type(error).__name__,
        "message": raw_message,
        "raw_message": raw_message,
        "technical_error_type": type(error).__name__,
        "code": getattr(error, "code", None),
        "technical_message": raw_message,
        "processes": getattr(error, "processes", None),
        "errno": getattr(error, "cause_errno", getattr(error, "errno", None)),
        "winerror": getattr(error, "cause_winerror", getattr(error, "winerror", None)),
        "filename": getattr(
            error, "cause_filename", getattr(error, "filename", None)
        )
        or getattr(error, "source_path", None),
        "filename2": getattr(
            error, "cause_filename2", getattr(error, "filename2", None)
        )
        or getattr(error, "destination_path", None),
        "operation": getattr(error, "operation", None),
        "source_path": getattr(error, "source_path", None),
        "destination_path": getattr(error, "destination_path", None),
        "attempt_count": getattr(error, "attempt_count", None),
        "stage": getattr(error, "stage", None) or phase or "setup",
        "failure_domain": failure_domain,
        "recovery_action": recovery_action,
        "user_title": user_title,
        "user_message": user_message,
        "user_action": user_action,
        "proton_reason": getattr(error, "proton_reason", None),
        "proton_candidates": getattr(error, "proton_candidates", None),
        "proton_selected": getattr(error, "proton_selected", None),
    }


def _error_message(error):
    return str(error) or type(error).__name__


def _slot_admission_failure_payload(error, phase):
    return {
        "error_type": type(error).__name__, "message": str(error), "raw_message": str(error),
        "technical_error_type": type(error).__name__, "technical_message": str(error),
        "code": error.code, "failure_domain": error.failure_domain, "stage": phase or "slot_admission",
        "recovery_action": "generate_room", "user_title": player_text('launcher.integration', 'room_data_is_incompatible'),
        "user_message": str(error), "user_action": player_text('launcher.integration', 'generate_a_compatible_room'),
    }


def installed_package_issue_payload(reason: str) -> dict[str, object]:
    """Return routing metadata for receipt-backed package verification failures."""
    return {
        "failure_domain": "installed_room_package",
        "recovery_action": "update_room_package",
        "user_title": player_text('launcher.ui', 'room_mod_update_required'),
        "user_message": player_text('launcher.integration', 'select_update_room_package_before_playing'),
        "user_action": player_text('launcher.integration', 'update_room_package'),
        "technical_message": reason,
        "raw_message": reason,
    }


@dataclass(frozen=True)
class IntegratedSetupRecord:
    manifest_hash: str
    randomize_dash: bool
    generated_mod: str
    staged_mod: str
    staged_sha256: str
    adapter_state: str
    adapter_message: str
    adapter_command: tuple[str, ...] = ()
    injector_command: tuple[str, ...] = ()
    steam_launch_option: str = ""
    steam_launch_option_diff: str = ""
    installation_mode: str = "automatic"
    user_confirmed: bool = False
    new_install: bool = False
    room_zip_sha256: str = ""
    generated_sha256: str = ""
    mods_path: str = ""
    injector_version: str = ""
    injector_exit_code: int | None = None
    required_resources: tuple[dict[str, object], ...] = ()
    post_install_verification: str = ""
    static_content_digest: str = ""
    installed_resources: dict[str, str] = field(default_factory=dict)
    adapter_details: dict[str, object] = field(default_factory=dict)
    transaction_id: str = ""


@dataclass(frozen=True)
class InstallState:
    state: str
    manifest_hash: str
    staged_mod: str = ""
    steam_launch_option: str = ""
    reason: str = ""
    readiness: str = "ready"
    readiness_reason: str = ""
    reason_code: str = ""


@dataclass(frozen=True)
class UninstallResult:
    state: str
    manifest_hash: str
    staged_mod: str
    quarantine_path: str = ""
    adapter_state: str = ""
    injector_state: str = ""
    message: str = ""
    attention: bool = False


class IntegratedLaunchWorkflow:
    """Generate, stage, and prepare platform tooling after authoritative Connected."""

    def __init__(
        self,
        application_dir: Path,
        state_dir: Path,
        config_path: Path,
        *,
        data_dir: Path | None = None,
        platform_name: str | None = None,
        event_sink: EventSink | None = None,
        consent: ConsentCallback | None = None,
        confirmation: ConfirmationCallback | None = None,
        uninstall_confirmation: ConfirmationCallback | None = None,
        session_owner: APSessionOwner | None = None,
    ):
        self.base_workflow = LaunchWorkflow()
        self.application_dir = application_dir
        self.state_dir = state_dir
        self.data_dir = data_dir or state_dir.parent / "data"
        self.session_owner = session_owner or APSessionOwner(application_dir, self.data_dir, state_dir)
        self.config_path = config_path
        self.platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
        self.event_sink = event_sink or (lambda _kind, _payload: None)
        self.consent = consent or (lambda _spec: False)
        self.confirmation = confirmation or (lambda: False)
        self.uninstall_confirmation = uninstall_confirmation or (lambda: False)
        self._failure_phase = "game_setup"
        self._configuration_snapshot: dict[str, object] | None = None
        self._job: LauncherJob | None = None
        self.core_runtime = CoreRuntime(self.data_dir / "core-runtime", emit=lambda kind, **payload: self._emit(kind, **payload))

    def for_job(self, job: LauncherJob, event_sink: EventSink,
                interactions: ScopedInteractions) -> IntegratedLaunchWorkflow:
        """Bind an adapter operation to one captured configuration and job lifetime."""
        job.check()
        configuration = deepcopy(self._config())
        job.check()

        workflow = IntegratedLaunchWorkflow(
            self.application_dir, self.state_dir, self.config_path,
            data_dir=self.data_dir, platform_name=self.platform_name,
            event_sink=lambda kind, payload: event_sink(kind, job.event(payload)),
            consent=interactions.consent,
            confirmation=interactions.confirmation,
            uninstall_confirmation=interactions.uninstall_confirmation,
            session_owner=self.session_owner,
        )
        workflow._configuration_snapshot = configuration
        workflow._job = job
        workflow.core_runtime = self.core_runtime
        return workflow

    def check_cancelled(self) -> None:
        if self._job is not None:
            self._job.check()

    def _publish_client_config(self, **kwargs) -> Path:
        def write():
            return self.base_workflow.write_client_config(self.application_dir, **kwargs)
        return self._job.publish(write) if self._job is not None else write()

    def _publish_receipt(self, temporary: Path, receipt: Path, *, operation: str):
        def write():
            return publish_file(temporary, receipt, operation=operation)
        return self._job.publish(write) if self._job is not None else write()

    def _room_compiler(
        self,
        *,
        decompressor: Path | None = None,
        dependency_manager: object | None = None,
        consent: object | None = None,
    ) -> RoomCompiler:
        return RoomCompiler(
            self.application_dir / "resources" / "base_mod.zip",
            self.application_dir / "resources" / "room_payloads.zip",
            self.application_dir / "resources" / "room_payload_manifest.json",
            decompressor=decompressor,
            dependency_manager=dependency_manager,
            consent=consent,
        )

    def _manifest_for(self, snapshot: RoomSnapshot) -> SeedManifest:
        admit_slot_data(snapshot.slot_data)
        try:
            room_compiler = self._room_compiler()
            return self.base_workflow.manifest_for(
                snapshot,
                static_content_digest=room_compiler.static_content_digest,
            )
        except (OSError, ValueError) as error:
            raise LauncherPackageError(str(error)) from error

    def _emit(self, kind: str, **payload: object) -> None:
        self.event_sink(kind, payload)

    def _config(self) -> dict[str, object]:
        self.check_cancelled()
        if self._configuration_snapshot is not None:
            return deepcopy(self._configuration_snapshot)
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(player_text('launcher.integration', 'launcher_configuration_must_contain_an_object'))
        return document

    @staticmethod
    def _game_root(config: dict[str, object]) -> Path:
        base = Path(str(config.get("doom_base_dir", ""))).expanduser().resolve()
        root = base.parent if base.name.casefold() == "base" else base
        if not (root / "DOOMEternalx64vk.exe").is_file() or not (root / "base").is_dir():
            raise ValueError(player_text('launcher.integration', 'invalid_configured_doom_eternal_installation_value_0', value_0=f'{root}'))
        return root

    def _steam_plan(self, config: dict[str, object]):
        current = str(config.get("steam_launch_options") or "%command%")
        remote = config.get("steam_remote_dir")
        if remote:
            remote_path = Path(str(remote)).expanduser()
            try:
                localconfig = remote_path.parents[1] / "config" / "localconfig.vdf"
            except IndexError:
                localconfig = Path()
            if localconfig.is_file():
                try:
                    current = SteamLaunchOptionsManager.detect(localconfig)
                except ValueError as error:
                    message = str(error)
                    self._emit(
                        "warning",
                        message=message,
                        field="steam_launch_options",
                        path=str(localconfig),
                    )
                    (self.state_dir / "steam_launch_option.txt").write_text("", encoding="utf-8")
                    (self.state_dir / "steam_launch_option.diff").write_text(
                        message + "\n", encoding="utf-8"
                    )
                    return type(
                        SteamLaunchOptionsManager.plan_bridge(
                            "%command%", self.application_dir / "run_bridge.sh"
                        )
                    )("", "", message)
        plan = SteamLaunchOptionsManager.plan_bridge(
            current,
            self.application_dir / "run_bridge.sh",
            delay=5,
        )
        (self.state_dir / "steam_launch_option.txt").write_text(
            plan.proposed + "\n", encoding="utf-8"
        )
        (self.state_dir / "steam_launch_option.diff").write_text(
            (plan.diff or player_text('launcher.integration', 'no_change_required') + '\n' + '') + ("\n" if plan.diff else ""),
            encoding="utf-8",
        )
        return plan

    def _acquire(self, manager: DependencyManager, spec, local_artifact: Path | None):
        installed = manager.acquire(
            spec,
            consent=self.consent,
            local_artifact=local_artifact,
        )
        self._emit(
            "dependency_ready",
            name=installed.name,
            version=installed.version,
            executable=installed.executable,
        )
        return installed

    def _idfile_manager(self) -> DependencyManager:
        manager = DependencyManager(
            self.data_dir / "dependencies",
            diagnostic_sink=lambda kind, payload: self._emit(kind, **payload),
        )
        spec = idfile_decompressor_spec(self.platform_name)
        if spec is None or not manager.platform_supported(spec, self.platform_name):
            message = (
                player_text('launcher.integration', 'idfiledecompressor_is_unavailable_on_unsupported_platform_value', value_0=f'{self.platform_name}')
            )
            self._emit(
                "prerequisite_missing",
                key="idfile_decompressor",
                message=message,
                details={
                    "status": "unsupported",
                    "platform": self.platform_name,
                    "supported_platforms": ["linux", "windows"],
                },
            )
            raise ModBuildingToolError(message)
        return manager

    def repair_idfile_decompressor(self):
        """Acquire selected verified codec after explicit repair consent."""
        spec = idfile_decompressor_spec(self.platform_name)
        manager = self._idfile_manager()
        if spec is None:
            raise RuntimeError(player_text('launcher.integration', 'idfiledecompressor_is_unavailable_on_platform_value_0', value_0=f'{self.platform_name}'))
        return self._acquire(manager, spec, None)

    def remove_idfile_decompressor(self) -> bool:
        """Remove only launcher-owned cached codec artifact."""
        manager = DependencyManager(self.data_dir / "dependencies")
        removed = False
        for spec in (IDFILE_DECOMPRESSOR_LINUX, IDFILE_DECOMPRESSOR_WINDOWS):
            removed = manager.remove(spec) or removed
        return removed

    def install_state(self, snapshot: RoomSnapshot) -> InstallState:
        """Resolve current room identity against verified launcher ownership."""
        manifest = self._manifest_for(snapshot)
        if self._installation_pending():
            reason = player_text('launcher.setup', 'protection_stopped')
            return InstallState("update_required", manifest.manifest_hash, reason=reason,
                                readiness="blocked", readiness_reason=reason)
        receipt_path = self.state_dir / "launcher_setup.json"
        if not receipt_path.is_file():
            return InstallState("install_needed", manifest.manifest_hash, reason=player_text('launcher.integration', 'no_launcher_install_record'))
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            staged = Path(str(receipt["staged_mod"])).resolve()
            expected_sha = str(receipt["staged_sha256"])
            recorded_manifest = str(receipt["manifest_hash"])
            adapter_state = str(receipt.get("adapter_state", ""))
            bool(receipt.get("windows_installation_confirmed", False))
            steam_option = str(receipt.get("steam_launch_option", ""))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return InstallState("update_required", manifest.manifest_hash, reason=player_text('launcher.integration', 'invalid_install_record_value_0', value_0=f'{error}'))
        if adapter_state in UNINSTALL_OWNED_STATES:
            if recorded_manifest != manifest.manifest_hash:
                return InstallState(
                    "install_needed",
                    manifest.manifest_hash,
                    reason=player_text('launcher.integration', 'previous_room_mod_has_uninstall_state_value_0', value_0=f'{adapter_state}'),
                )
            if adapter_state != "uninstalled":
                reason = str(receipt.get("adapter_message") or player_text('launcher.integration', 'room_mod_uninstall_requires_attention'))
                return InstallState(
                    adapter_state,
                    manifest.manifest_hash,
                    str(staged),
                    steam_option,
                    reason=reason,
                    readiness="blocked",
                    readiness_reason=reason,
                )
            return InstallState(
                "uninstalled",
                manifest.manifest_hash,
                str(staged),
                steam_option,
                reason=player_text('launcher.integration', 'room_mod_was_intentionally_uninstalled'),
            )
        if recorded_manifest != manifest.manifest_hash:
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, player_text('launcher.integration', 'installed_manifest_belongs_to_another_room_or_build'), reason_code="room_changed")
        recorded_static_digest = str(receipt.get("static_content_digest", ""))
        if recorded_static_digest and recorded_static_digest != manifest.static_content_digest:
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, player_text('launcher.integration', 'installed_package_static_content_is_out_of_date'))
        if not staged.is_file():
            return InstallState("update_required", manifest.manifest_hash, reason=player_text('launcher.integration', 'recorded_installed_package_is_missing'))
        if hashlib.sha256(staged.read_bytes()).hexdigest() != expected_sha:
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, player_text('launcher.integration', 'installed_package_hash_does_not_match_record'))
        if not RoomCompiler.validate_cached_package(staged, manifest):
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, player_text('launcher.integration', 'installed_package_embedded_content_identity_does_not_match_curre'))
        if adapter_state != "applied":
            return InstallState("install_needed", manifest.manifest_hash, reason=player_text('launcher.integration', 'previous_install_state_is_value_0', value_0=f"{adapter_state or 'unknown'}"))

        generated_path = Path(str(receipt.get("generated_mod", ""))).expanduser()
        generated_sha = str(receipt.get("generated_sha256", ""))
        room_zip_sha = str(receipt.get("room_zip_sha256", expected_sha))
        if not generated_path.is_file() or not generated_sha:
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason=player_text('launcher.integration', 'install_record_lacks_generated_room_zip_evidence'),
            )
        try:
            generated_actual = hashlib.sha256(generated_path.read_bytes()).hexdigest()
        except OSError as error:
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason=player_text('launcher.integration', 'generated_room_zip_evidence_unavailable_value_0', value_0=f'{error}'),
            )
        if generated_actual != generated_sha or expected_sha != room_zip_sha or generated_sha != expected_sha:
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason=player_text('launcher.doctor', 'generated_and_staged_room_zip_hashes_differ'),
            )
        if not RoomCompiler.validate_cached_package(generated_path, manifest):
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason=player_text('launcher.integration', 'generated_cache_embedded_content_identity_does_not_match_current'),
            )

        try:
            config = self._config()
            game_root = self._game_root(config)
            if self.platform_name == "windows":
                from .campaign_resources import verify_installed_resources
                try:
                    verify_installed_resources(game_root, receipt)
                except (OSError, ValueError) as error:
                    return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option,
                                        reason=str(error))
            if self.platform_name == "linux":
                if not self._linux_installation_evidence(receipt, game_root):
                    return InstallState(
                        "already_installed", manifest.manifest_hash, str(staged), steam_option,
                        readiness="blocked", readiness_reason=player_text('launcher.integration', 'linux_install_evidence_is_incomplete'),
                    )
                verify_linux_mod_installation(game_root, staged)
            prereqs = probe_runtime_prerequisites(game_root, self.application_dir, config)
            if not prereqs.ok:
                meathook = prereqs.meathook
                reason = meathook.message if meathook and not meathook.ok else player_text('launcher.integration', 'runtime_prerequisites_not_met')
                return InstallState(
                    "already_installed",
                    manifest.manifest_hash,
                    str(staged),
                    steam_option,
                    readiness="blocked",
                    readiness_reason=reason,
                )
        except Exception as error:
            reason = str(error) or type(error).__name__
            return InstallState(
                "already_installed",
                manifest.manifest_hash,
                str(staged),
                steam_option,
                readiness="blocked",
                readiness_reason=reason,
            )

        return InstallState("already_installed", manifest.manifest_hash, str(staged), steam_option, readiness="ready")

    def _installation_pending(self):
        config = self._config()
        if not config.get("doom_base_dir"):
            return False
        journal = self._installation_journal(self._game_root(config))
        try:
            return journal.load() and journal.phase not in {InstallPhase.COMPLETE, InstallPhase.ROLLED_BACK}
        except InstallationRecoveryRequired:
            return True

    @staticmethod
    def _linux_installation_evidence(receipt, game_root):
        common = (receipt.get("post_install_verification") == "verified"
            and receipt.get("mods_path") == str((game_root / "Mods").resolve())
            and IntegratedLaunchWorkflow._verified_resource_evidence(receipt.get("required_resources")))
        manual = receipt.get("installation_mode") == "manual_fallback" and receipt.get("user_confirmed") is True
        return common and (manual or IntegratedLaunchWorkflow._injector_evidence(receipt))

    @staticmethod
    def _verified_resource_evidence(rows):
        return (isinstance(rows, list | tuple) and len(rows) in (3, 4)
                and all(isinstance(row, dict) and row.get("status") == "verified" for row in rows))

    @staticmethod
    def _injector_evidence(receipt):
        command = receipt.get("injector_command", receipt.get("adapter_command"))
        return (bool(receipt.get("injector_version")) and receipt.get("injector_exit_code") == 0
                and isinstance(command, list | tuple) and bool(command))

    def mark_windows_installation(self, succeeded: bool) -> None:
        return

    def _prepare_adapter(
        self,
        config: dict[str, object],
        game_root: Path,
        journal,
    ):
        manager = DependencyManager(self.state_dir / "dependencies")
        if self.platform_name == "windows":
            local_key = "eternal_mod_injector_archive"
            local_value = (
                config.get(local_key)
                or config.get("eternal_mod_injector_zip")
                or config.get("eternal_mod_manager_archive")
                or config.get("eternal_mod_manager_zip")
            )
            local_artifact = Path(str(local_value)).expanduser() if local_value else None
            try:
                dependency = self._acquire(manager, WINDOWS_MOD_INJECTOR, local_artifact)
            except Exception as error:
                return (
                    AdapterResult(
                        state="manual_install_required",
                        message=player_text('launcher.integration', 'automatic_eternalmodinjector_setup_could_not_be_completed_value', value_0=f'{error}'),
                        details={"installation_mode": "manual_install_required", "error": str(error)},
                    ),
                    None,
                    "",
                    "",
                )
            adapter = WindowsModInjectorAdapter(
                dependency,
                state_dir=self.state_dir,
                confirmer=self.confirmation,
                event_sink=self._emit_injector,
            )
            return adapter, adapter.prepare(game_root, journal=journal), "", ""
        if self.platform_name == "linux":
            plan = self._steam_plan(config)
            local_value = config.get("eternal_basher_archive")
            local_artifact = Path(str(local_value)).expanduser() if local_value else None
            try:
                dependency = self._acquire(manager, LINUX_MOD_INJECTOR, local_artifact)
            except PermissionError:
                return (
                    AdapterResult(
                        state="failed",
                        message=player_text('launcher.integration', 'injector_download_was_not_approved_try_setup_again_when_ready'),
                    ),
                    None,
                    plan.proposed,
                    plan.diff,
                )
            adapter = LinuxModManagerAdapter(dependency, state_dir=self.state_dir, event_sink=self._emit_injector)
            return adapter, adapter.prepare(game_root, journal=journal), plan.proposed, plan.diff
        raise RuntimeError(player_text('launcher.integration', 'unsupported_platform_value_0', value_0=f'{self.platform_name}'))

    def _run_injector(
        self,
        config: dict[str, object],
        game_root: Path,
        *,
        operation: str = "install",
        staged_mod: str = "",
    ) -> tuple[AdapterResult, str, str]:
        """Run platform injector without coupling operation to package staging."""
        manager = DependencyManager(self.state_dir / "dependencies")
        if self.platform_name == "windows":
            local_value = (
                config.get("eternal_mod_injector_archive")
                or config.get("eternal_mod_injector_zip")
                or config.get("eternal_mod_manager_archive")
                or config.get("eternal_mod_manager_zip")
            )
            local_artifact = Path(str(local_value)).expanduser() if local_value else None
            dependency = self._acquire(manager, WINDOWS_MOD_INJECTOR, local_artifact)
            result = WindowsModInjectorAdapter(
                dependency,
                state_dir=self.state_dir,
                confirmer=lambda: True,
                event_sink=self._emit,
            ).run(
                game_root,
                staged_mod=staged_mod,
                operation=operation,
                uninstall_confirmation=(self.uninstall_confirmation if operation == "uninstall" else None),
            )
            return result, "", ""
        if self.platform_name == "linux":
            plan = self._steam_plan(config)
            local_value = config.get("eternal_basher_archive")
            local_artifact = Path(str(local_value)).expanduser() if local_value else None
            dependency = self._acquire(manager, LINUX_MOD_INJECTOR, local_artifact)
            self._emit("injector_started", command=[str(dependency.executable)], operation="uninstall")
            result = LinuxModManagerAdapter(dependency, state_dir=self.state_dir, event_sink=self._emit).run(game_root)
            self._emit(
                "injector_finished",
                state=result.state,
                message=result.message,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                operation="uninstall",
            )
            return result, plan.proposed, plan.diff
        raise RuntimeError(player_text('launcher.integration', 'unsupported_platform_value_0', value_0=f'{self.platform_name}'))

    def ensure_game_link(
        self,
        game_root: Path,
        *,
        local_artifact: Path | None = None,
        force_repair: bool = False,
        journal=None,
    ) -> GameLinkResult:
        config = self._config()
        if local_artifact is not None:
            config["core_runtime_manifest"] = str(local_artifact)
        selected = self.core_runtime.resolve(config, game_root, refresh=force_repair and local_artifact is None)
        config["selected_core_runtime_manifest"] = str(selected)
        self._configuration_snapshot = config
        from ..contracts.core_distribution import read_manifest
        selected_manifest = read_manifest(selected)
        self._emit("core_runtime_selected", path=str(selected), development_override=bool(config.get("core_runtime_manifest")),
                   message=player_text('launcher.integration', 'core_value_0_selected_build_value_1', value_0=f"{selected_manifest['version']}", value_1=f"{selected_manifest['build_id']}"))
        if journal is not None:
            journal.document["evidence"]["selected_core_runtime_manifest"] = str(selected)
            journal.save()
        dep_manager = DependencyManager(self.state_dir / "dependencies")
        result = install_meathook(
            game_root,
            dep_manager,
            state_dir=self.state_dir,
            consent=self.consent,
            local_artifact=selected,
            force_repair=force_repair,
            journal=journal,
        )
        if result.state in {"installed", "repaired"}:
            self.session_owner.retire()
            post_probe = probe_meathook(game_root)
            if not post_probe.ok:
                return GameLinkResult(
                    state="failed",
                    message=player_text('launcher.integration', 'game_link_runtime_installation_could_not_be_verified_after_insta', value_0=f'{post_probe.message}'),
                    path=result.path,
                    sha256=result.sha256,
                    ownership=result.ownership,
                    backup_path=result.backup_path,
                )
            self._emit(
                "game_link_installed",
                state=result.state,
                path=result.path,
                sha256=result.sha256,
                ownership=result.ownership,
                backup_path=result.backup_path,
            )
        return result

    def _cached_install(
        self,
        snapshot: RoomSnapshot,
        state: InstallState,
    ) -> IntegratedSetupRecord | None:
        """Return the verified room installation independently of session readiness."""
        if state.state != "already_installed":
            return None

        receipt_path = self.state_dir / "launcher_setup.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = self._manifest_for(snapshot)
        raw_command = receipt.get("adapter_command", ())
        adapter_command = tuple(str(item) for item in raw_command) if isinstance(raw_command, list | tuple) else ()
        raw_injector_command = receipt.get("injector_command", adapter_command)
        injector_command = (
            tuple(str(item) for item in raw_injector_command)
            if isinstance(raw_injector_command, list | tuple) else adapter_command
        )
        raw_required_resources = receipt.get("required_resources", ())
        required_resources = (
            tuple(item for item in raw_required_resources if isinstance(item, dict))
            if isinstance(raw_required_resources, list | tuple) else ()
        )
        return IntegratedSetupRecord(
            manifest_hash=manifest.manifest_hash,
            randomize_dash=bool(receipt.get("randomize_dash", manifest.options["runtime"]["randomize_dash"])),
            generated_mod=str(receipt.get("generated_mod", state.staged_mod)),
            staged_mod=state.staged_mod,
            staged_sha256=str(receipt["staged_sha256"]),
            adapter_state="applied",
            adapter_message=str(receipt.get("adapter_message", player_text('launcher.integration', 'mod_is_already_installed_for_current_room'))),
            adapter_command=adapter_command,
            injector_command=injector_command,
            steam_launch_option=state.steam_launch_option,
            steam_launch_option_diff=str(receipt.get("steam_launch_option_diff", "")),
            installation_mode=str(receipt.get("installation_mode", "automatic")),
            user_confirmed=bool(receipt.get("user_confirmed", False)),
            new_install=False,
            room_zip_sha256=str(receipt.get("room_zip_sha256", receipt["staged_sha256"])),
            generated_sha256=str(receipt.get("generated_sha256", "")),
            mods_path=str(receipt.get("mods_path", "")),
            injector_version=str(receipt.get("injector_version", "")),
            injector_exit_code=(
                receipt.get("injector_exit_code")
                if isinstance(receipt.get("injector_exit_code"), int) else None
            ),
            required_resources=required_resources,
            post_install_verification=str(receipt.get("post_install_verification", "")),
            static_content_digest=str(receipt.get("static_content_digest", manifest.static_content_digest)),
            installed_resources=receipt.get("installed_resources", {}),
            transaction_id=str(receipt.get("transaction_id", "")),
        )

    def installation_transaction_id(self) -> str | None:
        config = self._config()
        journal = self._installation_journal(self._game_root(config))
        return journal.transaction_id if journal.load() else None

    def _installation_journal(self, game_root):
        return InstallJournal(game_root, (self.state_dir / "launcher_setup.json",
                              self.base_workflow.client_config_path(self.application_dir)))

    def execute(self, snapshot: RoomSnapshot, endpoint: str = "", *, retry_injector=False) -> IntegratedSetupRecord:
        admit_slot_data(snapshot.slot_data)
        self._failure_phase = "game_setup"
        config = self._config()
        game_root = self._game_root(config)
        journal = self._installation_journal(game_root)
        pending = journal.load() and journal.phase not in {InstallPhase.COMPLETE, InstallPhase.ROLLED_BACK}
        pre_install_state = self.install_state(snapshot)
        cached = None if pending else self._cached_install(snapshot, pre_install_state)
        if cached is not None:
            return self._prepare_cached(snapshot, endpoint, cached, game_root)
        return self._execute_installation(snapshot, endpoint, config, game_root, pre_install_state, retry=retry_injector)

    def _prepare_cached(self, snapshot, endpoint, cached, game_root):
        from .launcher_core_install import game_write_lock
        with game_write_lock(game_root):
            self.check_cancelled()
            self.ensure_game_link(game_root)
            config = self._config()
            path = self._publish_client_config(endpoint=endpoint or str(config.get("server_address") or ""),
                manifest_hash=cached.manifest_hash, runtime_config=config)
            self._emit("runtime_config_ready", path=str(path))
            self._failure_phase = "campaign_session"
            self.session_owner.prepare(snapshot, config)
        return cached

    def _execute_installation(self, snapshot, endpoint, config, game_root, pre_install_state,
                              *, manual=False, transaction_id=None, retry=False):
        manifest = self._manifest_for(snapshot)
        plan = self._installation_plan(manifest, snapshot, game_root, pre_install_state)
        self._prepared_injector = None
        actions = InstallationActions(
            prepare=lambda journal: self._prepare_installation(plan, journal, manifest),
            inject=lambda: self._inject_installation(plan),
            confirm=self.confirmation,
            finalize=lambda outcome: self._finalize_installation(plan, outcome),
            verify=lambda outcome: self._verify_installation(plan, outcome),
            session=lambda journal: self._installation_session(plan, journal, snapshot, endpoint),
            receipt=lambda outcome: self._installation_receipt(plan, manifest, endpoint, outcome),
            check=self.check_cancelled,
        )
        try:
            outcome = InstallationService().execute(plan, actions, manual=manual, transaction_id=transaction_id, retry=retry)
        except InstallationRecoveryRequired as error:
            if manual:
                raise
            outcome = self._external_recovery_outcome(plan, error)
        journal = plan.journal()
        journal.load()
        return self._installation_record(plan, manifest, journal, outcome)

    def _external_recovery_outcome(self, plan, error):
        from .installation_adapters import InjectorState, injector_state
        from .ui.strings import injector_recovery

        journal = plan.journal()
        journal.load()
        external = journal.phase is InstallPhase.INJECTING or journal.document["evidence"].get("external_uncertain")
        if not external:
            raise error
        state = injector_state(journal.document["evidence"])
        return InstallationOutcome(AdapterResult("manual_install_required", injector_recovery(state is InjectorState.RUNNING, windows=self.platform_name == "windows"),
            details={**error.details, "injector_state": state.value, "installation_mode": "manual_fallback"}))

    def _installation_plan(self, manifest, snapshot, game_root, pre_install_state):
        journal = self._installation_journal(game_root)
        if journal.load() and journal.phase not in {InstallPhase.COMPLETE, InstallPhase.ROLLED_BACK}:
            identity = dict(journal.document["identity"])
            generated = Path(identity["generated_mod"])
            expected_room = (manifest.manifest_hash, manifest.seed_name, manifest.team, manifest.slot)
            if tuple(identity[key] for key in ("manifest_hash", "seed_name", "team", "slot")) != expected_room:
                raise InstallationRecoveryRequired(player_text('launcher.integration', 'pending_installation_belongs_to_another_room'))
        else:
            self._failure_phase = "room_package"
            generated = self._compile_installation(manifest)
            identity = {"manifest_hash": manifest.manifest_hash, "seed_name": manifest.seed_name,
                "team": manifest.team, "slot": manifest.slot,
                "native_generation_fingerprint": snapshot.slot_data["native_generation_fingerprint"],
                "static_content_digest": manifest.static_content_digest,
                "generated_mod": str(generated.resolve()), "generated_sha256": file_digest(generated),
                "executable_sha256": file_digest(game_root / "DOOMEternalx64vk.exe"),
                "new_install": pre_install_state.state == "install_needed",
                "configuration": self._installation_configuration(self._config())}
        return InstallationPlan(game_root, generated, self.state_dir / "launcher_setup.json",
            self.base_workflow.client_config_path(self.application_dir), self.state_dir, identity)

    def _prepare_game_link(self, game_root, journal):
        self._failure_phase = "game_setup"
        config = self._config()
        local_key = "core_runtime_manifest"
        local_val = config.get(local_key)
        local_artifact = Path(str(local_val)).expanduser() if local_val else None
        try:
            game_link = self.ensure_game_link(
                game_root,
                local_artifact=local_artifact,
                force_repair=False,
                journal=journal,
            )
            config = self._config()
            if game_link.state == "needs_repair":
                self._emit(
                    "prerequisite_missing",
                    key="meathook",
                    message=game_link.message,
                    details={"path": game_link.path, "sha256": game_link.sha256, "status": "incompatible"},
                )
                raise RuntimeError(
                    player_text('launcher.integration', 'installed_sentinel_core_runtime_does_not_match_the_selected_dist')
                )
            if game_link.state == "failed":
                raise RuntimeError(game_link.message)
        except PermissionError as error:
            self._emit(
                "prerequisite_missing",
                key="meathook",
                message=player_text('launcher.integration', 'game_link_installation_was_not_approved'),
                details={"status": "missing"},
            )
            raise RuntimeError(
                player_text('launcher.integration', 'game_link_download_was_not_approved_approve_the_download_to_fini')
            ) from error
        except Exception as error:
            if not isinstance(error, RuntimeError | PermissionError):
                self._emit(
                    "prerequisite_missing",
                    key="meathook",
                    message=str(error),
                    details={"status": "error"},
                )
            raise


    def _check_installation_prerequisites(self, game_root):
        config = self._config()
        prereqs = probe_runtime_prerequisites(game_root, self.application_dir, config)
        if not prereqs.ok:
            meathook = prereqs.meathook
            if meathook and not meathook.ok:
                self._emit(
                    "prerequisite_missing",
                    key="meathook",
                    message=meathook.message,
                    details=meathook.details,
                )
                raise RuntimeError(
                    player_text('launcher.integration', 'sentinel_core_runtime_is_not_installed_repair_game_link_before_p')
                )
            failed = [c.message for c in prereqs.checks if not c.ok]
            raise RuntimeError(player_text('launcher.integration', 'runtime_prerequisites_not_met_value_0', value_0=f"{'; '.join(failed)}"))


    def _compile_installation(self, manifest):
        self._emit(
            "room_validated",
            seed_name=manifest.seed_name,
            team=manifest.team,
            slot=manifest.slot,
            randomize_dash=manifest.options["runtime"]["randomize_dash"],
            manifest_hash=manifest.manifest_hash,
        )
        self._emit(
            "mod_building",
            randomize_dash=manifest.options["runtime"]["randomize_dash"],
        )
        decompressor_manager = self._idfile_manager()
        generated = self._room_compiler(
            dependency_manager=decompressor_manager,
            consent=self.consent,
        ).build(
            manifest,
            self.state_dir / "generated_mods",
        )
        return generated

    def _prepare_installation(self, plan, journal, manifest):
        self._prepare_game_link(plan.game_root, journal)
        self._check_installation_prerequisites(plan.game_root)
        self.check_cancelled()
        staged = stage_room_mod(plan.generated, plan.game_root, plan.receipt_path,
            manifest_hash=manifest.manifest_hash, journal=journal,
            legacy_removal_sink=lambda path: self._emit("log", message=player_text('launcher.integration', 'removed_legacy_doom_eternal_archipelago_mod_value_0', value_0=f'{path}')))
        if file_digest(staged) != plan.identity["generated_sha256"]:
            raise RuntimeError(player_text('launcher.integration', 'generated_and_staged_room_mod_hashes_differ'))
        self._emit("mod_staged", path=str(staged), manifest_hash=manifest.manifest_hash)
        self._installation_journal_port = journal
        self._prepared_injector = self._prepare_adapter(self._config(), plan.game_root, journal)
        adapter, executable, _, _ = self._prepared_injector
        if executable is not None:
            journal.document["evidence"]["tool"] = {
                "path": str(executable), "sha256": file_digest(executable),
                "version": adapter.dependency.version, "artifact_sha256": adapter.dependency.artifact_sha256}
            journal.save()

    def _emit_injector(self, kind, **payload):
        try:
            self._emit(kind, **payload)
        except LauncherWorkCancelled:
            pass

    def _inject_installation(self, plan):
        if self._prepared_injector is None:
            journal = plan.journal()
            journal.load()
            self._installation_journal_port = journal
            self._prepared_injector = self._prepare_adapter(self._config(), plan.game_root, journal)
        adapter, executable, steam_option, steam_diff = self._prepared_injector
        if executable is None:
            return InstallationOutcome(adapter, steam_option, steam_diff)
        journal = self._installation_journal_port
        tool = journal.document["evidence"]["tool"]
        if file_digest(executable) != tool["sha256"]:
            raise InstallationRecoveryRequired(player_text('launcher.integration', 'injector_tool_changed'))
        if self.platform_name == "linux":
            self._emit_injector("injector_started", command=[str(executable)])
        result = adapter.run(plan.game_root, prepared=executable, journal=journal)
        if self.platform_name == "linux":
            self._emit_injector("injector_finished", state=result.state, message=result.message,
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if self.platform_name == "windows":
            result.details["installation_mode"] = "windows_injector_assisted"
        return InstallationOutcome(result, steam_option, steam_diff)

    def _installation_session(self, plan, journal, snapshot, endpoint):
        config = self._config()
        if self._installation_configuration(config) != plan.identity["configuration"]:
            raise InstallationRecoveryRequired(player_text('launcher.integration', 'installation_configuration_changed'))
        runtime = journal.document["evidence"].get("selected_core_runtime_manifest")
        if runtime:
            config["selected_core_runtime_manifest"] = runtime
        self._failure_phase = "campaign_session"
        path = self._publish_client_config(endpoint=endpoint or str(config.get("server_address") or ""),
            manifest_hash=plan.identity["manifest_hash"], runtime_config=config, journal=journal)
        self._emit("runtime_config_ready", path=str(path))
        self.session_owner.prepare(snapshot, config)

    @staticmethod
    def _installation_configuration(config):
        return {key: config.get(key) for key in CLIENT_CONFIG_FIELDS - {
            "selected_core_runtime_manifest", "server_address", "seed_manifest_hash"}}

    def _finalize_installation(self, plan, outcome):
        self._failure_phase = "game_setup"
        journal = plan.journal()
        journal.load()
        result = finalize_mod_installation(plan.game_root, plan.game_root / "Mods" / plan.generated.name,
                                           outcome.adapter, event_sink=self._emit, journal=journal)
        return InstallationOutcome(result, outcome.steam_option, outcome.steam_diff)

    def _verify_installation(self, plan, outcome):
        staged = plan.game_root / "Mods" / plan.generated.name
        self._verify_room_package(staged, plan.identity)
        result = verify_mod_installation(plan.game_root, staged, outcome.adapter,
                                         platform_name=self.platform_name, event_sink=self._emit)
        return InstallationOutcome(result, outcome.steam_option, outcome.steam_diff)

    def _installation_record(self, plan, manifest, journal, outcome):
        generated = plan.generated
        staged = plan.game_root / "Mods" / generated.name
        game_root = plan.game_root
        staged_sha256 = plan.identity["generated_sha256"]
        generated_sha256 = staged_sha256
        adapter, steam_option, steam_diff = outcome.adapter, outcome.steam_option, outcome.steam_diff
        details = adapter.details
        installation_mode = cast(str, details.get("installation_mode", "automatic"))
        user_confirmed = cast(bool, details.get("user_confirmed", False))
        required_resources = tuple(cast(Sequence[dict[str, object]], details.get("required_resources", ())))
        record = IntegratedSetupRecord(
            manifest_hash=manifest.manifest_hash,
            randomize_dash=manifest.options["runtime"]["randomize_dash"],
            generated_mod=str(generated.resolve()),
            staged_mod=str(staged.resolve()),
            staged_sha256=staged_sha256,
            adapter_state=adapter.state,
            adapter_message=adapter.message,
            adapter_command=adapter.command,
            injector_command=adapter.command,
            steam_launch_option=steam_option,
            steam_launch_option_diff=steam_diff,
            installation_mode=installation_mode,
            user_confirmed=user_confirmed,
            new_install=(
                plan.identity["new_install"]
                and adapter.state in {"applied", "manual_install_required"}
            ),
            room_zip_sha256=staged_sha256,
            generated_sha256=generated_sha256,
            mods_path=str((game_root / "Mods").resolve()),
            injector_version=cast(str, details.get("injector_version", "")),
            injector_exit_code=adapter.returncode,
            required_resources=required_resources,
            post_install_verification=cast(str, details.get("post_install_verification", "")),
            static_content_digest=manifest.static_content_digest,
            installed_resources=cast(dict[str, str], details.get("installed_resources", {})),
            adapter_details=dict(adapter.details),
            transaction_id=journal.transaction_id,
        )
        return record

    def _installation_receipt(self, plan, manifest, endpoint, outcome):
        journal = plan.journal()
        journal.load()
        record = self._installation_record(plan, manifest, journal, outcome)
        return {**asdict(record), "endpoint": endpoint, "seed_name": manifest.seed_name,
                "team": manifest.team, "slot": manifest.slot,
                "static_content_digest": manifest.static_content_digest}

    @staticmethod
    def _verify_room_package(staged, identity):
        actual_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
        if actual_hash != identity["generated_sha256"]:
            raise RuntimeError(player_text('launcher.integration', 'post_install_room_mod_hash_validation_failed'))
        try:
            with zipfile.ZipFile(staged) as package:
                installed_manifest = json.loads(package.read("seed_manifest.json"))
            if installed_manifest.get("manifest_hash") != identity["manifest_hash"]:
                raise ValueError(player_text('launcher.integration', 'room_identity_differs'))
            if identity["static_content_digest"] and installed_manifest.get("static_content_digest") != identity["static_content_digest"]:
                raise ValueError(player_text('launcher.integration', 'room_content_identity_differs'))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            raise RuntimeError(player_text('launcher.integration', 'post_install_room_mod_validation_failed_value_0', value_0=f'{error}')) from error

    def uninstall(self, snapshot: RoomSnapshot) -> UninstallResult:
        """Remove only receipt-proven room package, then run platform injector once."""
        manifest = self._manifest_for(snapshot)
        config = self._config()
        game_root = self._game_root(config)
        with installation_write(game_root):
            running = detect_doom_processes()
            if running:
                names = ", ".join(str(item.get("name", "unknown")) for item in running)
                raise RuntimeError(player_text('launcher.integration', 'cannot_uninstall_room_mod_while_game_runtime_is_running_value_0', value_0=f'{names}'))

            receipt_path = self.state_dir / "launcher_setup.json"
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if not isinstance(receipt, dict):
                    raise ValueError(player_text('launcher.integration', 'receipt_must_contain_an_object'))
                if str(receipt.get("adapter_state", "")) != "applied":
                    raise ValueError(player_text('launcher.integration', 'receipt_does_not_prove_an_applied_room_mod'))
                if str(receipt.get("manifest_hash", "")) != manifest.manifest_hash:
                    raise ValueError(player_text('launcher.integration', 'receipt_belongs_to_another_room'))
                staged_input = Path(str(receipt["staged_mod"])).expanduser()
                if staged_input.is_symlink():
                    raise ValueError(player_text('launcher.integration', 'receipt_package_is_a_symbolic_link'))
                staged = staged_input.resolve()
                mods_dir = (game_root / "Mods").resolve()
                if staged.parent != mods_dir or staged.suffix.casefold() != ".zip":
                    raise ValueError(player_text('launcher.integration', 'receipt_package_is_outside_configured_game_mods_folder'))
                if staged.is_symlink() or not staged.is_file():
                    raise ValueError(player_text('launcher.integration', 'receipt_package_is_missing'))
                expected_sha256 = str(receipt["staged_sha256"])
                actual_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
                if actual_sha256 != expected_sha256:
                    raise ValueError(player_text('launcher.integration', 'receipt_package_hash_does_not_match'))
                with zipfile.ZipFile(staged) as package:
                    package_manifest = json.loads(package.read("seed_manifest.json"))
                if not isinstance(package_manifest, dict) or package_manifest.get("manifest_hash") != manifest.manifest_hash:
                    raise ValueError(player_text('launcher.integration', 'receipt_package_manifest_does_not_match_current_room'))
            except (OSError, RuntimeError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
                raise RuntimeError(player_text('launcher.integration', 'cannot_uninstall_room_mod_safely_value_0', value_0=f'{error}')) from error

            self.check_cancelled()
            quarantine_dir = self.state_dir / "uninstall-quarantine"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            quarantine = quarantine_dir / f"{staged.name}.{time.time_ns()}.zip"

            receipt.update(
                {
                    "adapter_state": "uninstall_attention",
                    "adapter_message": player_text('launcher.integration', 'room_mod_uninstall_is_in_progress_or_requires_attention'),
                    "installation_mode": "uninstall",
                    "uninstall_quarantine": str(quarantine),
                }
            )
            temporary = receipt_path.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                self._publish_receipt(temporary, receipt_path, operation="uninstall_attention_publish")
            except Exception as error:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise RuntimeError(player_text('launcher.integration', 'could_not_record_uninstall_attention_state_value_0', value_0=f'{error}')) from error

            def failed_result(message: str, injector_state: str = "", command: tuple[str, ...] = ()) -> UninstallResult:
                receipt.update(
                    {
                        "adapter_state": "uninstall_failed",
                        "adapter_message": message,
                        "adapter_command": list(command),
                        "installation_mode": "uninstall",
                        "uninstall_quarantine": str(quarantine),
                    }
                )
                try:
                    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    self._publish_receipt(temporary, receipt_path, operation="uninstall_failed_publish")
                except Exception as error:
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
                    message = player_text('launcher.integration', 'value_0_receipt_update_failed_value_1', value_0=f'{message}', value_1=f'{error}')
                return UninstallResult(
                    state="attention",
                    manifest_hash=manifest.manifest_hash,
                    staged_mod=str(staged),
                    quarantine_path=str(quarantine),
                    adapter_state="uninstall_failed",
                    injector_state=injector_state,
                    message=message,
                    attention=True,
                )

            self._emit(
                "uninstall_progress",
                phase="attention_recorded",
                manifest_hash=manifest.manifest_hash,
                staged_mod=str(staged),
            )

            try:
                shutil.copy2(staged, quarantine)
                if hashlib.sha256(quarantine.read_bytes()).hexdigest() != expected_sha256:
                    raise RuntimeError(player_text('launcher.integration', 'quarantine_package_hash_does_not_match_receipt'))
                staged.unlink()
                if staged.exists():
                    raise RuntimeError(player_text('launcher.integration', 'verified_room_package_remains_after_removal'))
            except Exception as error:
                raise RuntimeError(player_text('launcher.integration', 'room_package_removal_was_not_completed_safely_value_0', value_0=f'{error}')) from error
            self._emit(
                "uninstall_progress",
                phase="package_quarantined",
                manifest_hash=manifest.manifest_hash,
                staged_mod=str(staged),
                quarantine_path=str(quarantine),
            )

            try:
                self._emit(
                    "uninstall_progress",
                    phase="injector_started",
                    manifest_hash=manifest.manifest_hash,
                    staged_mod=str(staged),
                )
                injector, steam_option, steam_diff = self._run_injector(
                    config,
                    game_root,
                    operation="uninstall",
                    staged_mod=staged.name,
                )
            except Exception as error:
                return failed_result(player_text('launcher.integration', 'room_package_removed_but_injector_cleanup_did_not_complete_value', value_0=f'{error}'))
            self._emit(
                "uninstall_progress",
                phase="injector_finished",
                manifest_hash=manifest.manifest_hash,
                injector_state=injector.state,
                returncode=injector.returncode,
            )
            if injector.state != "applied":
                return failed_result(
                    player_text('launcher.integration', 'room_package_removed_but_injector_cleanup_failed_value_0', value_0=f'{injector.message}'),
                    injector.state,
                    injector.command,
                )
            try:
                remaining = []
                for candidate in sorted((game_root / "Mods").resolve().rglob("*.zip")):
                    if not candidate.is_file():
                        continue
                    try:
                        with zipfile.ZipFile(candidate) as package:
                            candidate_manifest = json.loads(package.read("seed_manifest.json"))
                    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile):
                        continue
                    if isinstance(candidate_manifest, dict) and candidate_manifest.get("manifest_hash") == manifest.manifest_hash:
                        remaining.append(candidate)
            except OSError as error:
                return failed_result(
                    player_text('launcher.integration', 'injector_completed_but_room_package_removal_could_not_be_verifie', value_0=f'{error}'),
                    injector.state,
                    injector.command,
                )
            if remaining:
                locations = ", ".join(str(path) for path in remaining)
                return failed_result(
                    player_text('launcher.integration', 'injector_completed_but_managed_room_package_remains_at_value_0', value_0=f'{locations}'),
                    injector.state,
                    injector.command,
                )

            try:
                hotkey_state = game_root / "base" / "ap_queue" / "ammo_refill_hotkey.state"
                if hotkey_state.is_file():
                    hotkey_state.unlink()
            except OSError:
                pass
            cleanup_stale_doom_config_bind(config, is_game_running=False)

            receipt.update(
                {
                    "adapter_state": "uninstalled",
                    "adapter_message": player_text('launcher.integration', 'room_mod_intentionally_uninstalled'),
                    "adapter_command": list(injector.command),
                    "installation_mode": "uninstalled",
                    "steam_launch_option": steam_option or str(receipt.get("steam_launch_option", "")),
                    "steam_launch_option_diff": steam_diff or str(receipt.get("steam_launch_option_diff", "")),
                    "uninstall_quarantine": str(quarantine),
                }
            )
            try:
                temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                self._publish_receipt(temporary, receipt_path, operation="uninstall_receipt_publish")
            except Exception as error:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                return UninstallResult(
                    state="attention",
                    manifest_hash=manifest.manifest_hash,
                    staged_mod=str(staged),
                    quarantine_path=str(quarantine),
                    adapter_state="uninstall_attention",
                    injector_state=injector.state,
                    message=player_text('launcher.integration', 'room_package_removed_and_injector_completed_but_receipt_update_f', value_0=f'{error}'),
                    attention=True,
                )
            return UninstallResult(
                state="uninstalled",
                manifest_hash=manifest.manifest_hash,
                staged_mod=str(staged),
                quarantine_path=str(quarantine),
                adapter_state="uninstalled",
                injector_state=injector.state,
                message=player_text('launcher.integration', 'room_mod_intentionally_uninstalled'),
            )

    def confirm_manual_installation(self, snapshot: RoomSnapshot, endpoint: str = "", *,
                                    transaction_id: str | None = None) -> IntegratedSetupRecord:
        admit_slot_data(snapshot.slot_data)
        config = self._config()
        game_root = self._game_root(config)
        return self._execute_installation(snapshot, endpoint, config, game_root, self.install_state(snapshot),
            manual=True, transaction_id=transaction_id)


class RoomSetupCoordinator:
    """Serialize setup per room and expose explicit retry without duplicate reconnect work."""

    def __init__(
        self,
        workflow: IntegratedLaunchWorkflow,
        event_sink: EventSink,
        result_sink: Callable[[IntegratedSetupRecord, int], None],
        workers: LauncherWorkers,
        interactions: LauncherInteractions,
    ):
        self.workflow = workflow
        self.event_sink = event_sink
        self.result_sink = result_sink
        self._state_lock = threading.Lock()
        self.workers = workers
        self.interactions = interactions
        self._active: set[tuple[object, ...]] = set()
        self._completed: set[tuple[object, ...]] = set()
        self._uninstall_active: int | None = None
        self._last_event: dict[str, object] | None = None
        self._room_active = False

    @property
    def last_event(self) -> dict[str, object] | None:
        with self._state_lock:
            return deepcopy(self._last_event)

    @property
    def current_event(self) -> dict[str, object] | None:
        with self._state_lock:
            return deepcopy(self._last_event) if self._room_active else None

    def invalidate(self) -> None:
        with self._state_lock:
            self.workers.invalidate()
            self._room_active = False
            self._active.clear()
            self._uninstall_active = None

    def observe(self, event: dict[str, object]) -> bool:
        if event.get("type") != "connected":
            return False
        with self._state_lock:
            if self._last_event is not None and self.room_key(self._last_event) != self.room_key(event):
                self.workers.invalidate()
                self._active.clear()
                self._uninstall_active = None
            self._last_event = deepcopy(event)
            self._room_active = True
        return True

    @staticmethod
    def room_key(event: dict[str, object]) -> tuple[object, ...]:
        slot_data = event.get("slot_data", {})
        fingerprint = slot_data.get("native_generation_fingerprint") if isinstance(slot_data, dict) else None
        return (
            event.get("seed_name"),
            event.get("team"),
            event.get("slot"),
            fingerprint if isinstance(fingerprint, str) else json.dumps(slot_data, sort_keys=True),
        )

    def _receipt_context(self) -> dict[str, object]:
        receipt_path = self.workflow.state_dir / "launcher_setup.json"
        context: dict[str, object] = {"receipt_exists": receipt_path.is_file()}
        if not receipt_path.is_file():
            return context
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                return context
            adapter_state = receipt.get("adapter_state")
            if isinstance(adapter_state, str):
                context["adapter_state"] = adapter_state
            staged_mod = receipt.get("staged_mod")
            if isinstance(staged_mod, str) and staged_mod:
                context["package_exists"] = Path(staged_mod).is_file()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return context

    def _room_event_context(self, event: dict[str, object]) -> dict[str, object]:
        context: dict[str, object] = {
            "seed_name": event.get("seed_name"),
            "team": event.get("team"),
            "slot": event.get("slot"),
            "room_state": event.get("room_state", "connected"),
        }
        if "explicit" in event:
            context["explicit"] = bool(event["explicit"])
        if event.get("source") is not None:
            context["source"] = event["source"]
        context.update(self._receipt_context())
        return context

    def _emit_room_event(
        self,
        kind: str,
        event: dict[str, object],
        **payload: object,
    ) -> None:
        context = self._room_event_context(event)
        context["launcher_job_generation"] = self.workers.generation
        context.update(payload)
        self.event_sink(kind, context)

    def submit(self, event: dict[str, object], *, force: bool = False, generation: int | None = None) -> bool:
        if event.get("type") != "connected":
            return False
        if generation is not None and not self.workers.accepts(generation):
            return False
        event = deepcopy(event)
        self.observe(event)
        key = self.room_key(event)
        generation = self.workers.generation if generation is None else generation
        active_key = (generation, key)
        with self._state_lock:
            if active_key in self._active or (key in self._completed and not force):
                return False
            if force:
                self._completed.discard(key)
            self._active.add(active_key)
        self._emit_room_event(
            'ROOM_INSTALL_ACTION_REQUESTED',
            event,
            explicit=bool(event.get("explicit", not force)),
            source=event.get("source", "coordinator"),
        )
        self.event_sink("setup_started", {"seed_name": event.get("seed_name"), "launcher_job_generation": generation})

        def worker(job: LauncherJob) -> None:
            workflow = self.workflow

            def emit(kind, payload):
                self.event_sink(kind, job.event(payload))

            def room_event(kind, source, **payload):
                job.check()
                self._emit_room_event(kind, source, launcher_job_generation=job.generation, **payload)

            try:
                workflow = self.workflow.for_job(job, self.event_sink, self.interactions.for_job(job))
                try:
                    snapshot = RoomSnapshot.from_event(event)
                except Exception as error:
                    emit(
                        "setup_failed",
                        setup_failure_payload(error, phase="room_snapshot"),
                    )
                    room_event(
                        'ROOM_INSTALL_RESULT',
                        event,
                        result="failure",
                        state="failed",
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                    return
                room_event(
                    'ROOM_INSTALL_PLAN',
                    event,
                    plan="compile_stage_inject",
                    adapter_state=self._receipt_context().get("adapter_state", ""),
                )
                room_event(
                    'ROOM_INSTALL_TRANSITION',
                    event,
                    phase="starting",
                    state="installing",
                )
                record = workflow.execute(
                    snapshot,
                    str(event.get("endpoint") or ""),
                    retry_injector=force,
                )
                room_event(
                    'ROOM_INSTALL_TRANSITION',
                    event,
                    phase="finished",
                    state=record.adapter_state,
                    adapter_state=record.adapter_state,
                    manifest_hash=record.manifest_hash,
                )
                if record.adapter_state == "manual_install_required":
                    self.result_sink(record, job.generation)
                    emit(
                        "manual_install_required",
                        {
                            "manifest_hash": record.manifest_hash,
                            "new_install": record.new_install,
                            "message": record.adapter_message,
                            "recovery_required": record.adapter_details.get("recovery_required", False),
                            "injector_state": record.adapter_details.get("injector_state", ""),
                            "guide_section": player_text('launcher.integration', 'windows_manual_mod_installer'),
                            "guide_url": WINDOWS_MANUAL_INSTALL_GUIDE_URL,
                        },
                    )
                    room_event(
                        'ROOM_INSTALL_RESULT',
                        event,
                        result="attention",
                        state=record.adapter_state,
                        adapter_state=record.adapter_state,
                        manifest_hash=record.manifest_hash,
                        message=record.adapter_message,
                    )
                    return
                if record.adapter_state != "applied":
                    raise RuntimeError(record.adapter_message or player_text('launcher.integration', 'mod_setup_was_not_applied'))
                with self._state_lock:
                    job.check()
                    self._completed.add(key)
                self.result_sink(record, job.generation)
                emit(
                    "setup_ready",
                    {
                        "manifest_hash": record.manifest_hash,
                        "randomize_dash": record.randomize_dash,
                        "adapter_state": record.adapter_state,
                        "new_install": record.new_install,
                        "message": record.adapter_message,
                        "steam_launch_option": record.steam_launch_option,
                    },
                )
                room_event(
                    'ROOM_INSTALL_RESULT',
                    event,
                    result="success",
                    state=record.adapter_state,
                    adapter_state=record.adapter_state,
                    manifest_hash=record.manifest_hash,
                    new_install=record.new_install,
                )
            except LauncherWorkCancelled:
                raise
            except Exception as error:
                emit(
                    "setup_failed",
                    setup_failure_payload(
                        error,
                        phase=str(getattr(workflow, "_failure_phase", "")),
                    ),
                )
                room_event(
                    'ROOM_INSTALL_RESULT',
                    event,
                    result="failure",
                    state="failed",
                    error_type=type(error).__name__,
                    message=str(error),
                )
            finally:
                with self._state_lock:
                    self._active.discard(active_key)

        accepted = self.workers.submit(('setup', key), worker, generation=generation)
        if not accepted:
            with self._state_lock:
                self._active.discard(active_key)
        return accepted

    def start(self, *, force: bool = False, generation: int | None = None) -> bool:
        with self._state_lock:
            if not self._room_active:
                return False
            event = dict(self._last_event) if self._last_event else None
        return self.submit(event, force=force, generation=generation) if event else False

    def retry(self) -> bool:
        with self._state_lock:
            if not self._room_active:
                return False
            event = dict(self._last_event) if self._last_event else None
        return self.submit(event, force=True) if event else False

    def submit_uninstall(self, event: dict[str, object], *, generation: int | None = None) -> bool:
        """Run uninstall on coordinator worker, sharing setup serialization lock."""
        if event.get("type") != "connected":
            return False
        if generation is not None and not self.workers.accepts(generation):
            return False
        event = deepcopy(event)
        self.observe(event)
        key = self.room_key(event)
        generation = self.workers.generation if generation is None else generation
        with self._state_lock:
            if self._uninstall_active == generation:
                return False
            self._uninstall_active = generation
        self._emit_room_event(
            'ROOM_UNINSTALL_ACTION_REQUESTED',
            event,
            explicit=True,
            source=event.get("source", "coordinator"),
        )

        def worker(job: LauncherJob) -> None:
            workflow = self.workflow

            def emit(kind, payload):
                self.event_sink(kind, job.event(payload))

            def room_event(kind, source, **payload):
                job.check()
                self._emit_room_event(kind, source, launcher_job_generation=job.generation, **payload)

            try:
                workflow = self.workflow.for_job(job, self.event_sink, self.interactions.for_job(job))
                emit(
                    "uninstall_queued",
                    {},
                )
                snapshot = RoomSnapshot.from_event(event)
                manifest = workflow._manifest_for(snapshot)
                room_event(
                    'ROOM_UNINSTALL_PLAN',
                    event,
                    plan="quarantine_remove_injector_cleanup",
                    adapter_state=self._receipt_context().get("adapter_state", ""),
                )
                room_event(
                    'ROOM_UNINSTALL_TRANSITION',
                    event,
                    phase="starting",
                    state="uninstalling",
                    manifest_hash=manifest.manifest_hash,
                )
                emit(
                    "uninstall_started",
                    {"manifest_hash": manifest.manifest_hash},
                )
                result = workflow.uninstall(snapshot)
                room_event(
                    'ROOM_UNINSTALL_TRANSITION',
                    event,
                    phase="finished",
                    state=result.state,
                    adapter_state=result.adapter_state,
                    manifest_hash=result.manifest_hash,
                )
                if result.state == "uninstalled":
                    with self._state_lock:
                        job.check()
                        self._completed.discard(key)
                emit(
                    "uninstall_complete" if result.state == "uninstalled" else "uninstall_attention",
                    {**asdict(result), "manifest_hash": result.manifest_hash},
                )
                room_event(
                    'ROOM_UNINSTALL_RESULT',
                    event,
                    result="success" if result.state == "uninstalled" else "attention",
                    state=result.state,
                    adapter_state=result.adapter_state,
                    manifest_hash=result.manifest_hash,
                    message=result.message,
                )
            except LauncherWorkCancelled:
                raise
            except Exception as error:
                emit(
                    "uninstall_failed",
                    {
                        "state": "attention",
                        "attention": True,
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
                )
                room_event(
                    'ROOM_UNINSTALL_RESULT',
                    event,
                    result="failure",
                    state="attention",
                    error_type=type(error).__name__,
                    message=str(error),
                )
            finally:
                with self._state_lock:
                    if self._uninstall_active == generation:
                        self._uninstall_active = None

        accepted = self.workers.submit(('uninstall', key), worker, generation=generation)
        if not accepted:
            with self._state_lock:
                if self._uninstall_active == generation:
                    self._uninstall_active = None
        return accepted
