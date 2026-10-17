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
    document = {"schema": 1, "product": "sentinel-core", "version": "1.1.0-rc-1", "base_version": "1.1.0",
                "channel": "rc", "rc_number": 1, "mod_version_range": ">=0.7.0,<0.8.0", "minimum_launcher_version": "0.7.0",
                "architecture": "x64", "platform": "windows",
                "abi": dict(ABI), "required_capabilities": [2097152, 4194304, 8388608], "source_commit": "a" * 40,
                "source_dirty": False, "build_id": "b" * 64, "pair_id": "b" * 64, "artifacts": artifacts}
    (root / "distribution.json").write_text(json.dumps(document), encoding="utf-8")
    return document


def test_runtime_rejects_direct_artifact_drift(tmp_path):
    fixture(tmp_path)
    verify_runtime(tmp_path / "distribution.json")
    (tmp_path / "sentinel_core.dll").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="differs from ZIP"):
        verify_runtime(tmp_path / "distribution.json")


@pytest.mark.parametrize("base,inventory_abi", [("1.1.0", 7), ("1.1.1", 7), ("1.1.99", 7)])
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


@pytest.mark.parametrize("base,inventory_abi", [("1.1.0", 3), ("1.1.0", 8), ("1.1.1", 99)])
def test_mismatched_inventory_distribution_is_refused(tmp_path, base, inventory_abi):
    manifest = fixture(tmp_path)
    manifest.update(version=base + "-rc-1", base_version=base, abi={**ABI, "inventory": inventory_abi})
    (tmp_path / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Incompatible"):
        verify_runtime(tmp_path / "distribution.json")


@pytest.mark.parametrize("version,mod_range", [("1.0.5", ">=0.7.0,<0.8.0"),
                                               ("1.1.0", ">=0.6.0,<0.7.0"),
                                               ("1.2.0", ">=0.7.0,<0.8.0")])
def test_distribution_requires_current_core_and_mod_lines(tmp_path, version, mod_range):
    manifest = fixture(tmp_path)
    manifest.update(version=version, base_version=version, channel="stable", rc_number=0,
                    mod_version_range=mod_range)
    (tmp_path / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Incompatible runtime"):
        verify_runtime(tmp_path / "distribution.json")


@pytest.mark.parametrize("name,version", [("combat", None), ("combat", 1), ("combat", 3),
                                          ("combat_presentation", None), ("combat_presentation", 2),
                                          ("enemy_movement", None), ("enemy_movement", 0), ("enemy_movement", 2),
                                          ("enemy_supply", None), ("enemy_supply", 0), ("enemy_supply", 2),
                                          ("enemy_limits", None), ("enemy_limits", 0), ("enemy_limits", 2),
                                          ("enemy_tracking", None), ("enemy_tracking", 0), ("enemy_tracking", 2)])
def test_distribution_refuses_incompatible_combat_before_reading_artifacts(tmp_path, name, version):
    manifest = fixture(tmp_path)
    if version is None:
        del manifest["abi"][name]
    else:
        manifest["abi"][name] = version
    (tmp_path / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "sentinel-runtime.zip").unlink()
    with pytest.raises(ValueError, match="Incompatible ABI or capability"):
        verify_runtime(tmp_path / "distribution.json")


def test_distribution_refuses_unknown_required_capability(tmp_path):
    manifest = fixture(tmp_path)
    manifest["required_capabilities"].append(1 << 24)
    (tmp_path / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Incompatible ABI or capability"):
        verify_runtime(tmp_path / "distribution.json")


def test_core_self_test_reports_compatible_version_range(tmp_path, capsys):
    from doom_eap.launcher.launcher_app import main

    fixture(tmp_path)
    assert main(["--self-test", "--core-only", "--core-runtime", str(tmp_path / "distribution.json")]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["mod_version_range"] == ">=0.7.0,<0.8.0"
    assert report["abi"]["combat"] == 2
