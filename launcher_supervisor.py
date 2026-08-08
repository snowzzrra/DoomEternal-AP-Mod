"""Subprocess supervisor used by CLI now and future PySide6 UI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any

from launcher_core import LaunchWorkflow, RoomSnapshot


EVENT_PREFIX = "AP_EVENT "


class BridgeState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


class BridgeSupervisor:
    _profiles: set[str] = set()
    _profiles_lock = threading.Lock()

    def __init__(self, *, client: Path, workflow: LaunchWorkflow, install_root: Path, profile_id: str, log_limit: int = 200):
        if log_limit < 1:
            raise ValueError("log_limit must be positive")
        self.client = client
        self.workflow = workflow
        self.install_root = install_root
        self.profile_id = profile_id
        self.state = BridgeState.STOPPED
        self.logs: deque[str] = deque(maxlen=log_limit)
        self.last_error: dict[str, str] | None = None
        self.last_snapshot: RoomSnapshot | None = None
        self.install_record = None
        self._process: subprocess.Popen[str] | None = None
        self._password = ""
        self._intentional_stop = False

    def start(self, *, endpoint: str, player: str, password: str = "") -> None:
        if not self.client.is_file():
            raise ValueError(f"bridge client not found: {self.client}")
        if self._process and self._process.poll() is None:
            raise RuntimeError("bridge is already running")
        with self._profiles_lock:
            if self.profile_id in self._profiles:
                raise RuntimeError(f"profile already supervised: {self.profile_id}")
            self._profiles.add(self.profile_id)
        self._password = password
        self._intentional_stop = False
        self.state = BridgeState.STARTING
        command = [str(self.client)]
        if self.client.suffix == ".py":
            command.insert(0, sys.executable)
        command.extend(["--connect", endpoint, "--name", player])
        environment = os.environ.copy()
        environment["DOOM_AP_LAUNCHER_EVENTS"] = "1"
        if password:
            environment["DOOM_AP_PASSWORD"] = password
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
        except Exception:
            with self._profiles_lock:
                self._profiles.discard(self.profile_id)
            self.state = BridgeState.FAILED
            raise
        threading.Thread(target=self._read_stream, args=(self._process.stdout, True, endpoint), daemon=True).start()
        threading.Thread(target=self._read_stream, args=(self._process.stderr, False, endpoint), daemon=True).start()
        threading.Thread(target=self._watch_process, daemon=True).start()

    def _redact(self, line: str) -> str:
        return line.replace(self._password, "[REDACTED]") if self._password else line

    def _read_stream(self, stream, structured: bool, endpoint: str) -> None:
        if stream is None:
            return
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\r\n")
            if structured and line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                    self._handle_event(event, endpoint)
                except Exception as error:
                    self.last_error = {"code": "invalid_bridge_event", "message": str(error)}
                    self.state = BridgeState.FAILED
                continue
            self.logs.append(self._redact(line))

    def _handle_event(self, event: dict[str, Any], endpoint: str) -> None:
        event_type = event.get("type")
        if event_type == "client_started":
            self.state = BridgeState.STARTING
        elif event_type == "connecting":
            self.state = BridgeState.CONNECTING
        elif event_type == "connected":
            snapshot = RoomSnapshot.from_event(event)
            self.install_record = self.workflow.execute(snapshot, self.install_root, endpoint)
            self.workflow.write_client_config(
                self.client.parent,
                endpoint=endpoint,
                manifest_hash=self.install_record.manifest_hash,
            )
            self.last_snapshot = snapshot
            self.state = BridgeState.CONNECTED
        elif event_type == "disconnected":
            self.state = BridgeState.DISCONNECTED
        elif event_type == "error":
            self.last_error = {
                "code": str(event.get("code", "bridge_error")),
                "message": self._redact(str(event.get("message", "bridge error"))),
            }
            self.state = BridgeState.FAILED
        elif event_type == "client_stopping":
            self.state = BridgeState.STOPPING

    def _watch_process(self) -> None:
        process = self._process
        if process is None:
            return
        return_code = process.wait()
        with self._profiles_lock:
            self._profiles.discard(self.profile_id)
        if self._intentional_stop:
            self.state = BridgeState.STOPPED
        else:
            self.last_error = {
                "code": "bridge_exited",
                "message": f"bridge exited unexpectedly with code {return_code}",
            }
            self.state = BridgeState.FAILED

    def stop(self, timeout: float = 5.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            self.state = BridgeState.STOPPED
            return
        self._intentional_stop = True
        self.state = BridgeState.STOPPING
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        with self._profiles_lock:
            self._profiles.discard(self.profile_id)
        self.state = BridgeState.STOPPED

    def restart(self, *, endpoint: str, player: str, password: str = "") -> None:
        self.stop()
        self.start(endpoint=endpoint, player=player, password=password)
