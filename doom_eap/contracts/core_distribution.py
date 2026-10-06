"""Verified Core runtime distributions for the MOD consumer."""
import hashlib
import json
import re
import struct
import zipfile
from pathlib import Path, PurePosixPath

ABI = {"base": 1, "wire": 1, "engine": 1, "context": 1, "native": 1, "save": 1,
       "admission": 1, "backup": 1, "installation": 1, "weapon_points": 1,
       "campaign_menu": 1, "inventory": 6, "arsenal": 1, "runes": 1,
       "special": 1, "deathlink": 1, "automap": 1, "commands": 1}
REQUIRED = {"sentinel_core.dll", "msimg32.dll", "sentinel_probe.exe",
            "notices/LICENSE.txt", "notices/MinHook-LICENSE.txt", "notices/MinHook-NOTICE.txt"}
HELPERS = {"prepare_vanilla_backup.py"}
from doom_eap import __version__ as MOD_VERSION
SUPPORTED_CAPABILITIES = {2097152}
REQUIRED_ABIS = {"base", "wire", "engine", "context", "native", "save", "admission", "backup", "installation", "inventory", "commands"}

def version_key(value):
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc-([1-9]\d*))?(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        raise ValueError("Invalid full Core version")
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), 1 if rc is None else 0, int(rc or 0))

def mod_compatible(value):
    minimum = value.get("minimum_launcher_version")
    if minimum and version_key(MOD_VERSION) < version_key(minimum):
        return False
    if "mod_version_range" not in value:
        versions = value["mod_versions"]
        return isinstance(versions, list) and any(version in versions for version in ("0.6.0", MOD_VERSION))
    terms = value["mod_version_range"].split(",")
    if not terms:
        return False
    current = version_key(MOD_VERSION)
    for term in terms:
        match = re.fullmatch(r"(>=|<=|>|<|==)(.+)", term.strip())
        if not match:
            raise ValueError("Invalid MOD version range")
        operator, version = match.groups()
        target = version_key(version)
        if not {">=": current >= target, "<=": current <= target, ">": current > target,
                "<": current < target, "==": current == target}[operator]:
            return False
    return True

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result

def read_manifest(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)
    validate_manifest(value)
    return value

def validate_manifest(value):
    if type(value["schema"]) is not int or value["schema"] != 1 or value["product"] != "sentinel-core":
        raise ValueError("Unsupported distribution")
    key = version_key(value["version"])
    base = ".".join(map(str, key[:3]))
    channel = "stable" if key[3] else "rc"
    if value["base_version"] != base or value["channel"] != channel or type(value["rc_number"]) is not int or value["rc_number"] != key[4]:
        raise ValueError("Inconsistent version fields")
    if not mod_compatible(value) or value["architecture"] != "x64" or value["platform"] != "windows":
        raise ValueError("Incompatible runtime")
    if not isinstance(value["abi"], dict) or not isinstance(value["required_capabilities"], list) or any(type(value["abi"].get(name)) is not int or value["abi"][name] not in
           ((4, 5, 6) if name == "inventory" else (number,)) for name, number in ABI.items() if name in REQUIRED_ABIS) or not set(value["required_capabilities"]) <= SUPPORTED_CAPABILITIES:
        raise ValueError("Incompatible ABI or capability")
    if not re.fullmatch("[a-f0-9]{40}", value["source_commit"]) or not re.fullmatch("[a-f0-9]{64}", value["build_id"]):
        raise ValueError("Invalid source/build identity")
    if value["pair_id"] != value["build_id"]:
        raise ValueError("Inconsistent Core/bootstrap pair")
    names = set()
    for item in value["artifacts"]:
        name = item["path"]
        parts = PurePosixPath(name)
        if not name or parts.is_absolute() or "\\" in name or ":" in name or ".." in parts.parts or str(parts) != name or name.casefold() in names:
            raise ValueError("Unsafe or duplicate artifact name")
        names.add(name.casefold())
        role = "runtime_zip" if name == "sentinel-runtime.zip" else "notice" if name.startswith("notices/") else "bootstrap" if name == "msimg32.dll" else "probe" if name == "sentinel_probe.exe" else "helper" if name in HELPERS else "core"
        if item.get("role") != role:
            raise ValueError("Inconsistent artifact role")
        if type(item["size"]) is not int or not 0 < item["size"] <= 50 * 1024 * 1024 or not re.fullmatch("[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Invalid artifact identity")
    required = {name.casefold() for name in REQUIRED} | {"sentinel-runtime.zip"}
    if not required <= names or any(name not in required | HELPERS and not name.startswith("notices/") for name in names):
        raise ValueError("Incomplete or unexpected artifact set")
    return value

def verify_bytes(name, data, item):
    if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"Artifact mismatch: {name}")
    if name.endswith((".dll", ".exe")):
        if len(data) < 64 or data[:2] != b"MZ":
            raise ValueError("Invalid PE artifact")
        offset = struct.unpack_from("<I", data, 60)[0]
        if offset + 6 > len(data) or data[offset:offset+4] != b"PE\0\0" or struct.unpack_from("<H", data, offset+4)[0] != 0x8664:
            raise ValueError("Artifact is not Windows x64")

def verify_runtime(manifest_path, *, bootstrap_from_archive=False):
    path = Path(manifest_path)
    value = read_manifest(path)
    items = {item["path"]: item for item in value["artifacts"]}
    archive_path = path.parent / "sentinel-runtime.zip"
    verify_bytes("sentinel-runtime.zip", archive_path.read_bytes(), items["sentinel-runtime.zip"])
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(name.casefold() for name in names)) or set(names) != set(items) - {"sentinel-runtime.zip"}:
            raise ValueError("Unexpected runtime ZIP contents")
        if sum(item.file_size for item in archive.infolist()) > 50 * 1024 * 1024:
            raise ValueError("Runtime ZIP exceeds size limit")
        contents = {}
        for name in names:
            data = archive.read(name)
            verify_bytes(name, data, items[name])
            contents[name] = data
        for name, data in contents.items():
            if bootstrap_from_archive and name == "msimg32.dll":
                continue
            if (path.parent / name).read_bytes() != data:
                raise ValueError(f"Direct runtime artifact differs from ZIP: {name}")
    return value, contents

def select_compatible(manifests, *, allow_rc=False):
    accepted = []
    for value in manifests:
        try:
            validate_manifest(value)
        except (ValueError, KeyError, TypeError):
            continue
        if allow_rc or value["channel"] == "stable":
            accepted.append(value)
    return max(accepted, key=lambda value: version_key(value["version"]), default=None)
