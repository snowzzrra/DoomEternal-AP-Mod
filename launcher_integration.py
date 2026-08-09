"""Integrated room-bound setup used by real Archipelago Launcher entrypoint."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
import threading
import traceback
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
    SteamLaunchOptionsManager,
    WindowsModManagerAdapter,
    stage_room_mod,
)


@dataclass(frozen=True)
class IntegratedSetupRecord:
    manifest_hash: str
    randomize_dash: bool
    generated_mod: str
    staged_mod: str
    staged_sha256: str
    adapter_state: str
    adapter_message: str
    steam_launch_option: str = ""
    steam_launch_option_diff: str = ""


def _message(title: str, text: str, *, error: bool = False, icon: Path | None = None) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if icon and icon.is_file():
            image = tk.PhotoImage(file=str(icon))
            root.iconphoto(True, image)
            root._doom_icon = image  # type: ignore[attr-defined]
        if error:
            messagebox.showerror(title, text, parent=root)
        else:
            messagebox.showinfo(title, text, parent=root)
        root.destroy()
    except Exception:
        print(f"{title}: {text}", file=sys.stderr if error else sys.stdout)


def _consent(spec) -> bool:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        accepted = messagebox.askyesno(
            "DOOM Eternal dependency",
            f"Download {spec.name} {spec.version} from its official GitHub release?\n\n"
            "Archive will be verified by SHA-256 before extraction.",
            parent=root,
        )
        root.destroy()
        return bool(accepted)
    except Exception:
        return False


class IntegratedLaunchWorkflow:
    """Generate one room package, stage it, and prepare platform-specific tooling."""

    def __init__(
        self,
        client_dir: Path,
        *,
        platform_name: str | None = None,
        notify: Callable[[str, str], None] | None = None,
        consent: Callable[[object], bool] | None = None,
        icon: Path | None = None,
    ):
        self.base_workflow = LaunchWorkflow()
        self.client_dir = client_dir
        self.platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
        self.icon = icon
        self.notify = notify or (lambda title, text: _message(title, text, icon=self.icon))
        self.consent = consent or _consent

    def manifest_for(self, snapshot: RoomSnapshot):
        return self.base_workflow.manifest_for(snapshot)

    @staticmethod
    def write_client_config(client_dir: Path, *, endpoint: str, manifest_hash: str) -> Path:
        return LaunchWorkflow.write_client_config(
            client_dir,
            endpoint=endpoint,
            manifest_hash=manifest_hash,
        )

    def _config(self) -> dict[str, object]:
        path = self.client_dir / "ap_config.json"
        if not path.is_file():
            raise FileNotFoundError("ap_config.json was not created by DOOM Eternal Client configuration")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("ap_config.json must contain an object")
        return document

    @staticmethod
    def _game_root(config: dict[str, object]) -> Path:
        base = Path(str(config.get("doom_base_dir", ""))).expanduser().resolve()
        root = base.parent if base.name.casefold() == "base" else base
        if not (root / "DOOMEternalx64vk.exe").is_file() or not (root / "base").is_dir():
            raise ValueError(f"invalid configured DOOM Eternal installation: {root}")
        return root

    def _template_hashes(self) -> set[str]:
        document = json.loads((self.client_dir / "mod_templates/index.json").read_text(encoding="utf-8"))
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
                current = SteamLaunchOptionsManager.detect(localconfig)
        plan = SteamLaunchOptionsManager.plan_bridge(
            current,
            self.client_dir / "run_bridge.sh",
            delay=5,
        )
        instruction = self.client_dir / "steam_launch_option.txt"
        instruction.write_text(plan.proposed + "\n", encoding="utf-8")
        diff_path = self.client_dir / "steam_launch_option.diff"
        diff_path.write_text((plan.diff or "No change required.\n") + ("\n" if plan.diff else ""), encoding="utf-8")
        return plan

    def _adapter(
        self,
        config: dict[str, object],
        game_root: Path,
        staged: Path,
    ) -> tuple[AdapterResult, str, str]:
        dependency_manager = DependencyManager(self.client_dir / "dependencies")
        local_key = "eternal_mod_manager_archive" if self.platform_name == "windows" else "eternal_basher_archive"
        local_value = config.get(local_key)
        local_artifact = Path(str(local_value)).expanduser() if local_value else None
        if self.platform_name == "windows":
            try:
                dependency = dependency_manager.acquire(
                    WINDOWS_MOD_MANAGER,
                    consent=self.consent,
                    local_artifact=local_artifact,
                )
            except PermissionError:
                return (
                    AdapterResult(
                        state="manual_action_required",
                        message="Mod staged. Download/open EternalModManager 4.2.3, then run its injector.",
                    ),
                    "",
                    "",
                )
            return WindowsModManagerAdapter(dependency).activate(game_root, staged), "", ""
        if self.platform_name == "linux":
            plan = self._steam_plan(config)
            try:
                dependency = dependency_manager.acquire(
                    LINUX_MOD_INJECTOR,
                    consent=self.consent,
                    local_artifact=local_artifact,
                )
            except PermissionError:
                result = AdapterResult(
                    state="manual_action_required",
                    message=(
                        "Mod staged. Obtain verified EternalModInjectorShell 6.66-rev3.12, run it interactively, "
                        "then start DOOM Eternal through Steam."
                    ),
                )
            else:
                result = LinuxModManagerAdapter(dependency).prepare(game_root, staged)
            return result, plan.proposed, plan.diff
        raise RuntimeError(f"unsupported platform: {self.platform_name}")

    def execute(self, snapshot: RoomSnapshot, install_root: Path, endpoint: str = "") -> IntegratedSetupRecord:
        del install_root
        print("[DOOM Setup] Connected; validating authoritative room options.", flush=True)
        manifest = self.manifest_for(snapshot)
        print(
            f"[DOOM Setup] Building physical {'Dash ON' if manifest.options['randomize_dash'] else 'Dash OFF'} package.",
            flush=True,
        )
        generated = RoomModPackageBuilder(self.client_dir / "mod_templates").build(
            manifest,
            self.client_dir / "generated_mods",
        )
        print(f"[DOOM Setup] Generated {generated.name}; preparing installation.", flush=True)
        config = self._config()
        game_root = self._game_root(config)
        receipt_path = self.client_dir / "launcher_setup.json"
        staged = stage_room_mod(
            generated,
            game_root,
            receipt_path,
            trusted_template_hashes=self._template_hashes(),
            manifest_hash=manifest.manifest_hash,
        )
        print(f"[DOOM Setup] Staged {staged}; preparing platform adapter.", flush=True)
        adapter, steam_option, steam_diff = self._adapter(config, game_root, staged)
        record = IntegratedSetupRecord(
            manifest_hash=manifest.manifest_hash,
            randomize_dash=manifest.options["randomize_dash"],
            generated_mod=str(generated.resolve()),
            staged_mod=str(staged.resolve()),
            staged_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
            adapter_state=adapter.state,
            adapter_message=adapter.message,
            steam_launch_option=steam_option,
            steam_launch_option_diff=steam_diff,
        )
        payload = {**asdict(record), "endpoint": endpoint, "seed_name": manifest.seed_name, "team": manifest.team, "slot": manifest.slot}
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, receipt_path)
        message = (
            f"Room mod ready ({'Dash ON' if record.randomize_dash else 'Dash OFF'}).\n"
            f"{adapter.message}"
        )
        if steam_option:
            message += f"\n\nSteam Launch Options (not written automatically):\n{steam_option}"
            if steam_diff:
                message += f"\n\nProposed diff:\n{steam_diff}"
        self.notify("DOOM Eternal room setup ready", message)
        return record


class IntegratedSetupCoordinator:
    """Serialize room setup and make reconnect/retry idempotent."""

    def __init__(self, workflow: IntegratedLaunchWorkflow, client_dir: Path):
        self.workflow = workflow
        self.client_dir = client_dir
        self._state_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._active: set[tuple[object, ...]] = set()
        self._completed: set[tuple[object, ...]] = set()

    def handle(self, event: dict[str, object]) -> None:
        if event.get("type") != "connected":
            return
        key = (
            event.get("seed_name"),
            event.get("team"),
            event.get("slot"),
            json.dumps(event.get("slot_data", {}), sort_keys=True),
        )
        with self._state_lock:
            if key in self._active or key in self._completed:
                return
            self._active.add(key)

        def worker() -> None:
            try:
                with self._worker_lock:
                    snapshot = RoomSnapshot.from_event(event)
                    endpoint = str(event.get("endpoint") or "")
                    record = self.workflow.execute(snapshot, self.client_dir / "generated_mods", endpoint)
                    self.workflow.write_client_config(
                        self.client_dir,
                        endpoint=endpoint,
                        manifest_hash=record.manifest_hash,
                    )
                with self._state_lock:
                    self._completed.add(key)
            except Exception as error:
                traceback.print_exc()
                _message(
                    "DOOM Eternal setup failed",
                    f"{type(error).__name__}: {error}\n\nFix the problem and reconnect to retry safely.",
                    error=True,
                    icon=self.workflow.icon,
                )
            finally:
                with self._state_lock:
                    self._active.discard(key)

        threading.Thread(target=worker, name="DoomEternalRoomSetup", daemon=True).start()


def launch_in_process(*launch_args: str, icon_path: str | None = None) -> None:
    """Run bridge inside AP Launcher's child process; safe for frozen builds."""
    client_dir = Path(__file__).resolve().parent
    bridge = client_dir / "bridge_client.py"
    icon = Path(icon_path).resolve() if icon_path else client_dir / "doom_logo.png"
    os.environ["DOOM_AP_ICON"] = str(icon)
    os.chdir(client_dir)
    if str(client_dir) not in sys.path:
        sys.path.insert(0, str(client_dir))
    bridge_globals = runpy.run_path(str(bridge), run_name="doom_eternal_external_client")
    workflow = IntegratedLaunchWorkflow(client_dir, icon=icon)
    coordinator = IntegratedSetupCoordinator(workflow, client_dir)
    bridge_globals["LAUNCHER_EVENT_HANDLER"] = coordinator.handle
    bridge_globals["launch"](*launch_args)


if __name__ == "__main__":
    launch_in_process(*sys.argv[1:])
