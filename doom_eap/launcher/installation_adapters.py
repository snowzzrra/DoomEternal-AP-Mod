"""Windows and Proton injector execution; no installation receipts or save ownership."""

from __future__ import annotations

import csv
import os
import shutil
import signal
import subprocess
import zipfile
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from doom_eap.launcher import launcher_injector
from doom_eap.launcher import launcher_platform as platform
from doom_eap.launcher.ui.strings import text as player_text

from .ui.strings import text


class InjectorState(str, Enum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    UNKNOWN = "unknown"


def injector_state(evidence):
    try:
        if injector_processes(evidence):
            return InjectorState.RUNNING
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return InjectorState.UNKNOWN
    result = evidence.get("injector_outcome", {}).get("adapter", {})
    if result.get("returncode") is None or result.get("details", {}).get("recovery_required"):
        return InjectorState.UNKNOWN
    return InjectorState.FINISHED if result["returncode"] == 0 else InjectorState.FAILED


def record_injector_settings(journal):
    from .install_journal import checked_path, file_digest

    settings = str(journal.root / 'EternalModInjector Settings.txt')
    entry = next((entry for entry in journal.document["files"] if entry["path"] == settings), None)
    if entry is not None:
        digest = file_digest(checked_path(Path(settings)))
        if digest not in entry["accepted"]:
            entry["accepted"].append(digest)
        journal.save()


def _record_command(journal, command):
    if journal is not None:
        journal.document["evidence"]["command"] = list(command)
        journal.save()


def injector_processes(evidence, *, process_list=None):
    if process_list is None:
        process_list = _read_processes()
    tool = Path(evidence.get("tool", {}).get("path", "")).name.casefold()
    names = {Path(name).name.casefold() for name in launcher_injector.WINDOWS_INJECTOR_REQUIRED_MEMBERS}
    names.update({tool, "injectorshell", "eternalmodinjector", "eternalmodinjector.exe"})
    names.update(Path(name).stem for name in tuple(names))
    parents = {str(evidence.get("injector_pid", ""))}
    parents.update(str(process["pid"]) for process in process_list if _injector_process(process, evidence, names))
    active = _injector_descendants(process_list, parents)
    return tuple(process for process in process_list if str(process["pid"]) in active)


def _injector_process(process, evidence, names):
    group = evidence.get("injector_group")
    return process["name"].casefold() in names or (group is not None and process.get("group_id") == str(group))


def _injector_descendants(processes, parents):
    while True:
        children = {str(process["pid"]) for process in processes if str(process["parent_pid"]) in parents}
        if children.issubset(parents):
            return parents
        parents.update(children)


def _read_processes():
    if os.name == "nt":
        command = (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Csv -NoTypeInformation',
        )
    else:
        command = ("ps", "-eo", "pid=,ppid=,pgid=,comm=")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if not completed.stdout.strip():
        raise ValueError('empty injector process inspection')
    if os.name == "nt":
        return [
            {"pid": row["ProcessId"], "parent_pid": row["ParentProcessId"], "name": row["Name"]}
            for row in csv.DictReader(completed.stdout.splitlines())
        ]
    return [_process_row(line) for line in completed.stdout.splitlines()]


def _process_row(line):
    pid, parent_pid, group_id, name = line.strip().split(maxsplit=3)
    return {"pid": pid, "parent_pid": parent_pid, "group_id": group_id, "name": name}


def recover_external_execution(journal):
    require_injector_stopped(journal)
    if (journal.root / launcher_injector.INJECTOR_HOLD).exists():
        record = launcher_injector._read_injector_hold(journal.root)
        launcher_injector.InjectorResourceHold(journal.root, record).finish_injector()
        launcher_injector.recover_injector_resources(journal.root)
    record_injector_settings(journal)


def require_injector_stopped(journal):
    check_injector_processes(journal.document["evidence"])


def check_injector_processes(evidence):
    from .install_journal import InstallationRecoveryRequired

    try:
        running = injector_processes(evidence)
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
        raise InstallationRecoveryRequired('injector process inspection failed: {value_0}'.format(value_0=f'{error}')) from error
    if running:
        raise InstallationRecoveryRequired('injector or a child process is still running')


class WindowsModInjectorAdapter:
    """Stage verified EternalModInjector toolchain, launch visible batch console, track process lifetime, and request player confirmation."""

    def __init__(
        self,
        dependency: launcher_injector.InstalledDependency,
        *,
        state_dir: Path | None = None,
        opener: Callable[[Sequence[str], Path], Any] | None = None,
        confirmer: Callable[[], bool] | None = None,
        event_sink: Callable[..., None] | None = None,
    ):
        self.dependency = dependency
        self.state_dir = state_dir
        self.opener = opener or self._default_opener
        self.confirmer = confirmer or (lambda: False)
        self.event_sink = event_sink

    @staticmethod
    def _default_opener(command: Sequence[str], cwd: Path) -> subprocess.Popen:
        flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
        return subprocess.Popen(command, cwd=cwd, creationflags=flags)

    def _run_process(
        self, game_root: Path, command: tuple[str, ...], staged_mod: str, hold: launcher_injector.InjectorResourceHold
    ) -> launcher_injector.AdapterResult:
        if self.event_sink is not None:
            self.event_sink(
                "injector_started",
                command=list(command),
                staged_mod=staged_mod,
                message='Keep this launcher open and press a key when EternalModInjector asks.',
            )

        try:
            hold.start_injector()
            process = self.opener(command, game_root)
            evidence = {"tool": {"path": command[-1]}, "injector_pid": getattr(process, "pid", None)}
            if self.journal is not None:
                self.journal.document["evidence"]["injector_pid"] = getattr(process, "pid", None)
                self.journal.save()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            return launcher_injector._installation_failure(
                launcher_injector.AdapterResult(state="failed", message="", command=command), error, "injector", self.event_sink
            )

        try:
            if hasattr(process, "wait"):
                process.wait()
            returncode = getattr(process, "returncode", 0)
            check_injector_processes(evidence)
            hold.finish_injector()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            return launcher_injector._installation_failure(
                launcher_injector.AdapterResult(state="failed", message="", command=command), error, "injector", self.event_sink
            )

        if self.event_sink is not None:
            self.event_sink("injector_closed", command=list(command), returncode=returncode)

        return launcher_injector.AdapterResult(
            state="applied" if returncode == 0 else "failed",
            message=(
                'Installation finished.'
                if returncode == 0
                else 'Installation failed at injector: exit code {value_0}. Select Help -> SAVE SUPPORT REPORT.'.format(value_0=f'{returncode}')
            ),
            command=command,
            returncode=returncode,
        )

    def _run_prepared_process(self, game_root, command, staged_mod):
        result = launcher_injector.AdapterResult(state="failed", message="", command=command)
        try:
            with launcher_injector.prepare_injector_resources(game_root, self.event_sink) as hold:
                result = self._run_process(game_root, command, staged_mod, hold)
            return result
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            error.details = {
                **result.details,
                **getattr(error, "details", {}),
                "original_verification_error": result.details.get("verification_error"),
            }
            return launcher_injector._installation_failure(result, error, 'prepare banks', self.event_sink)

    def prepare(self, game_root: Path, *, journal=None) -> Path:
        platform.require_doom_closed(player_text('launcher.installation_adapters', 'preparing_eternalmodinjector'))
        launcher_injector.recover_injector_resources(game_root)
        launcher_injector.recover_stale_sandbox_hold(game_root, state_dir=self.state_dir, event_sink=self.event_sink)
        return launcher_injector.stage_windows_injector_toolchain(
            self.dependency, game_root, journal=journal
        )

    def run(
        self,
        game_root: Path,
        *,
        staged_mod: str = "",
        operation: str = "install",
        uninstall_confirmation: Callable[[], bool] | None = None,
        prepared: Path | None = None,
        journal=None,
    ) -> launcher_injector.AdapterResult:
        with launcher_injector.installation_write(game_root, journal):
            return self._run(
                game_root,
                staged_mod=staged_mod,
                operation=operation,
                uninstall_confirmation=uninstall_confirmation,
                prepared=prepared,
                journal=journal,
            )

    def _run(
        self,
        game_root: Path,
        *,
        staged_mod: str = "",
        operation: str = "install",
        uninstall_confirmation: Callable[[], bool] | None = None,
        prepared: Path | None = None,
        journal=None,
    ) -> launcher_injector.AdapterResult:
        self.journal = journal
        platform.require_doom_closed(player_text('launcher.installation_adapters', 'running_eternalmodinjector'))
        try:
            batch_executable = prepared or self.prepare(game_root, journal=journal)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            return launcher_injector._installation_failure(
                launcher_injector.AdapterResult(state="failed", message=""), error, 'prepare banks', self.event_sink
            )

        comspec = os.environ.get("COMSPEC", "cmd.exe")
        target_name = (
            batch_executable.name if batch_executable.parent.resolve() == game_root.resolve() else str(batch_executable)
        )
        command = (comspec, "/d", "/c", target_name)
        _record_command(journal, command)
        result = self._run_prepared_process(game_root, command, staged_mod)
        if operation != "uninstall" or result.state != "applied":
            return result

        return self.confirm_uninstall(result, operation, uninstall_confirmation)

    def confirm_uninstall(self, result, operation, uninstall_confirmation):
        confirmed = False
        confirmation_error = ""
        try:
            confirmed = uninstall_confirmation is not None and bool(uninstall_confirmation())
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            confirmation_error = str(error)
        if self.event_sink is not None:
            self.event_sink(
                "injector_post_run_confirmation",
                operation=operation,
                confirmed=confirmed,
                message=confirmation_error or 'uninstall confirmation received',
            )
        details = {
            **result.details,
            "operation": operation,
            "post_run_confirmed": confirmed,
        }
        if confirmation_error:
            details["post_run_error"] = confirmation_error
        if not confirmed:
            return launcher_injector.AdapterResult(
                state="failed",
                message=(
                    'Removal could not be confirmed. Select Help -> SAVE SUPPORT REPORT.'
                    + (f" {confirmation_error}" if confirmation_error else "")
                ),
                command=result.command,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
                details=details,
            )
        return launcher_injector.AdapterResult(
            state=result.state,
            message='Removal finished.',
            command=result.command,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            details=details,
        )

    def activate(self, game_root: Path, mod_zip: Path) -> launcher_injector.AdapterResult:
        with launcher_injector.installation_write(game_root):
            return self._activate(game_root, mod_zip)

    def _activate(self, game_root: Path, mod_zip: Path) -> launcher_injector.AdapterResult:
        staged = launcher_injector._stage_mod(mod_zip, game_root)
        result = self.run(game_root, staged_mod=staged.name)
        if result.state != "applied":
            return launcher_injector.AdapterResult(
                state="manual_install_required",
                message=player_text('launcher.installation_adapters', 'manual_install_help', value_0=f'{result.message}'),
                command=result.command,
                returncode=result.returncode,
                details={
                    "installation_mode": "windows_injector_assisted",
                    **result.details,
                    **({"batch_returncode": result.returncode} if result.returncode is not None else {}),
                },
            )

        confirmed = self.confirmer()
        if confirmed:
            result.details["installation_mode"] = "windows_injector_assisted"
            return launcher_injector.finalize_mod_installation(game_root, staged, result, event_sink=self.event_sink)

        if self.event_sink is not None:
            self.event_sink("installation_declined", command=list(result.command))

        return launcher_injector.AdapterResult(
            state="manual_install_required",
            message=player_text('launcher.installation_adapters', 'installation_unconfirmed'),
            command=result.command,
            returncode=1,
            details={
                "installation_mode": "windows_injector_assisted",
                "user_confirmed": False,
            },
        )


WindowsModManagerAdapter = WindowsModInjectorAdapter


def _loader_symbol_failure(completed):
    return completed.returncode == 127 and any(
        marker in (completed.stderr or "").casefold()
        for marker in ('symbol lookup error', 'undefined symbol: rl_print_keybinding')
    )


def _residual_prompt(stdout, stderr):
    output = ((stdout or "") + "\n" + (stderr or "")).casefold()
    for marker, prompt in (
        ('old doom eternal backups detected', "old_backups"),
        ('reset backups now?', "reset_backups"),
        ("eternalmodmanager", "modmanager"),
    ):
        if marker in output:
            return prompt
    return None


class LinuxModManagerAdapter:
    """Stage bundled InjectorShell tools and apply mods without launching Steam."""

    TIMEOUT_SECONDS = 15 * 60
    LOADER_ENVIRONMENT_KEYS = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT')

    def __init__(
        self,
        dependency: launcher_injector.InstalledDependency,
        *,
        state_dir: Path | None = None,
        event_sink: Callable[..., None] | None = None,
    ):
        self.dependency = dependency
        self.state_dir = state_dir
        self.event_sink = event_sink

    def _prepare_tools(self, game_root: Path, *, journal=None) -> Path:
        source_root = Path(self.dependency.root)
        executable = Path(self.dependency.executable)
        relative_executable = executable.relative_to(source_root)
        members = tuple(source.relative_to(source_root) for source in sorted(source_root.rglob("*"))
                        if source.is_file() and source.name != launcher_injector.DependencyManager.RECEIPT)
        launcher_injector.check_existing_injector_tools(source_root, game_root, members)
        for relative in members:
            source = source_root / relative
            destination = game_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file():
                continue
            if journal is None:
                shutil.copy2(source, destination)
            else:
                journal.replace(destination, source.read_bytes(), operation="injector_stage")
        prepared = game_root / relative_executable
        prepared.chmod(prepared.stat().st_mode | 0o100)
        return prepared

    @staticmethod
    def _configure_first_run(game_root: Path, *, journal=None) -> None:
        """Configure InjectorShell for explicit launcher-controlled startup."""
        config = game_root / 'EternalModInjector Settings.txt'
        if not config.is_file():
            launcher_injector._write_installer_settings(
                config,
                "\n".join(
                    (
                        ':ASSET_VERSION=2026-04-03',
                        ':AUTO_LAUNCH_GAME=0',
                        ':GAME_PARAMETERS=',
                        ':HAS_CHECKED_RESOURCES=0',
                        ':HAS_READ_FIRST_TIME=1',
                        ':RESET_BACKUPS=0',
                        ':AUTO_UPDATE=0',
                        ':VERBOSE=0',
                        ':SLOW=0',
                        ':COMPRESS_TEXTURES=0',
                        ':DISABLE_MULTITHREADING=0',
                        ':ONLINE_SAFE=0',
                        "",
                    )
                ),
                journal=journal,
            )
            return

        content = config.read_text(encoding="utf-8")
        lines = content.splitlines()
        has_auto_update, has_auto_launch = False, False
        new_lines = []
        for line in lines:
            if line.startswith(":AUTO_UPDATE="):
                new_lines.append(':AUTO_UPDATE=0')
                has_auto_update = True
            elif line.startswith(":AUTO_LAUNCH_GAME="):
                new_lines.append(':AUTO_LAUNCH_GAME=0')
                has_auto_launch = True
            else:
                new_lines.append(line)

        if not has_auto_update:
            new_lines.append(':AUTO_UPDATE=0')
        if not has_auto_launch:
            new_lines.append(':AUTO_LAUNCH_GAME=0')

        launcher_injector._write_installer_settings(config, "\n".join(new_lines) + "\n", journal=journal)

    def prepare(self, game_root: Path, *, journal=None) -> Path:
        platform.require_doom_closed(player_text('launcher.installation_adapters', 'preparing_injectorshell'))
        launcher_injector.recover_injector_resources(game_root)
        launcher_injector.recover_stale_sandbox_hold(game_root, state_dir=self.state_dir, event_sink=self.event_sink)
        executable = self._prepare_tools(game_root, journal=journal)
        self._configure_first_run(game_root, journal=journal)
        return executable

    def run(self, game_root: Path, *, prepared: Path | None = None, journal=None) -> launcher_injector.AdapterResult:
        with launcher_injector.installation_write(game_root, journal):
            return self._run(game_root, prepared=prepared, journal=journal)

    def _run(self, game_root: Path, *, prepared: Path | None = None, journal=None) -> launcher_injector.AdapterResult:
        platform.require_doom_closed(player_text('launcher.installation_adapters', 'running_injectorshell'))
        try:
            executable = prepared or self.prepare(game_root, journal=journal)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            return launcher_injector._installation_failure(
                launcher_injector.AdapterResult(state="failed", message=""), error, 'prepare banks', self.event_sink
            )
        environment = os.environ.copy()
        stripped_loader_environment = [
            key for key in self.LOADER_ENVIRONMENT_KEYS if environment.pop(key, None) is not None
        ]
        environment.update({"skip": "1", "skip_debug_check": "1"})
        command = (str(executable),)
        _record_command(journal, command)
        return self._run_command(game_root, command, environment, stripped_loader_environment, journal)

    def _run_command(self, game_root, command, environment, stripped_loader_environment, journal):
        try:
            with launcher_injector.prepare_injector_resources(game_root, self.event_sink) as hold:
                hold.start_injector()
                completed = self._execute_process(game_root, command, environment, journal)
                hold.finish_injector()
        except subprocess.TimeoutExpired as error:
            return self._timeout_result(game_root, command, error, stripped_loader_environment)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            return launcher_injector._installation_failure(
                launcher_injector.AdapterResult(state="failed", message="", command=command),
                error,
                'prepare banks',
                self.event_sink,
            )
        return self._completed_result(game_root, command, completed, stripped_loader_environment)

    def _execute_process(self, game_root, command, environment, journal):
        process = subprocess.Popen(
            command,
            cwd=game_root,
            env=environment,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        evidence = {"tool": {"path": command[0]}, "injector_pid": process.pid, "injector_group": process.pid}
        if journal is not None:
            journal.document["evidence"].update(injector_pid=process.pid, injector_group=process.pid)
            journal.save()
        try:
            stdout, stderr = process.communicate(timeout=self.TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            error.details = {"recovery_required": True, "injector_state": "unknown"}
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as termination_error:
                error.details["termination_error"] = str(termination_error)
            raise
        finally:
            self._close_output(process)
        check_injector_processes(evidence)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    @staticmethod
    def _close_output(process):
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def _timeout_result(self, game_root, command, error, stripped_loader_environment):
        return launcher_injector.AdapterResult(
            state="timed_out",
            message=text("launcher.setup", "injector_timeout"),
            command=command,
            stdout=(error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""),
            stderr=(error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""),
            details={
                **getattr(error, "details", {}),
                "injector_version": self.dependency.version,
                "mods_path": str((game_root / "Mods").resolve()),
                "post_install_verification": "not_run",
                "loader_environment_removed": stripped_loader_environment,
            },
        )

    def _completed_result(self, game_root, command, completed, stripped_loader_environment):
        state = "applied" if completed.returncode == 0 else "failed"
        loader_symbol_failure = _loader_symbol_failure(completed)
        prompt = _residual_prompt(completed.stdout, completed.stderr) if state == "failed" else None
        if state == "applied":
            message = 'Mod installed. Start through Steam and keep the launcher open.'
        elif loader_symbol_failure:
            message = player_text('launcher.installation_adapters', 'injector_loader_failure')
        elif prompt:
            message = text("launcher.setup", "injector_prompt_" + prompt)
        else:
            message = text("launcher.setup", "injector_failed_linux", code=completed.returncode)
        return launcher_injector.AdapterResult(
            state=state,
            message=message,
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            details={
                "injector_version": self.dependency.version,
                "mods_path": str((game_root / "Mods").resolve()),
                "post_install_verification": "not_run",
                "loader_environment_removed": stripped_loader_environment,
                "failure_category": "linux_loader_symbol_failure" if loader_symbol_failure else None,
                "residual_prompt": prompt,
            },
        )

    def activate(self, game_root: Path, mod_zip: Path) -> launcher_injector.AdapterResult:
        with launcher_injector.installation_write(game_root):
            return self._activate(game_root, mod_zip)

    def _activate(self, game_root: Path, mod_zip: Path) -> launcher_injector.AdapterResult:
        staged = launcher_injector._stage_mod(mod_zip, game_root)
        result = self.run(game_root)
        if result.state != "applied":
            return result
        result = launcher_injector.finalize_mod_installation(game_root, staged, result, event_sink=self.event_sink)
        if result.state != "applied":
            return result
        return launcher_injector.verify_mod_installation(
            game_root, staged, result, event_sink=self.event_sink
        )
