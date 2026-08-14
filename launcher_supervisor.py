"""Supervise one headless Archipelago bridge worker and structured IPC."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from launcher_platform import redact_secrets

EVENT_PREFIX = "AP_EVENT "


class BridgeState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class BridgeSupervisor:
    """Process/IPC owner. Setup work is deliberately outside reader threads."""

    _profiles: ClassVar[set[str]] = set()
    _profiles_lock = threading.Lock()

    def __init__(
        self,
        *,
        entrypoint: Path,
        application_dir: Path,
        config_path: Path,
        profile_id: str,
        event_sink: Callable[[dict[str, Any]], None],
        log_sink: Callable[[str], None],
        frozen: bool | None = None,
        archipelago_source: Path | None = None,
        log_limit: int = 500,
    ):
        self.entrypoint = entrypoint
        self.application_dir = application_dir
        self.config_path = config_path
        self.profile_id = profile_id
        self.event_sink = event_sink
        self.log_sink = log_sink
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self.archipelago_source = archipelago_source
        self.state = BridgeState.STOPPED
        self.logs: deque[str] = deque(maxlen=log_limit)
        self.last_error: dict[str, str] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._password = ""
        self._intentional_stop = False
        self._emit_disconnected = True
        self._failure_emitted = False
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stopped = threading.Event()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _command(self, endpoint: str, player: str) -> list[str]:
        worker_args = [
            "--bridge-worker",
            "--nogui",
            "--connect",
            endpoint,
            "--name",
            player,
        ]
        if self.frozen:
            return [str(self.entrypoint), *worker_args]
        return [sys.executable, str(self.entrypoint), *worker_args]

    def start(self, *, endpoint: str, player: str, password: str = "") -> None:
        if self.running:
            raise RuntimeError("bridge worker is already running")
        with self._profiles_lock:
            if self.profile_id in self._profiles:
                raise RuntimeError(f"profile already supervised: {self.profile_id}")
            self._profiles.add(self.profile_id)
        self._password = password
        self._intentional_stop = False
        self._emit_disconnected = True
        self._failure_emitted = False
        self._stopped.clear()
        self.state = BridgeState.STARTING
        environment = os.environ.copy()
        environment.update(
            {
                "DOOM_AP_LAUNCHER_EVENTS": "1",
                "DOOM_AP_PASSWORD": password,
                "DOOM_AP_CONFIG_FILE": str(self.config_path),
                "DOOM_AP_APPLICATION_DIR": str(self.application_dir),
                "PYTHONUNBUFFERED": "1",
                "SKIP_REQUIREMENTS_UPDATE": "1",
            }
        )
        if self.archipelago_source is not None:
            environment["ARCHIPELAGO_SOURCE"] = str(self.archipelago_source)
        try:
            self._process = subprocess.Popen(
                self._command(endpoint, player),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.application_dir,
                env=environment,
            )
        except Exception:
            with self._profiles_lock:
                self._profiles.discard(self.profile_id)
            self.state = BridgeState.FAILED
            raise
        threading.Thread(
            target=self._read_stream,
            args=(self._process.stdout, True),
            name="DoomBridgeStdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stream,
            args=(self._process.stderr, False),
            name="DoomBridgeStderr",
            daemon=True,
        ).start()
        threading.Thread(target=self._watch_process, name="DoomBridgeWait", daemon=True).start()

    def _redact(self, text: str) -> str:
        return redact_secrets(text, [self._password])

    def _read_stream(self, stream, structured: bool) -> None:
        if stream is None:
            return
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\r\n")
            if structured and line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX) :])
                    if not isinstance(event, dict):
                        raise ValueError("worker event must be an object")
                    self._accept_event(event)
                except Exception as error:
                    self.last_error = {
                        "code": "invalid_bridge_event",
                        "message": self._redact(str(error)),
                    }
                    self.state = BridgeState.FAILED
                    self._failure_emitted = True
                    self.event_sink({"type": "setup_failed", **self.last_error})
                continue
            redacted = self._redact(line)
            self.logs.append(redacted)
            self.log_sink(redacted)

    def _accept_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "client_started":
            self.state = BridgeState.STARTING
        elif event_type == "connecting":
            self.state = BridgeState.CONNECTING
        elif event_type == "connected":
            self.state = BridgeState.CONNECTED
        elif event_type == "disconnected":
            self.state = BridgeState.DISCONNECTED
        elif event_type == "error":
            self.state = BridgeState.FAILED
            self._failure_emitted = True
            self.last_error = {
                "code": str(event.get("code", "bridge_error")),
                "message": self._redact(str(event.get("message", "bridge error"))),
            }
            event = {**event, **self.last_error}
        elif event_type == "client_stopping":
            self.state = BridgeState.STOPPING
        self.event_sink(event)

    def send_command(self, text: str) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("bridge worker is not running")
        with self._write_lock:
            process.stdin.write(text.rstrip("\r\n") + "\n")
            process.stdin.flush()

    def _watch_process(self) -> None:
        process = self._process
        if process is None:
            return
        return_code = process.wait()
        with self._profiles_lock:
            self._profiles.discard(self.profile_id)
        with self._lifecycle_lock:
            intentional_stop = self._intentional_stop
            emit_disconnected = self._emit_disconnected
        if intentional_stop:
            self.state = BridgeState.STOPPED
            if emit_disconnected:
                self.event_sink({"type": "disconnected", "intentional": True})
        else:
            self.last_error = {
                "code": "bridge_exited",
                "message": f"bridge worker exited with code {return_code}",
            }
            self.state = BridgeState.FAILED
            if not self._failure_emitted:
                self._failure_emitted = True
                self.event_sink({"type": "setup_failed", **self.last_error})
        self._stopped.set()
        self.event_sink(
            {
                "type": "worker_stopped",
                "intentional": intentional_stop,
                "returncode": return_code,
            }
        )

    def stop(self, timeout: float = 5.0, *, emit_disconnected: bool = True) -> None:
        """Request shutdown and return without waiting for worker process."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        with self._lifecycle_lock:
            if self._intentional_stop:
                return
            self._intentional_stop = True
            self._emit_disconnected = emit_disconnected
            self.state = BridgeState.STOPPING
        threading.Thread(
            target=self._stop_process,
            args=(process, timeout),
            name="DoomBridgeStop",
            daemon=True,
        ).start()

    def _stop_process(self, process: subprocess.Popen[str], timeout: float) -> None:
        try:
            self.send_command("/exit")
        except (BrokenPipeError, RuntimeError):
            pass
        if self._stopped.wait(timeout):
            return
        try:
            process.terminate()
        except OSError:
            pass
        if self._stopped.wait(timeout):
            return
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


class NativeClientSupervisor:
    """Own one launcher-started ap_client process and its runtime PID record."""

    def __init__(
        self,
        *,
        executable: Path,
        game_root: Path,
        state_dir: Path,
        event_sink: Callable[[dict[str, Any]], None],
        delay: float = 12.0,
    ):
        self.executable = executable.resolve()
        self.game_root = game_root.resolve()
        self.state_dir = state_dir
        self.event_sink = event_sink
        self.delay = max(0.0, delay)
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._owner_token = uuid.uuid4().hex

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "ap_client.pid.json"

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def _record(self, process: subprocess.Popen[bytes]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.pid_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "owner": self._owner_token,
                    "pid": process.pid,
                    "executable": str(self.executable),
                    "game_root": str(self.game_root),
                    "started": time.time(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.pid_path)

    def _clear_record(self, process: subprocess.Popen[bytes]) -> None:
        try:
            record = json.loads(self.pid_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {}
        if record.get("owner") == self._owner_token and record.get("pid") == process.pid:
            try:
                self.pid_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def start(self) -> None:
        with self._lock:
            if self.running or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError("ap_client is already supervised")
            self._stop_requested.clear()
            self._thread = threading.Thread(
                target=self._start_after_delay,
                name="DoomNativeClientStart",
                daemon=True,
            )
            self._thread.start()

    def _start_after_delay(self) -> None:
        if self._stop_requested.wait(self.delay):
            return
        try:
            process = subprocess.Popen(
                [str(self.executable), str(self.game_root)],
                cwd=self.game_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self.event_sink({"type": "ap_client_failed", "message": str(error)})
            return
        with self._lock:
            if self._stop_requested.is_set():
                self._terminate_owned(process, 5.0)
                return
            self._process = process
            try:
                self._record(process)
            except OSError as error:
                self._terminate_owned(process, 5.0)
                self.event_sink({"type": "ap_client_failed", "message": str(error)})
                return
        self.event_sink({"type": "ap_client_started", "pid": process.pid})
        threading.Thread(target=self._watch, args=(process,), name="DoomNativeClientWait", daemon=True).start()

    def _watch(self, process: subprocess.Popen[bytes]) -> None:
        returncode = process.wait()
        with self._lock:
            if self._process is process:
                self._process = None
                self._clear_record(process)
        self.event_sink(
            {
                "type": "ap_client_stopped",
                "pid": process.pid,
                "returncode": returncode,
                "intentional": self._stop_requested.is_set(),
            }
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_requested.set()
        timeout = max(0.1, timeout)
        deadline = time.monotonic() + timeout
        spawn_thread = self._thread
        if spawn_thread is not None and spawn_thread is not threading.current_thread():
            spawn_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        remaining = max(0.0, deadline - time.monotonic())
        if not self._lock.acquire(timeout=remaining):
            return
        try:
            process = self._process
        finally:
            self._lock.release()
        if process is None:
            return
        self._terminate_owned(process, timeout)

    def _terminate_owned(self, process: subprocess.Popen[bytes], timeout: float) -> None:
        if process.poll() is not None:
            self._clear_record(process)
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return
        self._clear_record(process)
