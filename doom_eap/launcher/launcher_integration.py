"""Room-bound setup services for standalone DOOM Eternal launcher."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .launcher_core import LaunchWorkflow, RoomCompiler, RoomSnapshot, SeedManifest
from .launcher_platform import (
    IDFILE_DECOMPRESSOR_LINUX,
    IDFILE_DECOMPRESSOR_WINDOWS,
    LINUX_MOD_INJECTOR,
    WINDOWS_MOD_INJECTOR,
    AdapterResult,
    DependencyManager,
    GameLinkResult,
    LinuxModManagerAdapter,
    SteamLaunchOptionsManager,
    WindowsModInjectorAdapter,
    cleanup_stale_doom_config_bind,
    detect_doom_processes,
    install_meathook,
    idfile_decompressor_spec,
    probe_meathook,
    probe_runtime_prerequisites,
    publish_file,
    stage_room_mod,
    verify_linux_mod_installation,
)

EventSink = Callable[[str, dict[str, object]], None]
ConsentCallback = Callable[[object], bool]
ConfirmationCallback = Callable[[], bool]

ROOM_PACKAGE_FAILURE_TITLE = "ROOM PACKAGE NEEDS ATTENTION"
ROOM_PACKAGE_FAILURE_MESSAGE = (
    "This room package could not be prepared with the current launcher. "
    "Rebuild the room package and try again."
)
ROOM_PACKAGE_FAILURE_ACTION = "REBUILD ROOM PACKAGE"
APPLICATION_PACKAGE_FAILURE_TITLE = "APPLICATION PACKAGE INCOMPLETE"
APPLICATION_PACKAGE_FAILURE_MESSAGE = (
    "The application package is incomplete or corrupted. "
    "Re-extract the full DOOM Eternal Archipelago release package."
)
APPLICATION_PACKAGE_FAILURE_ACTION = "RE-EXTRACT RELEASE PACKAGE"
MOD_BUILDING_TOOL_UNAVAILABLE_MESSAGE = (
    "A required mod-building tool could not be prepared. "
    "Check your connection and try rebuilding the Room Package."
)
GAME_SETUP_FAILURE_TITLE = "GAME SETUP NEEDS ATTENTION"
GAME_SETUP_FAILURE_MESSAGE = (
    "Game setup could not be completed. Repair game integration and try again."
)
GAME_SETUP_FAILURE_ACTION = "REPAIR GAME INTEGRATION"
INSTALLED_PACKAGE_FAILURE_TITLE = "ROOM PACKAGE NEEDS UPDATE"
INSTALLED_PACKAGE_FAILURE_MESSAGE = (
    "This room's installed package needs an update. Update the room package and try again."
)
INSTALLED_PACKAGE_FAILURE_ACTION = "UPDATE ROOM PACKAGE"
ROOM_PACKAGE_INCOMPATIBLE_TITLE = "ROOM PACKAGE NEEDS REBUILDING"
ROOM_PACKAGE_INCOMPATIBLE_MESSAGE = (
    "This room package was built with an older DOOM Eternal APWorld. "
    "Rebuild it with the current launcher and try again."
)
ROOM_PACKAGE_INCOMPATIBLE_ACTION = "REBUILD ROOM PACKAGE"

UNINSTALL_OWNED_STATES = frozenset({
    "uninstall_in_progress",
    "uninstall_attention",
    "uninstall_failed",
    "uninstalled",
})


def classify_setup_failure(error: BaseException, *, phase: str = "") -> str:
    """Classify setup failures without weakening any validation boundary."""
    text = f"{type(error).__name__}: {error}".casefold()
    app_tokens = (
        "component=map_package",
        "release package is incomplete",
        "application package is incomplete",
        "file=content/maps",
        "content/maps value=missing",
        "region_topology.json",
        "global_runtime.json",
        "options_schema.json missing",
        "region topology",
        "content catalog",
    )
    if any(token in text for token in app_tokens):
        return "application_package"
    if phase in {"room_snapshot", "manifest", "room_package"}:
        return "room_package"
    room_tokens = (
        "contract",
        "capability",
        "schema",
        "manifest",
        "placement",
        "room package",
        "room mod",
        "compiler",
        "release package",
        "assembled room",
        "location id",
        "idfiledecompressor",
        "mod-building tool",
    )
    if any(token in text for token in room_tokens):
        return "room_package"
    return "game_setup"


def setup_failure_payload(error: BaseException, *, phase: str = "") -> dict[str, object]:
    """Return user routing metadata alongside untouched technical failure data."""
    raw_message = str(error) or type(error).__name__
    failure_domain = classify_setup_failure(error, phase=phase)
    tool_unavailable = (
        "idfiledecompressor" in raw_message.casefold()
        or "mod-building tool" in raw_message.casefold()
    )
    if failure_domain == "application_package":
        recovery_action = "reinstall_application"
        user_title = APPLICATION_PACKAGE_FAILURE_TITLE
        user_message = APPLICATION_PACKAGE_FAILURE_MESSAGE
        user_action = APPLICATION_PACKAGE_FAILURE_ACTION
    elif failure_domain == "room_package":
        recovery_action = "rebuild_room_package"
        incompatible_tokens = (
            "contract is unsupported",
            "unsupported capabilities",
            "schema is unsupported",
            "revision is unsupported",
            "bridge_protocol is incompatible",
        )
        if any(token in raw_message.casefold() for token in incompatible_tokens):
            user_title = ROOM_PACKAGE_INCOMPATIBLE_TITLE
            user_message = ROOM_PACKAGE_INCOMPATIBLE_MESSAGE
            user_action = ROOM_PACKAGE_INCOMPATIBLE_ACTION
        elif tool_unavailable:
            user_title = ROOM_PACKAGE_FAILURE_TITLE
            user_message = MOD_BUILDING_TOOL_UNAVAILABLE_MESSAGE
            user_action = ROOM_PACKAGE_FAILURE_ACTION
        else:
            user_title = ROOM_PACKAGE_FAILURE_TITLE
            user_message = ROOM_PACKAGE_FAILURE_MESSAGE
            user_action = ROOM_PACKAGE_FAILURE_ACTION
    else:
        recovery_action = "repair_game_integration"
        user_title = GAME_SETUP_FAILURE_TITLE
        user_message = GAME_SETUP_FAILURE_MESSAGE
        user_action = GAME_SETUP_FAILURE_ACTION
    return {
        "error_type": type(error).__name__,
        "message": raw_message,
        "raw_message": raw_message,
        "technical_error_type": type(error).__name__,
        "technical_message": raw_message,
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
        "stage": phase or "setup",
        "failure_domain": failure_domain,
        "recovery_action": recovery_action,
        "user_title": user_title,
        "user_message": user_message,
        "user_action": user_action,
    }


def installed_package_issue_payload(reason: str) -> dict[str, object]:
    """Return routing metadata for receipt-backed package verification failures."""
    return {
        "failure_domain": "installed_room_package",
        "recovery_action": "update_room_package",
        "user_title": INSTALLED_PACKAGE_FAILURE_TITLE,
        "user_message": INSTALLED_PACKAGE_FAILURE_MESSAGE,
        "user_action": INSTALLED_PACKAGE_FAILURE_ACTION,
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


@dataclass(frozen=True)
class InstallState:
    state: str
    manifest_hash: str
    staged_mod: str = ""
    steam_launch_option: str = ""
    reason: str = ""
    readiness: str = "ready"
    readiness_reason: str = ""


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
    ):
        self.base_workflow = LaunchWorkflow()
        self.application_dir = application_dir
        self.state_dir = state_dir
        self.data_dir = data_dir or state_dir.parent / "data"
        self.config_path = config_path
        self.platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
        self.event_sink = event_sink or (lambda _kind, _payload: None)
        self.consent = consent or (lambda _spec: False)
        self.confirmation = confirmation or (lambda: False)
        self.uninstall_confirmation = uninstall_confirmation or (lambda: False)
        self._failure_phase = "game_setup"

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
        room_compiler = self._room_compiler()
        return self.base_workflow.manifest_for(
            snapshot,
            static_content_digest=room_compiler.static_content_digest,
        )

    def _emit(self, kind: str, **payload: object) -> None:
        self.event_sink(kind, payload)

    def _config(self) -> dict[str, object]:
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("launcher configuration must contain an object")
        return document

    @staticmethod
    def _game_root(config: dict[str, object]) -> Path:
        base = Path(str(config.get("doom_base_dir", ""))).expanduser().resolve()
        root = base.parent if base.name.casefold() == "base" else base
        if not (root / "DOOMEternalx64vk.exe").is_file() or not (root / "base").is_dir():
            raise ValueError(f"invalid configured DOOM Eternal installation: {root}")
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
            (plan.diff or "No change required.\n") + ("\n" if plan.diff else ""),
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
                f"idFileDeCompressor is unavailable on unsupported platform "
                f"{self.platform_name}; no pinned codec artifact is available"
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
            raise RuntimeError(message)
        return manager

    def repair_idfile_decompressor(self):
        """Acquire selected verified codec after explicit repair consent."""
        spec = idfile_decompressor_spec(self.platform_name)
        manager = self._idfile_manager()
        if spec is None:
            raise RuntimeError(f"idFileDeCompressor is unavailable on platform {self.platform_name}")
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
        receipt_path = self.state_dir / "launcher_setup.json"
        if not receipt_path.is_file():
            return InstallState("install_needed", manifest.manifest_hash, reason="no launcher install record")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            staged = Path(str(receipt["staged_mod"])).resolve()
            expected_sha = str(receipt["staged_sha256"])
            recorded_manifest = str(receipt["manifest_hash"])
            adapter_state = str(receipt.get("adapter_state", ""))
            windows_confirmed = bool(receipt.get("windows_installation_confirmed", False))
            steam_option = str(receipt.get("steam_launch_option", ""))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return InstallState("update_required", manifest.manifest_hash, reason=f"invalid install record: {error}")
        if adapter_state in UNINSTALL_OWNED_STATES:
            if recorded_manifest != manifest.manifest_hash:
                return InstallState(
                    "install_needed",
                    manifest.manifest_hash,
                    reason=f"previous room mod has uninstall state {adapter_state}",
                )
            if adapter_state != "uninstalled":
                reason = str(receipt.get("adapter_message") or "Room mod uninstall requires attention.")
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
                reason="room mod was intentionally uninstalled",
            )
        if recorded_manifest != manifest.manifest_hash:
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, "installed manifest belongs to another room or build")
        recorded_static_digest = str(receipt.get("static_content_digest", ""))
        if recorded_static_digest and recorded_static_digest != manifest.static_content_digest:
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, "installed package static content is out of date")
        if not staged.is_file():
            return InstallState("update_required", manifest.manifest_hash, reason="recorded installed package is missing")
        if hashlib.sha256(staged.read_bytes()).hexdigest() != expected_sha:
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, "installed package hash does not match record")
        if not RoomCompiler.validate_cached_package(staged, manifest):
            return InstallState("update_required", manifest.manifest_hash, str(staged), steam_option, "installed package embedded content identity does not match current room")
        if adapter_state != "applied":
            return InstallState("install_needed", manifest.manifest_hash, reason=f"previous install state is {adapter_state or 'unknown'}")

        generated_path = Path(str(receipt.get("generated_mod", ""))).expanduser()
        generated_sha = str(receipt.get("generated_sha256", ""))
        room_zip_sha = str(receipt.get("room_zip_sha256", expected_sha))
        if not generated_path.is_file() or not generated_sha:
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason="install record lacks generated room ZIP evidence",
            )
        try:
            generated_actual = hashlib.sha256(generated_path.read_bytes()).hexdigest()
        except OSError as error:
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason=f"generated room ZIP evidence unavailable: {error}",
            )
        if generated_actual != generated_sha or expected_sha != room_zip_sha or generated_sha != expected_sha:
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason="generated and staged room ZIP hashes differ",
            )
        if not RoomCompiler.validate_cached_package(generated_path, manifest):
            return InstallState(
                "update_required", manifest.manifest_hash, str(staged), steam_option,
                reason="generated cache embedded content identity does not match current room",
            )

        # Room package is verified installed. Now check live runtime prerequisites.
        try:
            config = self._config()
            game_root = self._game_root(config)
            if self.platform_name == "linux":
                raw_command = receipt.get("injector_command", receipt.get("adapter_command", ()))
                raw_resources = receipt.get("required_resources", ())
                if (
                    str(receipt.get("post_install_verification", "")) != "verified"
                    or not str(receipt.get("injector_version", ""))
                    or not isinstance(raw_command, (list, tuple)) or not raw_command
                    or receipt.get("injector_exit_code") != 0
                    or not isinstance(raw_resources, (list, tuple))
                    or len(raw_resources) != 4
                    or any(not isinstance(item, dict) or item.get("status") != "verified" for item in raw_resources)
                    or str(receipt.get("mods_path", "")) != str((game_root / "Mods").resolve())
                ):
                    return InstallState(
                        "already_installed", manifest.manifest_hash, str(staged), steam_option,
                        readiness="blocked", readiness_reason="Linux install evidence is incomplete",
                    )
                verify_linux_mod_installation(game_root, staged)
            prereqs = probe_runtime_prerequisites(game_root, self.application_dir, config)
            if not prereqs.ok:
                meathook = prereqs.meathook
                reason = meathook.message if meathook and not meathook.ok else "Runtime prerequisites not met"
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

    def mark_windows_installation(self, succeeded: bool) -> None:
        return

    def _adapter(
        self,
        config: dict[str, object],
        game_root: Path,
        staged: Path,
    ) -> tuple[AdapterResult, str, str]:
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
                        message=f"Automatic EternalModInjector setup could not be completed ({error}). Follow the Windows Manual Mod Installer section in INSTALL.md.",
                        details={"installation_mode": "manual_install_required", "error": str(error)},
                    ),
                    "",
                    "",
                )
            adapter = WindowsModInjectorAdapter(
                dependency,
                state_dir=self.state_dir,
                confirmer=self.confirmation,
                event_sink=self._emit,
            )
            result = adapter.activate(game_root, staged)
            return result, "", ""
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
                        message="Injector download was not approved. Try setup again when ready.",
                    ),
                    plan.proposed,
                    plan.diff,
                )
            self._emit("injector_started", command=[str(dependency.executable)])
            result = LinuxModManagerAdapter(dependency, state_dir=self.state_dir, event_sink=self._emit).activate(game_root, staged)
            self._emit(
                "injector_finished",
                state=result.state,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            return result, plan.proposed, plan.diff
        raise RuntimeError(f"unsupported platform: {self.platform_name}")

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
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                operation="uninstall",
            )
            return result, plan.proposed, plan.diff
        raise RuntimeError(f"unsupported platform: {self.platform_name}")

    def ensure_game_link(
        self,
        game_root: Path,
        *,
        local_artifact: Path | None = None,
        force_repair: bool = False,
    ) -> GameLinkResult:
        dep_manager = DependencyManager(self.state_dir / "dependencies")
        result = install_meathook(
            game_root,
            dep_manager,
            state_dir=self.state_dir,
            consent=self.consent,
            local_artifact=local_artifact,
            force_repair=force_repair,
        )
        if result.state in {"installed", "repaired"}:
            post_probe = probe_meathook(game_root)
            if not post_probe.ok:
                return GameLinkResult(
                    state="failed",
                    message=f"Game Link runtime installation could not be verified after installation: {post_probe.message}",
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

    def _cached_linux_install(
        self,
        snapshot: RoomSnapshot,
        state: InstallState,
    ) -> IntegratedSetupRecord | None:
        """Return receipt-backed room setup when Linux runtime is already ready."""
        if self.platform_name != "linux":
            return None
        if state.state != "already_installed" or state.readiness != "ready":
            return None

        receipt_path = self.state_dir / "launcher_setup.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = self._manifest_for(snapshot)
        raw_command = receipt.get("adapter_command", ())
        adapter_command = tuple(str(item) for item in raw_command) if isinstance(raw_command, (list, tuple)) else ()
        raw_injector_command = receipt.get("injector_command", adapter_command)
        injector_command = (
            tuple(str(item) for item in raw_injector_command)
            if isinstance(raw_injector_command, (list, tuple)) else adapter_command
        )
        raw_required_resources = receipt.get("required_resources", ())
        required_resources = (
            tuple(item for item in raw_required_resources if isinstance(item, dict))
            if isinstance(raw_required_resources, (list, tuple)) else ()
        )
        return IntegratedSetupRecord(
            manifest_hash=manifest.manifest_hash,
            randomize_dash=bool(receipt.get("randomize_dash", manifest.options["randomize_dash"])),
            generated_mod=str(receipt.get("generated_mod", state.staged_mod)),
            staged_mod=state.staged_mod,
            staged_sha256=str(receipt["staged_sha256"]),
            adapter_state="applied",
            adapter_message=str(receipt.get("adapter_message", "Mod is already installed for current room.")),
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
        )

    def execute(self, snapshot: RoomSnapshot, endpoint: str = "") -> IntegratedSetupRecord:
        self._failure_phase = "game_setup"
        config = self._config()
        game_root = self._game_root(config)
        self._failure_phase = "room_package"
        pre_install_state = self.install_state(snapshot)
        cached = self._cached_linux_install(snapshot, pre_install_state)
        if cached is not None:
            runtime_config = self.base_workflow.write_client_config(
                self.application_dir,
                endpoint=endpoint or str(config.get("server_address") or ""),
                manifest_hash=cached.manifest_hash,
                runtime_config=config,
            )
            self._emit("runtime_config_ready", path=str(runtime_config))
            return cached

        # 1. Ensure Game Link / Meathook dependency before any room mod operations
        local_key = "meathook_dll"
        local_val = config.get(local_key)
        local_artifact = Path(str(local_val)).expanduser() if local_val else None
        try:
            game_link = self.ensure_game_link(
                game_root,
                local_artifact=local_artifact,
                force_repair=False,
            )
            if game_link.state == "needs_repair":
                self._emit(
                    "prerequisite_missing",
                    key="meathook",
                    message=game_link.message,
                    details={"path": game_link.path, "sha256": game_link.sha256, "status": "incompatible"},
                )
                raise RuntimeError(
                    "Installed Game Link runtime does not match supported Meathook v7.2. "
                    "Repair Game Link before preparing the room mod."
                )
            if game_link.state == "failed":
                raise RuntimeError(game_link.message)
        except PermissionError:
            self._emit(
                "prerequisite_missing",
                key="meathook",
                message="Game Link installation was not approved.",
                details={"status": "missing"},
            )
            raise RuntimeError(
                "Game Link download was not approved. Approve the download to finish setup."
            )
        except Exception as error:
            if not isinstance(error, (RuntimeError, PermissionError)):
                self._emit(
                    "prerequisite_missing",
                    key="meathook",
                    message=str(error),
                    details={"status": "error"},
                )
            raise

        # 2. Probe mandatory runtime prerequisites
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
                    "Meathook runtime is not installed (<DOOM root>/XINPUT1_3.dll). "
                    "Install Meathook before preparing the room mod."
                )
            failed = [c.message for c in prereqs.checks if not c.ok]
            raise RuntimeError(f"Runtime prerequisites not met: {'; '.join(failed)}")

        manifest = self._manifest_for(snapshot)
        self._emit(
            "room_validated",
            seed_name=manifest.seed_name,
            team=manifest.team,
            slot=manifest.slot,
            randomize_dash=manifest.options["randomize_dash"],
            manifest_hash=manifest.manifest_hash,
        )
        self._emit(
            "mod_building",
            randomize_dash=manifest.options["randomize_dash"],
        )
        decompressor_manager = self._idfile_manager()
        generated = self._room_compiler(
            dependency_manager=decompressor_manager,
            consent=self.consent,
        ).build(
            manifest,
            self.state_dir / "generated_mods",
        )
        generated_sha256 = hashlib.sha256(generated.read_bytes()).hexdigest()
        runtime_config = self.base_workflow.write_client_config(
            self.application_dir,
            endpoint=endpoint or str(config.get("server_address") or ""),
            manifest_hash=manifest.manifest_hash,
            runtime_config=config,
        )
        self._emit("runtime_config_ready", path=str(runtime_config))
        receipt_path = self.state_dir / "launcher_setup.json"
        staged = stage_room_mod(
            generated,
            game_root,
            receipt_path,
            manifest_hash=manifest.manifest_hash,
            legacy_removal_sink=lambda path: self._emit(
                "log", message=f"Removed legacy DOOM Eternal Archipelago mod: {path}"
            ),
        )
        self._emit("mod_staged", path=str(staged), manifest_hash=manifest.manifest_hash)
        staged_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
        if staged_sha256 != generated_sha256:
            raise RuntimeError("generated and staged room mod hashes differ")
        self._failure_phase = "game_setup"
        adapter, steam_option, steam_diff = self._adapter(config, game_root, staged)
        self._failure_phase = "room_package"
        installation_mode = str(adapter.details.get("installation_mode", "automatic") if adapter.details else "automatic")
        user_confirmed = bool(adapter.details.get("user_confirmed", False) if adapter.details else False)
        raw_required_resources = adapter.details.get("required_resources", ()) if adapter.details else ()
        required_resources = (
            tuple(item for item in raw_required_resources if isinstance(item, dict))
            if isinstance(raw_required_resources, (list, tuple)) else ()
        )
        record = IntegratedSetupRecord(
            manifest_hash=manifest.manifest_hash,
            randomize_dash=manifest.options["randomize_dash"],
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
                pre_install_state.state == "install_needed"
                and adapter.state in {"applied", "manual_install_required"}
            ),
            room_zip_sha256=staged_sha256,
            generated_sha256=generated_sha256,
            mods_path=str((game_root / "Mods").resolve()),
            injector_version=str(adapter.details.get("injector_version", "") if adapter.details else ""),
            injector_exit_code=adapter.returncode,
            required_resources=required_resources,
            post_install_verification=str(
                adapter.details.get("post_install_verification", "") if adapter.details else ""
            ),
            static_content_digest=manifest.static_content_digest,
        )
        actual_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
        if actual_hash != record.staged_sha256:
            raise RuntimeError("post-install room mod hash validation failed")
        try:
            with zipfile.ZipFile(staged) as package:
                installed_manifest = json.loads(package.read("seed_manifest.json"))
            if installed_manifest.get("manifest_hash") != manifest.manifest_hash:
                raise ValueError("room identity differs")
            if manifest.static_content_digest and installed_manifest.get("static_content_digest") != manifest.static_content_digest:
                raise ValueError("room content identity differs")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            raise RuntimeError(f"post-install room mod validation failed: {error}") from error
        payload = {
            **asdict(record),
            "endpoint": endpoint,
            "seed_name": manifest.seed_name,
            "team": manifest.team,
            "slot": manifest.slot,
            "static_content_digest": manifest.static_content_digest,
        }
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        publish_file(temporary, receipt_path, operation="setup_receipt_publish")
        if adapter.state == "manual_install_required":
            self._emit(
                "manual_install_required",
                message=adapter.message,
                guide_section="Windows Manual Mod Installer",
                guide_url="https://github.com/DoomEAP/DoomEternal-AP-Mod/blob/main/docs/INSTALL.md#windows-manual-mod-installer",
            )
        return record

    def uninstall(self, snapshot: RoomSnapshot) -> UninstallResult:
        """Remove only receipt-proven room package, then run platform injector once."""
        manifest = self._manifest_for(snapshot)
        config = self._config()
        game_root = self._game_root(config)
        running = detect_doom_processes()
        if running:
            names = ", ".join(str(item.get("name", "unknown")) for item in running)
            raise RuntimeError(f"Cannot uninstall room mod while game runtime is running: {names}")

        receipt_path = self.state_dir / "launcher_setup.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("receipt must contain an object")
            if str(receipt.get("adapter_state", "")) != "applied":
                raise ValueError("receipt does not prove an applied room mod")
            if str(receipt.get("manifest_hash", "")) != manifest.manifest_hash:
                raise ValueError("receipt belongs to another room")
            staged_input = Path(str(receipt["staged_mod"])).expanduser()
            if staged_input.is_symlink():
                raise ValueError("receipt package is a symbolic link")
            staged = staged_input.resolve()
            mods_dir = (game_root / "Mods").resolve()
            if staged.parent != mods_dir or staged.suffix.casefold() != ".zip":
                raise ValueError("receipt package is outside configured game Mods folder")
            if staged.is_symlink() or not staged.is_file():
                raise ValueError("receipt package is missing")
            expected_sha256 = str(receipt["staged_sha256"])
            actual_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError("receipt package hash does not match")
            with zipfile.ZipFile(staged) as package:
                package_manifest = json.loads(package.read("seed_manifest.json"))
            if not isinstance(package_manifest, dict) or package_manifest.get("manifest_hash") != manifest.manifest_hash:
                raise ValueError("receipt package manifest does not match current room")
        except (OSError, RuntimeError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            raise RuntimeError(f"Cannot uninstall room mod safely: {error}") from error

        quarantine_dir = self.state_dir / "uninstall-quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine = quarantine_dir / f"{staged.name}.{time.time_ns()}.zip"

        receipt.update(
            {
                "adapter_state": "uninstall_attention",
                "adapter_message": "Room mod uninstall is in progress or requires attention.",
                "installation_mode": "uninstall",
                "uninstall_quarantine": str(quarantine),
            }
        )
        temporary = receipt_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            publish_file(temporary, receipt_path, operation="uninstall_attention_publish")
        except Exception as error:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise RuntimeError(f"Could not record uninstall attention state: {error}") from error

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
                publish_file(temporary, receipt_path, operation="uninstall_failed_publish")
            except Exception as error:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                message = f"{message} Receipt update failed: {error}"
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
                raise RuntimeError("quarantine package hash does not match receipt")
            staged.unlink()
            if staged.exists():
                raise RuntimeError("verified room package remains after removal")
        except Exception as error:
            raise RuntimeError(f"Room package removal was not completed safely: {error}") from error
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
            return failed_result(f"Room package removed, but injector cleanup did not complete: {error}")
        self._emit(
            "uninstall_progress",
            phase="injector_finished",
            manifest_hash=manifest.manifest_hash,
            injector_state=injector.state,
            returncode=injector.returncode,
        )
        if injector.state != "applied":
            return failed_result(
                f"Room package removed, but injector cleanup failed: {injector.message}",
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
                f"Injector completed, but room package removal could not be verified: {error}",
                injector.state,
                injector.command,
            )
        if remaining:
            locations = ", ".join(str(path) for path in remaining)
            return failed_result(
                f"Injector completed, but managed room package remains at: {locations}",
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
                "adapter_message": "Room mod intentionally uninstalled.",
                "adapter_command": list(injector.command),
                "installation_mode": "uninstalled",
                "steam_launch_option": steam_option or str(receipt.get("steam_launch_option", "")),
                "steam_launch_option_diff": steam_diff or str(receipt.get("steam_launch_option_diff", "")),
                "uninstall_quarantine": str(quarantine),
            }
        )
        try:
            temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            publish_file(temporary, receipt_path, operation="uninstall_receipt_publish")
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
                message=f"Room package removed and injector completed, but receipt update failed: {error}",
                attention=True,
            )
        return UninstallResult(
            state="uninstalled",
            manifest_hash=manifest.manifest_hash,
            staged_mod=str(staged),
            quarantine_path=str(quarantine),
            adapter_state="uninstalled",
            injector_state=injector.state,
            message="Room mod intentionally uninstalled.",
        )

    def confirm_manual_installation(self, snapshot: RoomSnapshot, endpoint: str = "") -> IntegratedSetupRecord:
        """Record manual mod installation completion after player verifies external injector run."""
        manifest = self._manifest_for(snapshot)
        receipt_path = self.state_dir / "launcher_setup.json"
        try:
            receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt_document, dict):
                raise ValueError("receipt must contain an object")
            if str(receipt_document["manifest_hash"]) != manifest.manifest_hash:
                raise ValueError("receipt belongs to another room")
            if str(receipt_document.get("adapter_state", "")) != "manual_install_required":
                raise ValueError("receipt is not pending manual installation")
            new_install = receipt_document["new_install"]
            if not isinstance(new_install, bool):
                raise ValueError("receipt new_install must be boolean")
            staged_mod_path = Path(str(receipt_document["staged_mod"])).resolve()
            expected_sha256 = str(receipt_document["staged_sha256"])
            if not staged_mod_path.is_file():
                raise ValueError("recorded room package is missing")
            staged_sha256 = hashlib.sha256(staged_mod_path.read_bytes()).hexdigest()
            if staged_sha256 != expected_sha256:
                raise ValueError("recorded room package hash does not match receipt")
            with zipfile.ZipFile(staged_mod_path) as package:
                package_manifest = json.loads(package.read("seed_manifest.json"))
            if not isinstance(package_manifest, dict) or package_manifest.get("manifest_hash") != manifest.manifest_hash:
                raise ValueError("room package manifest does not match current room")
        except (OSError, RuntimeError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            raise RuntimeError(f"Pending manual installation receipt is invalid: {error}") from error

        config = self._config()
        game_root = self._game_root(config)
        verification: dict[str, object] = {}
        if self.platform_name == "linux":
            verification = verify_linux_mod_installation(game_root, staged_mod_path)
        steam_plan = self._steam_plan(config) if self.platform_name == "linux" else None
        steam_option = steam_plan.proposed if steam_plan else ""
        steam_diff = steam_plan.diff if steam_plan else ""
        raw_required_resources = verification.get("required_resources", ())
        required_resources = (
            tuple(item for item in raw_required_resources if isinstance(item, dict))
            if isinstance(raw_required_resources, (list, tuple)) else ()
        )

        record = IntegratedSetupRecord(
            manifest_hash=manifest.manifest_hash,
            randomize_dash=manifest.options["randomize_dash"],
            generated_mod=str(staged_mod_path.resolve()),
            staged_mod=str(staged_mod_path.resolve()),
            staged_sha256=staged_sha256,
            adapter_state="applied",
            adapter_message="Manual mod installation confirmed by user.",
            adapter_command=(),
            injector_command=(),
            steam_launch_option=steam_option,
            steam_launch_option_diff=steam_diff,
            installation_mode="manual_fallback",
            user_confirmed=True,
            new_install=new_install,
            room_zip_sha256=staged_sha256,
            generated_sha256=staged_sha256,
            mods_path=str((game_root / "Mods").resolve()),
            required_resources=required_resources,
            post_install_verification=str(verification.get("state", "")),
            static_content_digest=manifest.static_content_digest,
        )

        payload = {
            **asdict(record),
            "endpoint": endpoint,
            "seed_name": manifest.seed_name,
            "team": manifest.team,
            "slot": manifest.slot,
            "static_content_digest": manifest.static_content_digest,
        }
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        publish_file(temporary, receipt_path, operation="manual_confirm_publish")
        return record


class RoomSetupCoordinator:
    """Serialize setup per room and expose explicit retry without duplicate reconnect work."""

    def __init__(
        self,
        workflow: IntegratedLaunchWorkflow,
        event_sink: EventSink,
        result_sink: Callable[[IntegratedSetupRecord], None],
    ):
        self.workflow = workflow
        self.event_sink = event_sink
        self.result_sink = result_sink
        self._state_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._active: set[tuple[object, ...]] = set()
        self._completed: set[tuple[object, ...]] = set()
        self._uninstall_active = False
        self._last_event: dict[str, object] | None = None

    def observe(self, event: dict[str, object]) -> bool:
        if event.get("type") != "connected":
            return False
        with self._state_lock:
            self._last_event = dict(event)
        return True

    @staticmethod
    def _key(event: dict[str, object]) -> tuple[object, ...]:
        return (
            event.get("seed_name"),
            event.get("team"),
            event.get("slot"),
            json.dumps(event.get("slot_data", {}), sort_keys=True),
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
        context.update(payload)
        self.event_sink(kind, context)

    def submit(self, event: dict[str, object], *, force: bool = False) -> bool:
        if event.get("type") != "connected":
            return False
        key = self._key(event)
        with self._state_lock:
            self._last_event = dict(event)
            if key in self._active or (key in self._completed and not force):
                return False
            if force:
                self._completed.discard(key)
            self._active.add(key)
        self._emit_room_event(
            "ROOM_INSTALL_ACTION_REQUESTED",
            event,
            explicit=bool(event.get("explicit", not force)),
            source=event.get("source", "coordinator"),
        )
        self.event_sink("setup_started", {"seed_name": event.get("seed_name")})

        def worker() -> None:
            try:
                with self._worker_lock:
                    try:
                        snapshot = RoomSnapshot.from_event(event)
                    except Exception as error:
                        self.event_sink(
                            "setup_failed",
                            setup_failure_payload(error, phase="room_snapshot"),
                        )
                        self._emit_room_event(
                            "ROOM_INSTALL_RESULT",
                            event,
                            result="failure",
                            state="failed",
                            error_type=type(error).__name__,
                            message=str(error),
                        )
                        return
                    self._emit_room_event(
                        "ROOM_INSTALL_PLAN",
                        event,
                        plan="compile_stage_inject",
                        adapter_state=self._receipt_context().get("adapter_state", ""),
                    )
                    self._emit_room_event(
                        "ROOM_INSTALL_TRANSITION",
                        event,
                        phase="starting",
                        state="installing",
                    )
                    record = self.workflow.execute(
                        snapshot,
                        str(event.get("endpoint") or ""),
                    )
                self._emit_room_event(
                    "ROOM_INSTALL_TRANSITION",
                    event,
                    phase="finished",
                    state=record.adapter_state,
                    adapter_state=record.adapter_state,
                    manifest_hash=record.manifest_hash,
                )
                if record.adapter_state == "manual_install_required":
                    self.result_sink(record)
                    self.event_sink(
                        "manual_install_required",
                        {
                            "manifest_hash": record.manifest_hash,
                            "new_install": record.new_install,
                            "message": record.adapter_message,
                            "guide_section": "Windows Manual Mod Installer",
                            "guide_url": "https://github.com/DoomEAP/DoomEternal-AP-Mod/blob/main/docs/INSTALL.md#windows-manual-mod-installer",
                        },
                    )
                    self._emit_room_event(
                        "ROOM_INSTALL_RESULT",
                        event,
                        result="attention",
                        state=record.adapter_state,
                        adapter_state=record.adapter_state,
                        manifest_hash=record.manifest_hash,
                        message=record.adapter_message,
                    )
                    return
                if record.adapter_state != "applied":
                    raise RuntimeError(record.adapter_message or "Mod setup was not applied.")
                with self._state_lock:
                    self._completed.add(key)
                self.result_sink(record)
                self.event_sink(
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
                self._emit_room_event(
                    "ROOM_INSTALL_RESULT",
                    event,
                    result="success",
                    state=record.adapter_state,
                    adapter_state=record.adapter_state,
                    manifest_hash=record.manifest_hash,
                    new_install=record.new_install,
                )
            except Exception as error:
                self.event_sink(
                    "setup_failed",
                    setup_failure_payload(
                        error,
                        phase=str(getattr(self.workflow, "_failure_phase", "")),
                    ),
                )
                self._emit_room_event(
                    "ROOM_INSTALL_RESULT",
                    event,
                    result="failure",
                    state="failed",
                    error_type=type(error).__name__,
                    message=str(error),
                )
            finally:
                with self._state_lock:
                    self._active.discard(key)

        threading.Thread(target=worker, name="DoomRoomSetup", daemon=True).start()
        return True

    def start(self, *, force: bool = False) -> bool:
        with self._state_lock:
            event = dict(self._last_event) if self._last_event else None
        return self.submit(event, force=force) if event else False

    def retry(self) -> bool:
        with self._state_lock:
            event = dict(self._last_event) if self._last_event else None
        return self.submit(event, force=True) if event else False

    def submit_uninstall(self, event: dict[str, object]) -> bool:
        """Run uninstall on coordinator worker, sharing setup serialization lock."""
        if event.get("type") != "connected":
            return False
        key = self._key(event)
        with self._state_lock:
            self._last_event = dict(event)
            if self._uninstall_active:
                return False
            self._uninstall_active = True
        self._emit_room_event(
            "ROOM_UNINSTALL_ACTION_REQUESTED",
            event,
            explicit=True,
            source=event.get("source", "coordinator"),
        )

        def worker() -> None:
            try:
                self.event_sink(
                    "uninstall_queued",
                    {},
                )
                with self._worker_lock:
                    snapshot = RoomSnapshot.from_event(event)
                    manifest = self.workflow._manifest_for(snapshot)
                    self._emit_room_event(
                        "ROOM_UNINSTALL_PLAN",
                        event,
                        plan="quarantine_remove_injector_cleanup",
                        adapter_state=self._receipt_context().get("adapter_state", ""),
                    )
                    self._emit_room_event(
                        "ROOM_UNINSTALL_TRANSITION",
                        event,
                        phase="starting",
                        state="uninstalling",
                        manifest_hash=manifest.manifest_hash,
                    )
                    self.event_sink(
                        "uninstall_started",
                        {"manifest_hash": manifest.manifest_hash},
                    )
                    result = self.workflow.uninstall(snapshot)
                self._emit_room_event(
                    "ROOM_UNINSTALL_TRANSITION",
                    event,
                    phase="finished",
                    state=result.state,
                    adapter_state=result.adapter_state,
                    manifest_hash=result.manifest_hash,
                )
                if result.state == "uninstalled":
                    with self._state_lock:
                        self._completed.discard(key)
                self.event_sink(
                    "uninstall_complete" if result.state == "uninstalled" else "uninstall_attention",
                    {**asdict(result), "manifest_hash": result.manifest_hash},
                )
                self._emit_room_event(
                    "ROOM_UNINSTALL_RESULT",
                    event,
                    result="success" if result.state == "uninstalled" else "attention",
                    state=result.state,
                    adapter_state=result.adapter_state,
                    manifest_hash=result.manifest_hash,
                    message=result.message,
                )
            except Exception as error:
                self.event_sink(
                    "uninstall_failed",
                    {
                        "state": "attention",
                        "attention": True,
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
                )
                self._emit_room_event(
                    "ROOM_UNINSTALL_RESULT",
                    event,
                    result="failure",
                    state="attention",
                    error_type=type(error).__name__,
                    message=str(error),
                )
            finally:
                with self._state_lock:
                    self._uninstall_active = False

        threading.Thread(target=worker, name="DoomRoomUninstall", daemon=True).start()
        return True
