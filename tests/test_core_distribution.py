import hashlib
import json
import struct
import zipfile

import pytest

from doom_eap.contracts.core_distribution import ABI, REQUIRED, verify_runtime


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
    document = {"schema": 1, "product": "sentinel-core", "version": "1.0.0-rc-1", "base_version": "1.0.0",
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
