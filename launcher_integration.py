"""Room-bound setup services for standalone DOOM Eternal launcher."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from launcher_core import LaunchWorkflow, RoomModPackageBuilder, RoomSnapshot
from launcher_platform import (
    LINUX_MOD_INJECTOR,
    WINDOWS_MOD_MANAGER,
    AdapterResult,
    DependencyManager,
    LinuxModManagerAdapter,
    LaunchOptionPlan,
    SteamLaunchOptionsManager,
    WindowsModManagerAdapter,
    stage_room_mod,
)

EventSink = Callable[[str, dict[str, object]], None]
ConsentCallback = Callable[[object], bool]


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
    steam_launch_option: str = ""
    steam_launch_option_diff: str = ""


@dataclass(frozen=True)
class InstallState:
    state: str
    manifest_hash: str
    staged_mod: str = ""
    steam_launch_option: str = ""
    reason: str = ""


class IntegratedLaunchWorkflow:
    """Generate, stage, and prepare platform tooling after authoritative Connected."""

    def __init__(
        self,
        application_dir: Path,
        state_dir: Path,
        config_path: Path,
        *,
        platform_name: str | None = None,
        event_sink: EventSink | None = None,
        consent: ConsentCallback | None = None,
    ):
        self.base_workflow = LaunchWorkflow()
        self.application_dir = application_dir
        self.state_dir = state_dir
        self.config_path = config_path
        self.platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
        self.event_sink = event_sink or (lambda _kind, _payload: None)
        self.consent = consent or (lambda _spec: False)

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

    def _template_hashes(self) -> set[str]:
        resource = self.application_dir / "resources" / "mod_templates.zip"
        with zipfile.ZipFile(resource) as archive:
            document = json.loads(archive.read("index.json"))
        return {
            str(entry["sha256"])
            for entry in document.get("variants", {}).values()
            if isinstance(entry, dict) and entry.get("sha256")
        }

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

    def install_state(self, snapshot: RoomSnapshot) -> InstallState:
        """Resolve current room identity against verified launcher ownership."""
        manifest = self.base_workflow.manifest_for(snapshot)
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
            return InstallState("install_needed", manifest.manifest_hash, reason=f"invalid install record: {error}")
        if recorded_manifest != manifest.manifest_hash:
            return InstallState("install_needed", manifest.manifest_hash, reason="installed manifest belongs to another room")
        if not staged.is_file():
            return InstallState("install_needed", manifest.manifest_hash, reason="recorded installed package is missing")
        if hashlib.sha256(staged.read_bytes()).hexdigest() != expected_sha:
            return InstallState("install_needed", manifest.manifest_hash, reason="installed package hash does not match record")
        try:
            with zipfile.ZipFile(staged) as package:
                package_manifest = json.loads(package.read("seed_manifest.json"))
            if package_manifest.get("manifest_hash") != manifest.manifest_hash:
                raise ValueError("package manifest hash does not match current room")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            return InstallState("install_needed", manifest.manifest_hash, reason=f"installed package validation failed: {error}")
        if adapter_state != "applied" and not (adapter_state == "manual_action_required" and windows_confirmed):
            return InstallState("install_needed", manifest.manifest_hash, reason=f"previous install state is {adapter_state or 'unknown'}")
        return InstallState("already_installed", manifest.manifest_hash, str(staged), steam_option)

    def mark_windows_installation(self, succeeded: bool) -> None:
        if not succeeded:
            return
        receipt_path = self.state_dir / "launcher_setup.json"
        if not receipt_path.is_file():
            return
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        payload["adapter_state"] = "applied"
        payload["windows_installation_confirmed"] = True
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, receipt_path)

    def _adapter(
        self,
        config: dict[str, object],
        game_root: Path,
        staged: Path,
    ) -> tuple[AdapterResult, str, str]:
        manager = DependencyManager(self.state_dir / "dependencies")
        local_key = (
            "eternal_mod_manager_archive"
            if self.platform_name == "windows"
            else "eternal_basher_archive"
        )
        local_value = config.get(local_key)
        local_artifact = Path(str(local_value)).expanduser() if local_value else None
        if self.platform_name == "windows":
            try:
                dependency = self._acquire(manager, WINDOWS_MOD_MANAGER, local_artifact)
            except PermissionError:
                return (
                    AdapterResult(
                        state="manual_action_required",
                        message="The mod is ready. Approve the manager download, then try again.",
                    ),
                    "",
                    "",
                )
            return WindowsModManagerAdapter(dependency).activate(game_root, staged), "", ""
        if self.platform_name == "linux":
            plan = self._steam_plan(config)
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
            result = LinuxModManagerAdapter(dependency).activate(game_root, staged)
            self._emit(
                "injector_finished",
                state=result.state,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            return result, plan.proposed, plan.diff
        raise RuntimeError(f"unsupported platform: {self.platform_name}")

    def execute(self, snapshot: RoomSnapshot, endpoint: str = "") -> IntegratedSetupRecord:
        manifest = self.base_workflow.manifest_for(snapshot)
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
        generated = RoomModPackageBuilder(
            self.application_dir / "resources" / "mod_templates.zip"
        ).build(
            manifest,
            self.state_dir / "generated_mods",
        )
        config = self._config()
        game_root = self._game_root(config)
        receipt_path = self.state_dir / "launcher_setup.json"
        staged = stage_room_mod(
            generated,
            game_root,
            receipt_path,
            trusted_template_hashes=self._template_hashes(),
            manifest_hash=manifest.manifest_hash,
            legacy_removal_sink=lambda path: self._emit(
                "log", message=f"Removed legacy DOOM Eternal Archipelago mod: {path}"
            ),
        )
        self._emit("mod_staged", path=str(staged), manifest_hash=manifest.manifest_hash)
        adapter, steam_option, steam_diff = self._adapter(config, game_root, staged)
        record = IntegratedSetupRecord(
            manifest_hash=manifest.manifest_hash,
            randomize_dash=manifest.options["randomize_dash"],
            generated_mod=str(generated.resolve()),
            staged_mod=str(staged.resolve()),
            staged_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
            adapter_state=adapter.state,
            adapter_message=adapter.message,
            adapter_command=adapter.command,
            steam_launch_option=steam_option,
            steam_launch_option_diff=steam_diff,
        )
        payload = {
            **asdict(record),
            "endpoint": endpoint,
            "seed_name": manifest.seed_name,
            "team": manifest.team,
            "slot": manifest.slot,
        }
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, receipt_path)
        if adapter.state == "manual_action_required":
            self._emit(
                "manual_action_required",
                message=adapter.message,
                command=list(adapter.command),
            )
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
        self.event_sink("setup_started", {"seed_name": event.get("seed_name")})

        def worker() -> None:
            try:
                with self._worker_lock:
                    snapshot = RoomSnapshot.from_event(event)
                    record = self.workflow.execute(
                        snapshot,
                        str(event.get("endpoint") or ""),
                    )
                with self._state_lock:
                    self._completed.add(key)
                self.result_sink(record)
                self.event_sink(
                    "setup_ready",
                    {
                        "manifest_hash": record.manifest_hash,
                        "randomize_dash": record.randomize_dash,
                        "adapter_state": record.adapter_state,
                        "message": record.adapter_message,
                        "steam_launch_option": record.steam_launch_option,
                    },
                )
            except Exception as error:
                self.event_sink(
                    "setup_failed",
                    {
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
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
