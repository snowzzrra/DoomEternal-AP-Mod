from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from launcher_core import RoomModPackageBuilder, SeedManifest
from tools.validation.audit_item_notification_release import _extract_playable_zip


def _template_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("EternalMod.json", "{}")
    return buffer.getvalue()


def _write_playable(
    path: Path,
    *,
    obsolete_mod: bool = False,
    nested_launcher: bool = False,
) -> None:
    template = _template_bytes()
    with zipfile.ZipFile(path, "w") as archive:
        for name in (
            "README.md",
            "INSTALL.md",
            "RELEASE_MANIFEST.json",
            "doometernal.apworld",
            "DoomEternalArchipelagoLauncher",
            "licenses/Python-LICENSE.txt",
            "client/mod_templates/index.json",
        ):
            archive.writestr(name, "{}")
        archive.writestr("client/mod_templates/dash-on.zip", template)
        archive.writestr("client/mod_templates/dash-off.zip", template)
        if obsolete_mod:
            archive.writestr("DoomEternalArchipelagoBeta.zip", template)
        if nested_launcher:
            archive.writestr("client/DoomEternalArchipelagoLauncher", "binary")


def test_public_layout_extracts_both_internal_templates(tmp_path: Path) -> None:
    playable = tmp_path / "candidate.zip"
    _write_playable(playable)

    roots, client, manifest = _extract_playable_zip(playable, tmp_path / "extracted")

    assert set(roots) == {"dash-on", "dash-off"}
    assert all((root / "EternalMod.json").is_file() for root in roots.values())
    assert client.name == "client"
    assert manifest.name == "RELEASE_MANIFEST.json"


def test_public_layout_rejects_obsolete_universal_mod(tmp_path: Path) -> None:
    playable = tmp_path / "candidate.zip"
    _write_playable(playable, obsolete_mod=True)

    with pytest.raises(AssertionError, match="obsolete universal"):
        _extract_playable_zip(playable, tmp_path / "extracted")


def test_public_layout_rejects_launcher_under_client(tmp_path: Path) -> None:
    playable = tmp_path / "candidate.zip"
    _write_playable(playable, nested_launcher=True)

    with pytest.raises(AssertionError, match="launcher under client"):
        _extract_playable_zip(playable, tmp_path / "extracted")


def test_build_places_launcher_at_release_root() -> None:
    script = Path("scripts/build/playable_test.sh").read_text(encoding="utf-8")
    runtime_audit = Path("scripts/validate/runtime_install.sh").read_text(encoding="utf-8")

    assert '--output-dir "$OUTPUT_DIR"' in script
    assert '--output-dir "$OUTPUT_DIR/client"' not in script
    assert 'cp "$OUTPUT_DIR/client/mod_templates/dash-on.zip"' not in script
    assert "DoomEternalArchipelagoLauncher* licenses" in script
    assert 'MOD_ZIP="$GAME_DIR/Mods/DoomEternalArchipelagoBeta.zip"' not in runtime_audit
    assert "Exact dynamic room ZIP required" in runtime_audit


def test_room_package_builder_binds_seed_manifest_without_fixed_fallback(
    tmp_path: Path,
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    template = templates / "dash-off.zip"
    template.write_bytes(_template_bytes())
    template_sha = hashlib.sha256(template.read_bytes()).hexdigest()
    (templates / "index.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "variants": {
                    "dash_off": {"file": template.name, "sha256": template_sha},
                    "dash_on": {"file": template.name, "sha256": template_sha},
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = SeedManifest.create(
        seed_name="room-package-test",
        team=1,
        slot=2,
        options={"randomize_dash": False},
        active_location_ids=[],
    )

    built = RoomModPackageBuilder(templates).build(manifest, tmp_path / "output")

    assert built.name == f"DoomEternalArchipelago-{manifest.manifest_hash[:16]}.zip"
    with zipfile.ZipFile(built) as package:
        names = set(package.namelist())
        packaged_manifest = json.loads(package.read("seed_manifest.json"))
        receipt = json.loads(package.read("seed_receipt.json"))
    assert "DoomEternalArchipelagoBeta.zip" not in names
    assert packaged_manifest["manifest_hash"] == manifest.manifest_hash
    assert receipt["manifest_hash"] == manifest.manifest_hash
    assert receipt["template_sha256"] == template_sha
