import hashlib
import json
import struct
import zipfile
from types import SimpleNamespace

import pytest

from doom_eap.contracts.core_distribution import ABI, REQUIRED, verify_runtime
from doom_eap.runtime.weapon_points import SentinelWeaponPoints, WeaponPointsBlocked


def fixture(root):
    pe = bytearray(128)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 60, 64)
    pe[64:68] = b"PE\0\0"
    struct.pack_into("<H", pe, 68, 0x8664)
    artifacts = []
    with zipfile.ZipFile(root / "sentinel-runtime.zip", "w") as archive:
        for name in sorted(REQUIRED):
            data = bytes(pe) if name.endswith((".dll", ".exe")) else b"Model fixture notice\n"
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            archive.writestr(name, data)
            role = "notice" if name.startswith("notices/") else "bootstrap" if name == "msimg32.dll" else "probe" if name.endswith(".exe") else "core"
            artifacts.append({"path": name, "role": role, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    data = (root / "sentinel-runtime.zip").read_bytes()
    artifacts.append({"path": "sentinel-runtime.zip", "role": "runtime_zip", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    document = {"schema": 1, "product": "sentinel-core", "version": "1.0.2-rc-1", "base_version": "1.0.2",
                "channel": "rc", "rc_number": 1, "mod_versions": ["0.6.0"], "architecture": "x64", "platform": "windows",
                "abi": ABI, "required_capabilities": [2097152], "source_commit": "a" * 40,
                "source_dirty": False, "build_id": "b" * 64, "pair_id": "b" * 64, "artifacts": artifacts}
    (root / "distribution.json").write_text(json.dumps(document), encoding="utf-8")
    return document


def test_runtime_rejects_direct_artifact_drift(tmp_path):
    fixture(tmp_path)
    verify_runtime(tmp_path / "distribution.json")
    (tmp_path / "sentinel_core.dll").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="differs from ZIP"):
        verify_runtime(tmp_path / "distribution.json")


@pytest.mark.parametrize("base,inventory_abi", [("1.0.0", 4), ("1.0.0", 5), ("1.0.1", 6), ("1.0.2", 6), ("1.0.3", 6), ("1.0.4", 6)])
@pytest.mark.parametrize("suffix", ["", "-rc-1"])
def test_matched_inventory_distributions(tmp_path, monkeypatch, base, inventory_abi, suffix):
    manifest = fixture(tmp_path)
    manifest["version"] = base + suffix
    manifest["base_version"] = base
    manifest.update(channel="rc" if suffix else "stable", rc_number=1 if suffix else 0)
    manifest["abi"] = {**manifest["abi"], "inventory": inventory_abi}
    (tmp_path / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
    accepted, _ = verify_runtime(tmp_path / "distribution.json")
    assert accepted["abi"]["inventory"] == inventory_abi
    response = {"result": "ok", "core_version": accepted["version"], "build_id": accepted["build_id"]}
    monkeypatch.setattr("doom_eap.runtime.weapon_points.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(response).encode(), stderr=b""))
    link = SentinelWeaponPoints(tmp_path / "sentinel_probe.exe", 7, "a" * 64)
    assert link._run(["--pid", "7", "--native", "--json"]) == response


@pytest.mark.parametrize("version,result,returncode,predicate", [
    ("1.0.2", "refused", 0, "exit_or_result"),
    ("1.0.2", "ok", 1, "exit_or_result"),
])
def test_native_probe_refuses_failed_response(tmp_path, monkeypatch, version, result, returncode, predicate):
    probe = tmp_path / "sentinel_probe.exe"
    probe.write_bytes(b"isolated probe")
    response = {"result": result, "core_version": version}
    monkeypatch.setattr("doom_eap.runtime.weapon_points.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=json.dumps(response).encode(), stderr=b""))
    link = SentinelWeaponPoints(probe, 7, "a" * 64)
    with pytest.raises(WeaponPointsBlocked, match=predicate):
        link._run(["--pid", "7", "--native", "--json"])


@pytest.mark.parametrize("base,inventory_abi", [("1.0.0", 3), ("1.0.1", 7), ("1.0.4", 99)])
def test_mismatched_inventory_distribution_is_refused(tmp_path, base, inventory_abi):
    manifest = fixture(tmp_path)
    manifest.update(version=base + "-rc-1", base_version=base, abi={**ABI, "inventory": inventory_abi})
    (tmp_path / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Incompatible"):
        verify_runtime(tmp_path / "distribution.json")
