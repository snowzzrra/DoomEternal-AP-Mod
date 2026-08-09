"""Supervise one headless Archipelago bridge worker and structured IPC."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
        self._write_lock = threading.Lock()

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
        if self._intentional_stop:
            self.state = BridgeState.STOPPED
            self.event_sink({"type": "disconnected", "intentional": True})
        else:
            self.state = BridgeState.FAILED
            self.last_error = {
                "code": "bridge_exited",
                "message": f"bridge worker exited with code {return_code}",
            }
            self.event_sink({"type": "setup_failed", **self.last_error})

    def stop(self, timeout: float = 5.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            self.state = BridgeState.STOPPED
            return
        self._intentional_stop = True
        self.state = BridgeState.STOPPING
        try:
            self.send_command("/exit")
            process.wait(timeout=timeout)
        except (BrokenPipeError, RuntimeError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout)
        finally:
            with self._profiles_lock:
                self._profiles.discard(self.profile_id)
            self.state = BridgeState.STOPPED
