import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.release.assemble_ci_artifact import (
    assemble_platform_release,
    audit_final_zip,
    audit_platform_parity,
    sha256_file,
    validate_handoff_structure,
    write_deterministic_zip,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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


class TestAssembleCIArtifact(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handoff_dir = Path(self.temp_dir) / "handoff"
        self.output_dir = Path(self.temp_dir) / "output"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_successful_assembly_and_audit(self):
        create_fake_handoff(self.handoff_dir)
        manifest = validate_handoff_structure(self.handoff_dir)
        self.assertEqual(manifest["version_label"], "v0.4.0-beta.4")

        stage_dir = Path(self.temp_dir) / "stage"
        lin_stage = assemble_platform_release("linux", self.handoff_dir, REPO_ROOT, manifest, stage_dir)
        win_stage = assemble_platform_release("windows", self.handoff_dir, REPO_ROOT, manifest, stage_dir)

        # Check parity audit passes
        audit_platform_parity(lin_stage, win_stage)

        # Check ZIP creation & audit
        lin_zip = self.output_dir / "DoomEternalArchipelago-v0.4.0-beta.4-linux-x86_64.zip"
        win_zip = self.output_dir / "DoomEternalArchipelago-v0.4.0-beta.4-windows-x86_64.zip"

        write_deterministic_zip(lin_stage, lin_zip)
        audit_final_zip(lin_zip, "linux")

        write_deterministic_zip(win_stage, win_zip)
        audit_final_zip(win_zip, "windows")

        self.assertTrue(lin_zip.is_file())
        self.assertTrue(win_zip.is_file())

        # Verify ZIP contents
        with zipfile.ZipFile(lin_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher", names)
            self.assertNotIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher.exe", names)
            self.assertIn("DoomEternalArchipelago/doometernal.apworld", names)
            self.assertIn("DoomEternalArchipelago/client/ap_client.exe", names)
            self.assertIn("DoomEternalArchipelago/RELEASE-MANIFEST.json", names)

        with zipfile.ZipFile(win_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher.exe", names)
            self.assertNotIn("DoomEternalArchipelago/DoomEternalArchipelagoLauncher", names)
            self.assertIn("DoomEternalArchipelago/doometernal.apworld", names)
            self.assertIn("DoomEternalArchipelago/client/ap_client.exe", names)
            self.assertIn("DoomEternalArchipelago/RELEASE-MANIFEST.json", names)

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
        stage_dir = Path(self.temp_dir) / "stage"
        lin_stage = assemble_platform_release("linux", self.handoff_dir, REPO_ROOT, manifest, stage_dir)
        win_stage = assemble_platform_release("windows", self.handoff_dir, REPO_ROOT, manifest, stage_dir)

        # Corrupt a shared file in windows stage
        (win_stage / "doometernal.apworld").write_bytes(b"corrupted_apworld_bytes")
        with self.assertRaises(ValueError) as ctx:
            audit_platform_parity(lin_stage, win_stage)
        self.assertIn("Byte mismatch in shared file doometernal.apworld", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
