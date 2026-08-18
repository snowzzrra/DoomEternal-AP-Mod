import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from doom_eap.launcher.launcher_core import RoomCompiler
from scripts.release.assemble_ci_artifact import (
    assemble_platform_release,
    audit_final_zip,
    audit_platform_parity,
    sha256_file,
    validate_handoff_structure,
    validate_room_resources,
    write_deterministic_zip,
)
from tools.release.release_manifest import MANIFEST_FILENAME, validate_release_manifest
from tools.validation.release_layout import ROOM_COMPILER_RESOURCE_FILES, public_file_members

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_LOCAL_RESOURCES = REPO_ROOT / "build" / "release" / "client" / "resources"


def create_fake_handoff(
    target_dir: Path,
    *,
    version: str = "v0.4.0-beta.4",
    mod_sha: str = "790ddfb80f40c98d090ed9545372c8105025e6ef",
    apworld_sha: str = "0cab6158bbd2683cd87b48f068a4c7ee8262c087",
    corrupt_hash: bool = False,
    omit_linux_launcher: bool = False,
    omit_win_launcher: bool = False,
    omit_ap_client: bool = False,
    bad_elf: bool = False,
    bad_mz: bool = False,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    linux_dir = target_dir / "linux"
    win_dir = target_dir / "windows"
    shared_dir = target_dir / "shared"
    shared_client_dir = shared_dir / "client"

    linux_dir.mkdir(parents=True, exist_ok=True)
    win_dir.mkdir(parents=True, exist_ok=True)
    shared_client_dir.mkdir(parents=True, exist_ok=True)

    # Linux launcher
    if not omit_linux_launcher:
        lin_content = b"\x7fELF" + b"\x00" * 100
        if bad_elf:
            lin_content = b"NOT_ELF" + b"\x00" * 100
        (linux_dir / "DoomEternalArchipelagoLauncher").write_bytes(lin_content)

    # Windows launcher
    if not omit_win_launcher:
        win_content = b"MZ" + b"\x00" * 100
        if bad_mz:
            win_content = b"NOT_MZ" + b"\x00" * 100
        (win_dir / "DoomEternalArchipelagoLauncher.exe").write_bytes(win_content)

    # APWorld (valid zip)
    apworld_path = shared_dir / "doometernal.apworld"
    with zipfile.ZipFile(apworld_path, "w") as zf:
        zf.writestr("doometernal/__init__.py", "# test apworld\n")

    # Native ap_client.exe
    if not omit_ap_client:
        client_content = b"MZ" + b"\x00" * 200
        (shared_client_dir / "ap_client.exe").write_bytes(client_content)

    # BUILD-MANIFEST.json
    manifest = {
        "schema_version": 1,
        "version_label": version,
        "mod": {
            "requested_ref": mod_sha,
            "resolved_sha": mod_sha,
        },
        "apworld": {
            "requested_ref": apworld_sha,
            "resolved_sha": apworld_sha,
        },
        "build": {
            "linux_runner": "ubuntu-latest",
            "windows_runner": "windows-latest",
            "python_version_linux": "3.12.0",
            "python_version_windows": "3.12.0",
            "pyinstaller_version_linux": "6.5.0",
            "pyinstaller_version_windows": "6.5.0",
            "native_client_toolchain_identity": "x86_64-w64-mingw32-gcc (GCC) 13.2.0",
        },
    }
    (target_dir / "BUILD-MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # SHA256SUMS.txt
    lines = []
    for root, _, files in os.walk(target_dir):
        for f in sorted(files):
            if f in {"SHA256SUMS.txt", "BUILD-MANIFEST.json"}:
                continue
            fp = Path(root) / f
            rel = fp.relative_to(target_dir).as_posix()
            digest = sha256_file(fp)
            if corrupt_hash:
                digest = "0" * 64
            lines.append(f"{digest}  {rel}")
    (target_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return target_dir


def create_synthetic_room_resources(
    target_dir: Path,
    *,
    omit_base_mod: bool = False,
    omit_room_payloads: bool = False,
    omit_manifest: bool = False,
    corrupt_manifest_json: bool = False,
    corrupt_member_hash: bool = False,
    mismatch_archive_members: bool = False,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)

    base_file = "e1m1_intro_patch3/maps/game/sp/e1m1_intro/e1m1_intro.entities"
    rep_file = "e1m1_intro_dash.entities"

    base_bytes = b"base_entities_content_data"
    rep_bytes = b"replacement_entities_content_data"

    base_hash = hashlib.sha256(base_bytes).hexdigest()
    rep_hash = hashlib.sha256(rep_bytes).hexdigest()

    if corrupt_member_hash:
        rep_hash = "0" * 64

    manifest_doc = {
        "schema_version": 1,
        "model": "dependent_map_payloads",
        "physical_option_keys": [
            "randomize_chainsaw", "randomize_dash", "randomize_first_battery"
        ],
        "base_members": [base_file],
        "maps": {
            "e1m1_intro": {
                "option_keys": ["randomize_chainsaw"],
                "target_member": base_file,
                "state_policy": "cartesian",
                "states": [
                    {
                        "options": {"randomize_chainsaw": False},
                        "source": "base",
                        "member": None,
                        "sha256": base_hash,
                    },
                    {
                        "options": {"randomize_chainsaw": True},
                        "source": "replacement",
                        "member": rep_file,
                        "sha256": rep_hash,
                    },
                ],
            }
        },
    }

    if not omit_manifest:
        if corrupt_manifest_json:
            (target_dir / "room_payload_manifest.json").write_text("invalid json {{{", encoding="utf-8")
        else:
            (target_dir / "room_payload_manifest.json").write_text(
                json.dumps(manifest_doc, indent=2), encoding="utf-8"
            )

    if not omit_base_mod:
        base_zip = target_dir / "base_mod.zip"
        with zipfile.ZipFile(base_zip, "w") as zf:
            zf.writestr(base_file, base_bytes)
            if mismatch_archive_members:
                zf.writestr("unexpected_extra.file", b"extra")

    if not omit_room_payloads:
        rep_zip = target_dir / "room_payloads.zip"
        with zipfile.ZipFile(rep_zip, "w") as zf:
            zf.writestr(rep_file, rep_bytes)

    return target_dir


class TestAssembleCIArtifact(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handoff_dir = Path(self.temp_dir) / "handoff"
        self.output_dir = Path(self.temp_dir) / "output"
        self.resources_dir = Path(self.temp_dir) / "resources"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_resources_dir(self) -> Path:
        if CANONICAL_LOCAL_RESOURCES.is_dir():
            return CANONICAL_LOCAL_RESOURCES
        create_synthetic_room_resources(self.resources_dir)
        return self.resources_dir

    def test_successful_assembly_and_audit(self):
        create_fake_handoff(self.handoff_dir)
        manifest = validate_handoff_structure(self.handoff_dir)
        self.assertEqual(manifest["version_label"], "v0.4.0-beta.4")

        resources_dir = self._get_resources_dir()
        validate_room_resources(resources_dir, repo_root=REPO_ROOT if resources_dir == CANONICAL_LOCAL_RESOURCES else None)

        stage_dir = Path(self.temp_dir) / "stage"
        lin_stage = assemble_platform_release("linux", self.handoff_dir, REPO_ROOT, resources_dir, manifest, stage_dir)
        win_stage = assemble_platform_release("windows", self.handoff_dir, REPO_ROOT, resources_dir, manifest, stage_dir)

        # Check parity audit passes
        audit_platform_parity(lin_stage, win_stage)

        # Check ZIP creation & audit
        lin_zip = self.output_dir / "DoomEternalArchipelago-v0.4.0-beta.4-linux-x86_64.zip"
        win_zip = self.output_dir / "DoomEternalArchipelago-v0.4.0-beta.4-windows-x86_64.zip"

        write_deterministic_zip(lin_stage, lin_zip)
        audit_final_zip(lin_zip, "linux", repo_root=REPO_ROOT)

        write_deterministic_zip(win_stage, win_zip)
        audit_final_zip(win_zip, "windows", repo_root=REPO_ROOT)

        self.assertTrue(lin_zip.is_file())
        self.assertTrue(win_zip.is_file())

        # Verify ZIP contents and mandatory room resources
        with zipfile.ZipFile(lin_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher", names)
            self.assertNotIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher.exe", names)
            self.assertIn("DoomEternalArchipelago/doometernal.apworld", names)
            self.assertIn("DoomEternalArchipelago/client/ap_client.exe", names)
            self.assertIn(f"DoomEternalArchipelago/{MANIFEST_FILENAME}", names)
            for res in ROOM_COMPILER_RESOURCE_FILES:
                self.assertIn(f"DoomEternalArchipelago/{res}", names)

        with zipfile.ZipFile(win_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher.exe", names)
            self.assertNotIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher", names)
            self.assertIn("DoomEternalArchipelago/doometernal.apworld", names)
            self.assertIn("DoomEternalArchipelago/client/ap_client.exe", names)
            self.assertIn(f"DoomEternalArchipelago/{MANIFEST_FILENAME}", names)
            for res in ROOM_COMPILER_RESOURCE_FILES:
                self.assertIn(f"DoomEternalArchipelago/{res}", names)

    def test_missing_base_mod_rejected(self):
        create_synthetic_room_resources(self.resources_dir, omit_base_mod=True)
        with self.assertRaises(ValueError) as ctx:
            validate_room_resources(self.resources_dir)
        self.assertIn("base_mod.zip", str(ctx.exception))

    def test_missing_room_payloads_rejected(self):
        create_synthetic_room_resources(self.resources_dir, omit_room_payloads=True)
        with self.assertRaises(ValueError) as ctx:
            validate_room_resources(self.resources_dir)
        self.assertIn("room_payloads.zip", str(ctx.exception))

    def test_missing_room_payload_manifest_rejected(self):
        create_synthetic_room_resources(self.resources_dir, omit_manifest=True)
        with self.assertRaises(ValueError) as ctx:
            validate_room_resources(self.resources_dir)
        self.assertIn("room_payload_manifest.json", str(ctx.exception))

    def test_corrupt_manifest_json_rejected(self):
        create_synthetic_room_resources(self.resources_dir, corrupt_manifest_json=True)
        with self.assertRaises(Exception):
            validate_room_resources(self.resources_dir)

    def test_room_archive_manifest_mismatch_rejected(self):
        create_synthetic_room_resources(self.resources_dir, mismatch_archive_members=True)
        with self.assertRaises(ValueError) as ctx:
            validate_room_resources(self.resources_dir)
        self.assertIn("disagree", str(ctx.exception))

    def test_checksum_mismatch_rejected(self):
        create_fake_handoff(self.handoff_dir, corrupt_hash=True)
        with self.assertRaises(ValueError) as ctx:
            validate_handoff_structure(self.handoff_dir)
        self.assertIn("Checksum mismatch", str(ctx.exception))

    def test_missing_linux_launcher_rejected(self):
        create_fake_handoff(self.handoff_dir, omit_linux_launcher=True)
        with self.assertRaises(ValueError) as ctx:
            validate_handoff_structure(self.handoff_dir)
        self.assertIn("Missing Linux launcher", str(ctx.exception))

    def test_missing_windows_launcher_rejected(self):
        create_fake_handoff(self.handoff_dir, omit_win_launcher=True)
        with self.assertRaises(ValueError) as ctx:
            validate_handoff_structure(self.handoff_dir)
        self.assertIn("Missing Windows launcher", str(ctx.exception))

    def test_missing_ap_client_rejected(self):
        create_fake_handoff(self.handoff_dir, omit_ap_client=True)
        with self.assertRaises(ValueError) as ctx:
            validate_handoff_structure(self.handoff_dir)
        self.assertIn("Missing native ap_client.exe", str(ctx.exception))

    def test_invalid_elf_magic_rejected(self):
        create_fake_handoff(self.handoff_dir, bad_elf=True)
        with self.assertRaises(ValueError) as ctx:
            validate_handoff_structure(self.handoff_dir)
        self.assertIn("ELF magic", str(ctx.exception))

    def test_invalid_mz_magic_rejected(self):
        create_fake_handoff(self.handoff_dir, bad_mz=True)
        with self.assertRaises(ValueError) as ctx:
            validate_handoff_structure(self.handoff_dir)
        self.assertIn("MZ magic", str(ctx.exception))

    def test_parity_mismatch_rejected(self):
        create_fake_handoff(self.handoff_dir)
        manifest = validate_handoff_structure(self.handoff_dir)
        resources_dir = self._get_resources_dir()
        stage_dir = Path(self.temp_dir) / "stage"
        lin_stage = assemble_platform_release("linux", self.handoff_dir, REPO_ROOT, resources_dir, manifest, stage_dir)
        win_stage = assemble_platform_release("windows", self.handoff_dir, REPO_ROOT, resources_dir, manifest, stage_dir)

        # Corrupt a shared file in windows stage
        (win_stage / "doometernal.apworld").write_bytes(b"corrupted_apworld_bytes")
        with self.assertRaises(ValueError) as ctx:
            audit_platform_parity(lin_stage, win_stage)
        self.assertIn("Byte mismatch in shared file doometernal.apworld", str(ctx.exception))

    def test_final_zip_audit_rejects_missing_room_resources(self):
        create_fake_handoff(self.handoff_dir)
        manifest = validate_handoff_structure(self.handoff_dir)
        resources_dir = self._get_resources_dir()
        stage_dir = Path(self.temp_dir) / "stage"
        lin_stage = assemble_platform_release("linux", self.handoff_dir, REPO_ROOT, resources_dir, manifest, stage_dir)

        # Remove a room resource before zip creation
        (lin_stage / "client" / "resources" / "room_payloads.zip").unlink()
        lin_zip = self.output_dir / "test_missing_resource.zip"
        write_deterministic_zip(lin_stage, lin_zip)

        with self.assertRaises(ValueError) as ctx:
            audit_final_zip(lin_zip, "linux", repo_root=REPO_ROOT)
        self.assertIn("Mandatory room compiler resource missing", str(ctx.exception))

    def test_room_compiler_reports_missing_resources_cleanly(self):
        empty_dir = Path(self.temp_dir) / "empty_resources"
        empty_dir.mkdir()
        with self.assertRaises(FileNotFoundError) as ctx:
            RoomCompiler(
                empty_dir / "base_mod.zip",
                empty_dir / "room_payloads.zip",
                empty_dir / "room_payload_manifest.json",
            )
        self.assertIn("Release package is incomplete or corrupted", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
