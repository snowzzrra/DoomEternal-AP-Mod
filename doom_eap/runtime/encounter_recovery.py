"""Transient encounter recovery over the process-authenticated typed transport."""
import secrets
import struct

from doom_eap.runtime.weapon_points import WeaponPointsBlocked

CAPABILITY = 16777216
OBSERVE, SUBMIT, RESULT, RELEASE = 52, 53, 54, 55
MESSAGES = {
    1: "Encounter finished",
    2: "No active encounter",
    3: "Encounter changed; request cancelled",
    4: "Unsupported encounter",
    5: "Recovery outcome unknown",
}


def finish_encounter(link, *, trace=None):
    trace = trace if trace is not None else []
    response = {"message": MESSAGES[5]}
    try:
        scope = link._run(["--pid", str(link.pid), "--native", "--json"])
        if scope.get("availability") != "enabled" or scope.get("lifecycle") != "active":
            return {"message": MESSAGES[2]}
        request_id, nonce = secrets.randbits(63) or 1, secrets.token_bytes(16)
        response.update(request_id=str(request_id), pid=link.pid, namespace=link.namespace,
                        build=scope.get("build_id"))
        body = bytes(112)
        payload = link._typed_payload(CAPABILITY, scope, request_id, nonce, body)
        message = struct.pack("<IHHII", 0x50494353, 1, OBSERVE, len(payload), 0) + payload
        observed = link._run(["--finish-encounter"], message)
        trace.append({"stage": "target", "response": observed})
        if (observed.get("namespace") != link.namespace or observed.get("build_id") != scope.get("build_id")
                or int(observed["request_id"]) != request_id or observed.get("nonce") != nonce.hex()):
            trace.append({"stage": "error", "error": "recovery target identity mismatch"})
            return {**response, "message": MESSAGES[3]}
        save = observed["save"].encode("ascii")
        if len(save) > 95 or b"\0" in save:
            raise ValueError("invalid recovery save")
        body = struct.pack("<96sQQ", save, int(observed["token"]), int(observed["stamp"]))
        try:
            result = link._execute_typed(CAPABILITY, "--finish-encounter", SUBMIT, RESULT, RELEASE,
                                         body, trace=trace, prepared=(scope, request_id, nonce))
        except WeaponPointsBlocked as error:
            result = getattr(error, "native_result", None)
            if result is None:
                raise
        response["message"] = MESSAGES[int(result["outcome"])]
        response.update(token=observed["token"], stamp=observed["stamp"], save=observed["save"])
    except WeaponPointsBlocked as error:
        if getattr(error, "probe_result", None) == "capability_unavailable" or getattr(error, "returncode", None) == 2:
            response.update(message=MESSAGES[4], note="The running Core or its probe does not support encounter recovery.")
        elif getattr(error, "probe_result", None) == "process_mismatch":
            response["message"] = MESSAGES[3]
        trace.append({"stage": "error", "error": str(error)})
    except (KeyError, TypeError, ValueError, OverflowError, struct.error) as error:
        trace.append({"stage": "error", "error": str(error)})
    return response
