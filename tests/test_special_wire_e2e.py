"""Exercise the production Special codec through the host, probe, and bridge."""

import json
import struct
import subprocess
import sys
from pathlib import Path

from doom_eap.runtime.weapon_points import SentinelWeaponPoints, WeaponPointsBlocked


def test_special_wire(host_exe: Path, probe_exe: Path):
    host = subprocess.Popen([str(host_exe), str(probe_exe), "special-service"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        ready = {}
        for _ in range(6):
            line = host.stdout.readline().strip()
            if not line:
                continue
            ready = json.loads(line)
            if ready.get("ready"):
                break
        assert ready["ready"] and ready["pid"] == host.pid
        namespace = "a" * 64
        link = SentinelWeaponPoints(probe_exe, host.pid, namespace)
        first = link.ensure_progressive_special_weapon(3)
        assert first["outcome"] == 0 and first["native_hammer_perks"] == 0
        assert first["flags"] & 0x4000
        assert link.ensure_progressive_special_weapon(3)["outcome"] == 1

        operations = []
        real_run = link._run

        def invalid_after_execution(args, payload=None):
            result = real_run(args, payload)
            if args == ["--special"] and payload:
                operation = struct.unpack_from("<H", payload, 6)[0]
                operations.append(operation)
                if operation == 38 and result["state"] == 3:
                    error = WeaponPointsBlocked("invalid_response after execution")
                    error.probe_result = "invalid_response"
                    raise error
            return result

        link._run = invalid_after_execution
        try:
            link.ensure_progressive_special_weapon(3)
            assert False, "invalid response was accepted"
        except WeaponPointsBlocked as error:
            assert "invalid_response" in str(error)
        assert operations.count(37) == 1 and operations.count(40) == 1
        try:
            link.ensure_progressive_special_weapon(3)
            assert False, "incompatible runtime was retried"
        except WeaponPointsBlocked as error:
            assert "incompatible" in str(error)
        assert operations.count(37) == 1
        assert link.observe()["outcome"] in (1, 2)

        fresh = SentinelWeaponPoints(probe_exe, host.pid, namespace)
        for _ in range(12):
            assert fresh.ensure_progressive_special_weapon(3)["outcome"] == 1
        assert fresh.observe()["outcome"] in (1, 2)
    finally:
        if host.poll() is None:
            host.stdin.write("quit\n")
            host.stdin.flush()
        stdout, stderr = host.communicate(timeout=10)
        assert host.returncode == 0, (stdout, stderr)


if __name__ == "__main__":
    test_special_wire(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    print("PASS Special wire codec, probe, consumer, cleanup, capacity, and WUP progress")
