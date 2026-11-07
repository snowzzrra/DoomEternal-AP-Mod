"""Windows process-session owner shared by the native and Proton launchers."""

import errno
import json
import subprocess
import sys
import time
from pathlib import Path

from doom_eap.launcher.launcher_core import RoomSnapshot
from doom_eap.launcher.launcher_injector import restore_standard_streams
from doom_eap.launcher.launcher_session import SESSION_PIPE_LIMIT, APSessionOwner, stop_native_client
from doom_eap.runtime.observers import same_executable_path, windows_game_processes


def respond(document):
    try:
        print(json.dumps(document, ensure_ascii=True), flush=True)
    except OSError as error:
        # windows can report a closed pipe as EINVAL
        if error.errno != errno.EPIPE and not (sys.platform == "win32" and error.errno == errno.EINVAL):
            raise
        sys.stdout = None
        raise SystemExit(1) from None


def failure_response(owner, error):
    from doom_eap.launcher.launcher_integration import setup_failure_payload

    return {"error": str(error), "failure": setup_failure_payload(error),
            "status": owner.status if owner else {"state": "not_prepared", "ready": False},
            "can_close": owner.can_close() if owner else True}


def observe_native_client(native, command, log, status, can_close):
    if can_close:
        stop_native_client(native)
        return None
    if status.get("pid") and (native is None or native.poll() is not None):
        return subprocess.Popen(command, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    return native


class SessionOwnerServer:
    def __init__(self):
        self.owner = None
        self.native = None
        self.log = None
        self.command = None

    def prepare(self, request):
        if self.owner is not None:
            raise RuntimeError('A session owner is already prepared')
        executable = Path(request["client_dir"]) / "ap_client.exe"
        if not executable.is_file():
            raise RuntimeError('The packaged Windows integration helper is unavailable')
        self.command = [str(executable), request["config"]["game_root"]]
        self.owner = APSessionOwner(Path(request["client_dir"]), Path(request["data_dir"]), Path(request["state_dir"]))
        snapshot = RoomSnapshot.from_event(request["room"])
        status = self.owner.prepare(snapshot, request["config"], recover_prelaunch=bool(request.get("recover_prelaunch")))
        self.log = (Path(request["state_dir"]) / "native_client.log").open("ab")
        respond({"status": status, "can_close": self.owner.can_close()})

    def dispatch(self, request):
        action = request["action"]
        if action == "prepare":
            self.prepare(request)
        elif self.owner is None:
            raise RuntimeError('Prepare a session before observing it')
        elif action == "observe":
            status = self.owner.observe()
            can_close = self.owner.can_close()
            self.native = observe_native_client(self.native, self.command, self.log, status, can_close)
            respond({"status": status, "can_close": can_close})
        elif action == "can_close":
            respond({"can_close": self.owner.can_close()})
        elif action == "retire":
            self.owner.retire()
            respond({"status": self.owner.status, "can_close": True})
            return True
        else:
            raise ValueError('Unsupported session action')
        return False

    def close(self):
        if self.owner is not None:
            while not self.owner.can_close():
                self.owner.observe()
                time.sleep(1)
            self.owner.retire()
        stop_native_client(self.native)
        if self.log is not None:
            self.log.close()

    def run(self):
        try:
            while raw := sys.stdin.buffer.readline(SESSION_PIPE_LIMIT + 1):
                if len(raw) > SESSION_PIPE_LIMIT:
                    raise ValueError('Session request is too large')
                try:
                    if self.dispatch(json.loads(raw)):
                        return 0
                except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                    respond(failure_response(self.owner, error))
        finally:
            self.close()
        return 0

def main():
    restore_standard_streams(require_pipes=True)
    if "--process-identity" in sys.argv:
        processes = windows_game_processes()
        if "--expected-game-exe" in sys.argv:
            expected = sys.argv[sys.argv.index("--expected-game-exe") + 1]
            processes = tuple({**row, "same_executable": same_executable_path(row["path"], expected)}
                              for row in processes) if processes is not None else None
        respond({"processes": processes})
        return 0
    return SessionOwnerServer().run()


if __name__ == "__main__":
    raise SystemExit(main())
