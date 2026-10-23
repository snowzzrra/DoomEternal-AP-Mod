"""Windows process-session owner shared by the native and Proton launchers."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from doom_eap.launcher.launcher_session import APSessionOwner
from doom_eap.launcher.launcher_core import RoomSnapshot
from doom_eap.launcher.launcher_platform import restore_standard_streams
from doom_eap.runtime.observer_lifecycle import windows_game_processes


def respond(document):
    try:
        print(json.dumps(document, ensure_ascii=True), flush=True)
    except OSError as error:
        # windows can report a closed pipe as EINVAL
        if error.errno != errno.EPIPE and not (sys.platform == "win32" and error.errno == errno.EINVAL):
            raise
        sys.stdout = None
        raise SystemExit(1) from None


def main():
    restore_standard_streams(require_pipes=True)
    if "--process-identity" in sys.argv:
        respond({"processes": windows_game_processes()})
        return 0
    owner = None
    native = None
    log = None
    try:
        while raw := sys.stdin.buffer.readline(1024 * 1024 + 1):
            if len(raw) > 1024 * 1024:
                raise ValueError("Session request is too large")
            try:
                request = json.loads(raw)
                action = request["action"]
                if action == "prepare":
                    if owner is not None:
                        raise RuntimeError("A session owner is already prepared")
                    executable = Path(request["client_dir"]) / "ap_client.exe"
                    if not executable.is_file():
                        raise RuntimeError("The packaged Windows integration helper is unavailable")
                    owner = APSessionOwner(Path(request["client_dir"]), Path(request["data_dir"]), Path(request["state_dir"]))
                    snapshot = RoomSnapshot.from_event(request["room"])
                    status = owner.prepare(snapshot, request["config"], recover_prelaunch=bool(request.get("recover_prelaunch")))
                    log = (Path(request["state_dir"]) / "native_client.log").open("ab")
                    native = subprocess.Popen([str(executable), request["config"]["game_root"]],
                                              stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
                    respond({"status": status, "can_close": owner.can_close()})
                elif owner is None:
                    raise RuntimeError("Prepare a session before observing it")
                elif action == "observe":
                    respond({"status": owner.observe(), "can_close": owner.can_close()})
                elif action == "can_close":
                    respond({"can_close": owner.can_close()})
                elif action == "retire":
                    owner.retire()
                    respond({"status": owner.status, "can_close": True})
                    return 0
                else:
                    raise ValueError("Unsupported session action")
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                from doom_eap.launcher.launcher_integration import setup_failure_payload
                respond({"error": str(error), "failure": setup_failure_payload(error), "can_close": False})
    finally:
        if owner is not None:
            while not owner.can_close():
                owner.observe()
                time.sleep(1)
            owner.retire()
        if native is not None and native.poll() is None:
            native.terminate()
            native.wait(timeout=5)
        if log is not None:
            log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
