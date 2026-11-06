"""Windows process-session owner shared by the native and Proton launchers."""

import errno
import json
import subprocess
import sys
import time
from pathlib import Path

from doom_eap.launcher.launcher_core import RoomSnapshot
from doom_eap.launcher.launcher_platform import restore_standard_streams
from doom_eap.launcher.launcher_session import SESSION_PIPE_LIMIT, APSessionOwner, stop_native_client
from doom_eap.runtime.observer_lifecycle import same_executable_path, windows_game_processes


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
    owner = None
    native = None
    log = None
    try:
        while raw := sys.stdin.buffer.readline(SESSION_PIPE_LIMIT + 1):
            if len(raw) > SESSION_PIPE_LIMIT:
                raise ValueError('Session request is too large')
            try:
                request = json.loads(raw)
                action = request["action"]
                if action == "prepare":
                    if owner is not None:
                        raise RuntimeError('A session owner is already prepared')
                    executable = Path(request["client_dir"]) / "ap_client.exe"
                    if not executable.is_file():
                        raise RuntimeError('The packaged Windows integration helper is unavailable')
                    command = [str(executable), request["config"]["game_root"]]
                    owner = APSessionOwner(Path(request["client_dir"]), Path(request["data_dir"]), Path(request["state_dir"]))
                    snapshot = RoomSnapshot.from_event(request["room"])
                    status = owner.prepare(snapshot, request["config"], recover_prelaunch=bool(request.get("recover_prelaunch")))
                    log = (Path(request["state_dir"]) / "native_client.log").open("ab")
                    respond({"status": status, "can_close": owner.can_close()})
                elif owner is None:
                    raise RuntimeError('Prepare a session before observing it')
                elif action == "observe":
                    status = owner.observe()
                    can_close = owner.can_close()
                    native = observe_native_client(native, command, log, status, can_close)
                    respond({"status": status, "can_close": can_close})
                elif action == "can_close":
                    respond({"can_close": owner.can_close()})
                elif action == "retire":
                    owner.retire()
                    respond({"status": owner.status, "can_close": True})
                    return 0
                else:
                    raise ValueError('Unsupported session action')
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                respond(failure_response(owner, error))
    finally:
        if owner is not None:
            while not owner.can_close():
                owner.observe()
                time.sleep(1)
            owner.retire()
        stop_native_client(native)
        if log is not None:
            log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
