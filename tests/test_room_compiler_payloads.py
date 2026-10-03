"""Cold real-resource compilation without authoring maps; takes a retained room manifest."""
import argparse
import hashlib
import json
import re
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from doom_eap.launcher import launcher_core
from tools.release.room_payloads import assemble_room_files


def check(resources, room, decompressor, output, frozen_launcher=None):
    frozen = json.loads(room.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="room-compiler-test-") as temporary:
        core = launcher_core
        bundle = Path(temporary)
        if frozen_launcher:
            from PyInstaller.archive.readers import CArchiveReader
            archive = CArchiveReader(str(frozen_launcher))
            for name in archive.toc:
                relative = Path(name.replace("\\", "/"))
                if relative.parts[0] in {"data", "content", "manifests"}:
                    target = bundle / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extract(name))
            core = types.ModuleType("doom_eap.launcher.frozen_test_core")
            core.__package__ = "doom_eap.launcher"
            core.__file__ = str(bundle / "doom_eap/launcher/launcher_core.py")
            sys.modules[core.__name__] = core
            pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith(".pyz")))
            import importlib.util
            with frozen_launcher.open("rb") as stream:
                stream.seek(pyz._start_offset + 4)
                assert stream.read(4) == importlib.util.MAGIC_NUMBER, "Use matching frozen Python"
            exec(pyz.extract("doom_eap.launcher.launcher_core"), core.__dict__)
        original_read_bytes, original_read_text = Path.read_bytes, Path.read_text

        def read_bytes(path):
            assert "vanillamaps" not in path.parts, f"Authoring map dependency: {path}"
            return original_read_bytes(path)

        def read_text(path, *args, **kwargs):
            assert "vanillamaps" not in path.parts, f"Authoring map dependency: {path}"
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_bytes", read_bytes), patch.object(Path, "read_text", read_text), \
             patch.object(sys, "frozen", bool(frozen_launcher), create=True):
            compiler = core.RoomCompiler(resources / "base_mod.zip", resources / "room_payloads.zip",
                                         resources / "room_payload_manifest.json", decompressor=decompressor)
            manifest = core.SeedManifest.create(seed_name=frozen["seed_name"], team=frozen["team"], slot=frozen["slot"],
                options=frozen["options"], active_location_ids=frozen["active_location_ids"], placements=frozen["placements"],
                static_content_digest=compiler.static_content_digest)
            fields = ("seed_name", "team", "slot", "options", "active_location_ids", "placements")
            document = json.loads(json.dumps(manifest.document()))
            assert all(document[key] == frozen[key] for key in fields)
            before, _ = assemble_room_files(compiler.base_resource, compiler.payload_resource, compiler.payload_manifest, manifest.options)
            package = compiler.build(manifest, output, force=True)
            active = set(manifest.active_location_ids)
            checked = {}
            with zipfile.ZipFile(package) as result:
                for identity, member in compiler.payload_manifest["context_targets"].items():
                    if member not in before or member not in result.namelist():
                        continue
                    source = compiler._decompress_entities_text(before[member], member)
                    compiled = compiler._decompress_entities_text(result.read(member), member)
                    names = set(re.findall(r"\bentityDef\s+(ap_publisher_\S+|ap_notify_location_\d+)\s*\{", source))
                    names = {name for name in names if not name.startswith("ap_notify_location_") or int(name.rsplit("_", 1)[1]) in active}
                    assert "idWorldspawn" in compiled
                    missing = {name: len(re.findall(rf"\bentityDef\s+{re.escape(name)}\s*\{{", compiled)) for name in names}
                    missing = {name: count for name, count in missing.items() if count != 1}
                    assert not missing, (identity, missing)
                    checked[identity] = len(names)
            assert compiler.validate_cached_package(package, manifest)
            report = {"result": "PASS", "method": "actual frozen launcher_core PYZ" if frozen_launcher else "source compiler",
                      "candidate": str(package), "manifest_hash": manifest.manifest_hash,
                      "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                      "static_content_digest": compiler.static_content_digest,
                      "frozen_fields_preserved": list(fields), "dlc_ap_entities_preserved": checked,
                      "authoring_map_reads": 0, "gameplay": "not_exercised"}
            (output / "COLD-COMPILE-PROOF.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("resources", "room", "decompressor", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--frozen-launcher", type=Path)
    args = parser.parse_args()
    check(args.resources, args.room, args.decompressor, args.output, args.frozen_launcher)
