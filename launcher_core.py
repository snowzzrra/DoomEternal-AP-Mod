"""Headless seed compiler and install workflow. UI/CLI are adapters only."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parent
DASH_LOCATION_ID = 7770083
DASH_ENTITY = "AP_CHECK_CAPITOL_PROGRESS_DASH_1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_identity() -> dict[str, Any]:
    return json.loads((ROOT / "data" / "content_identity.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class RoomSnapshot:
    seed_name: str
    team: int
    slot: int
    slot_data: dict[str, Any]
    missing_locations: tuple[int, ...]
    checked_locations: tuple[int, ...]

    @classmethod
    def from_packets(cls, room_info: dict[str, Any], connected: dict[str, Any]) -> RoomSnapshot:
        seed_name = room_info.get("seed_name")
        team = connected.get("team")
        slot = connected.get("slot")
        slot_data = connected.get("slot_data")
        if not isinstance(seed_name, str) or not seed_name:
            raise ValueError("RoomInfo.seed_name is required")
        if not isinstance(team, int) or isinstance(team, bool) or team < 0:
            raise ValueError("Connected.team must be a non-negative integer")
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
            raise ValueError("Connected.slot must be a positive integer")
        if not isinstance(slot_data, dict):
            raise ValueError("Connected.slot_data must be an object")
        if not isinstance(slot_data.get("randomize_dash"), bool):
            raise ValueError("Connected.slot_data.randomize_dash must be boolean")

        def locations(field: str) -> tuple[int, ...]:
            values = connected.get(field)
            if not isinstance(values, (list, tuple, set)):
                raise ValueError(f"Connected.{field} must be a location list")
            if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
                raise ValueError(f"Connected.{field} contains invalid location ID")
            return tuple(sorted(set(values)))

        return cls(
            seed_name=seed_name,
            team=team,
            slot=slot,
            slot_data=dict(slot_data),
            missing_locations=locations("missing_locations"),
            checked_locations=locations("checked_locations"),
        )

    @classmethod
    def from_event(cls, payload: dict[str, Any]) -> RoomSnapshot:
        """Parse launcher event payload; intended for bridge/supervisor IPC."""
        return cls.from_packets(
            {"seed_name": payload.get("seed_name")},
            {
                "team": payload.get("team"),
                "slot": payload.get("slot"),
                "slot_data": payload.get("slot_data"),
                "missing_locations": payload.get("missing_locations"),
                "checked_locations": payload.get("checked_locations"),
            },
        )

    @property
    def active_location_ids(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.missing_locations) | set(self.checked_locations)))


@dataclass(frozen=True)
class SeedManifest:
    schema: int
    game: str
    seed_name: str
    team: int
    slot: int
    bridge_protocol: int
    apworld_revision: str
    content_revision: str
    compiler_revision: int
    options: dict[str, bool]
    active_location_ids: tuple[int, ...]
    manifest_hash: str

    @classmethod
    def create(cls, *, seed_name: str, team: int, slot: int, options: dict[str, Any], active_location_ids: list[int]) -> SeedManifest:
        identity = release_identity()
        normalized_options = {key: bool(value) for key, value in sorted(options.items())}
        payload = {
            "schema": 1,
            "game": identity["game"],
            "seed_name": seed_name,
            "team": int(team),
            "slot": int(slot),
            "bridge_protocol": identity["bridge_protocol_version"],
            "apworld_revision": identity["apworld_revision"],
            "content_revision": identity["content_revision"],
            "compiler_revision": identity["compiler_revision"],
            "options": normalized_options,
            "active_location_ids": sorted(active_location_ids),
        }
        return cls(**payload, manifest_hash=hashlib.sha256(_canonical(payload)).hexdigest())

    def document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_room(cls, snapshot: RoomSnapshot, known_location_ids: set[int]) -> SeedManifest:
        identity = release_identity()
        slot_data = snapshot.slot_data
        compatibility = {
            "bridge_protocol": ("bridge_protocol", identity["bridge_protocol_version"]),
            "content_revision": ("content_revision", identity["content_revision"]),
            "apworld_revision": ("apworld_revision", identity["apworld_revision"]),
            "compiler_revision": ("compiler_revision", identity["compiler_revision"]),
        }
        for label, (field, expected) in compatibility.items():
            if field in slot_data and slot_data[field] != expected:
                raise ValueError(f"session {label} is incompatible: {slot_data[field]!r} != {expected!r}")
        unknown = sorted(set(snapshot.active_location_ids) - known_location_ids)
        if unknown:
            raise ValueError(f"session contains unknown DOOM Eternal location IDs: {unknown}")
        return cls.create(
            seed_name=snapshot.seed_name,
            team=snapshot.team,
            slot=snapshot.slot,
            options={"randomize_dash": slot_data["randomize_dash"]},
            active_location_ids=list(snapshot.active_location_ids),
        )


class RoomModPackageBuilder:
    """Select a verified physical template, then bind it to one room manifest."""

    INDEX_NAME = "index.json"

    def __init__(self, templates_root: Path):
        self.templates_root = templates_root

    def _template(self, dash_enabled: bool) -> tuple[Path, dict[str, Any]]:
        index_path = self.templates_root / self.INDEX_NAME
        document = json.loads(index_path.read_text(encoding="utf-8"))
        if document.get("schema") != 1 or not isinstance(document.get("variants"), dict):
            raise ValueError("invalid physical template index")
        key = "dash_on" if dash_enabled else "dash_off"
        entry = document["variants"].get(key)
        if not isinstance(entry, dict):
            raise ValueError(f"missing physical template variant: {key}")
        filename = entry.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError(f"unsafe physical template filename: {filename!r}")
        template = self.templates_root / filename
        if not template.is_file() or not zipfile.is_zipfile(template):
            raise ValueError(f"physical template is not a ZIP: {template}")
        expected = entry.get("sha256")
        actual = _sha256(template)
        if expected != actual:
            raise ValueError(f"physical template SHA-256 mismatch: expected {expected}, got {actual}")
        return template, entry

    def build(self, manifest: SeedManifest, output_root: Path) -> Path:
        dash_enabled = manifest.options.get("randomize_dash", False)
        template, template_entry = self._template(dash_enabled)
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / f"DoomEternalArchipelago-{manifest.manifest_hash[:16]}.zip"
        temporary = destination.with_name(f".{destination.name}.incoming")
        seed_document = manifest.document()
        receipt = {
            "schema": 1,
            "manifest_hash": manifest.manifest_hash,
            "randomize_dash": dash_enabled,
            "template": template.name,
            "template_sha256": template_entry["sha256"],
            "physical_e1m2_sha256": template_entry.get("physical_e1m2_sha256"),
        }
        with zipfile.ZipFile(template) as source, zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as output:
            seen: set[str] = set()
            for info in source.infolist():
                path = Path(info.filename)
                if path.is_absolute() or ".." in path.parts or info.filename in seen:
                    raise ValueError(f"unsafe or duplicate template member: {info.filename}")
                seen.add(info.filename)
                if info.filename in {"seed_manifest.json", "seed_receipt.json"}:
                    continue
                output.writestr(info, source.read(info))
            output.writestr("seed_manifest.json", json.dumps(seed_document, indent=2, sort_keys=True) + "\n")
            output.writestr("seed_receipt.json", json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, destination)
        return destination


@dataclass
class InstallRecord:
    state: str
    game_path: str
    game_fingerprint: str
    endpoint: str
    manifest_hash: str
    installed_files: dict[str, str]
    rollback_path: str | None
    apworld_revision: str = ""
    content_revision: str = ""
    compiler_revision: int = 0
    error: str | None = None


class EternalModInjectorAdapter(Protocol):
    """Runtime adapter boundary; concrete Injector process support is pending."""

    def activate(self, install_root: Path, record: InstallRecord) -> None: ...


class InstallPlan:
    """Install launcher-owned files only, with reverse-order rollback."""

    def __init__(self, target: Path, source: Path):
        self.target = target
        self.source = source

    RECORD_NAME = ".doom_ap_install_record.json"

    def _record_path(self) -> Path:
        return self.target / self.RECORD_NAME

    def _read_record(self) -> dict[str, Any] | None:
        path = self._record_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _write_record(self, record: InstallRecord) -> None:
        self.target.mkdir(parents=True, exist_ok=True)
        temporary = self._record_path().with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self._record_path())

    def install(self, record: InstallRecord, *, fail_after: int | None = None) -> InstallRecord:
        files = sorted(path for path in self.source.rglob("*") if path.is_file())
        previous = self._read_record()
        previous_owned = set((previous or {}).get("installed_files", {}))
        expected_hashes = {str(path.relative_to(self.source)): _sha256(path) for path in files}
        if (
            previous
            and previous.get("state") == "active"
            and previous.get("manifest_hash") == record.manifest_hash
            and previous.get("installed_files") == expected_hashes
            and all((self.target / relative).is_file() and _sha256(self.target / relative) == digest for relative, digest in expected_hashes.items())
        ):
            return InstallRecord(**previous)

        collisions = [
            relative for relative in expected_hashes
            if (self.target / relative).exists() and relative not in previous_owned
        ]
        if collisions:
            raise ValueError(f"refusing to replace files not owned by launcher: {collisions}")

        rollback = self.target.parent / f".{self.target.name}.rollback-{record.manifest_hash[:12]}"
        shutil.rmtree(rollback, ignore_errors=True)
        rollback.mkdir(parents=True)
        record.state = "installing"
        record.rollback_path = str(rollback)
        self._write_record(record)
        applied: list[str] = []
        try:
            for index, source_path in enumerate(files, start=1):
                relative = str(source_path.relative_to(self.source))
                destination = self.target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    backup = rollback / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup)
                incoming = destination.with_name(f".{destination.name}.incoming")
                shutil.copy2(source_path, incoming)
                os.replace(incoming, destination)
                applied.append(relative)
                if fail_after is not None and index >= fail_after:
                    raise RuntimeError("injected install failure")
        except Exception as error:
            for relative in reversed(applied):
                destination = self.target / relative
                backup = rollback / relative
                if backup.exists():
                    os.replace(backup, destination)
                else:
                    destination.unlink(missing_ok=True)
            record.state = "failed"
            record.error = f"{type(error).__name__}: {error}"
            record.installed_files = dict((previous or {}).get("installed_files", {}))
            self._write_record(record)
            raise
        record.state = "active"
        record.error = None
        record.installed_files = expected_hashes
        self._write_record(record)
        return record

    def rollback(self, record: InstallRecord, error: Exception) -> InstallRecord:
        rollback = Path(record.rollback_path) if record.rollback_path else None
        for relative in reversed(tuple(record.installed_files)):
            destination = self.target / relative
            backup = rollback / relative if rollback else None
            if backup is not None and backup.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
        record.state = "failed"
        record.error = f"{type(error).__name__}: {error}"
        record.installed_files = {}
        self._write_record(record)
        return record


class ModCompiler:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def active_location_ids(self, randomize_dash: bool) -> list[int]:
        names = json.loads((self.root / "data" / "location_names.json").read_text(encoding="utf-8"))["locations"]
        ids = sorted(int(location_id) for location_id in names)
        return ids if randomize_dash else [location_id for location_id in ids if location_id != DASH_LOCATION_ID]

    def known_location_ids(self) -> set[int]:
        return set(self.active_location_ids(True))

    def compile(self, manifest: SeedManifest, output_root: Path) -> Path:
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "seed_manifest.json").write_text(
            json.dumps(manifest.document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        dash_enabled = manifest.options.get("randomize_dash", False)
        e1m2 = json.loads((self.root / "content/maps/e1m2_war/locations.json").read_text(encoding="utf-8"))
        active_ids = set(manifest.active_location_ids)
        e1m2["entities"] = {
            check: location_id for check, location_id in e1m2["entities"].items()
            if location_id in active_ids
        }
        e1m2["names"] = {
            location_id: name for location_id, name in e1m2["names"].items()
            if int(location_id) in active_ids
        }
        e1m2["secret_encounters"] = [
            encounter for encounter in e1m2.get("secret_encounters", [])
            if encounter["location_id"] in active_ids
        ]
        active_checks = set(e1m2["entities"]) | {
            encounter["ap_check"] for encounter in e1m2["secret_encounters"]
        }
        e1m2["location_feedback"] = {
            check: policy for check, policy in e1m2.get("location_feedback", {}).items()
            if check in active_checks
        }
        active_entity_names = {
            check.removeprefix("AP_CHECK_").lower() for check in e1m2["entities"]
        }
        e1m2["target_policies"] = {
            name: policy for name, policy in e1m2.get("target_policies", {}).items()
            if name in active_entity_names
        }
        # Minimal per-seed overlays must not mutate unrelated vanilla owners.
        if not (active_ids - {DASH_LOCATION_ID}):
            for key in (
                "inline_currency_removals", "neutralize_pickups", "target_removals",
                "remove_entities", "neutralize_entity_references",
            ):
                e1m2[key] = [] if key != "target_removals" else {}
        if not dash_enabled:
            e1m2["entities"].pop(DASH_ENTITY, None)
            e1m2["names"].pop(str(DASH_LOCATION_ID), None)
        (output_root / "e1m2_war.locations.json").write_text(
            json.dumps(e1m2, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return output_root

    def compile_map(self, manifest: SeedManifest, vanilla_entities: Path, output_entities: Path) -> Path:
        """Compile physical Dash mutation. Caller supplies legal local vanilla dump."""
        from tools.maps.ap_map_generator import generate_map

        with tempfile.TemporaryDirectory() as temporary:
            staged = self.compile(manifest, Path(temporary))
            generate_map(
                vanilla_entities,
                output_entities,
                staged / "e1m2_war.locations.json",
                output_entities.with_suffix(".manifest.json"),
                json.loads((self.root / "data/items.json").read_text(encoding="utf-8")),
            )
        output_entities.with_suffix(".seed.json").write_text(
            json.dumps(manifest.document(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return output_entities


class LaunchWorkflow:
    def __init__(self, compiler: ModCompiler | None = None, *, vanilla_exultia: Path | None = None, injector: EternalModInjectorAdapter | None = None):
        self.compiler = compiler or ModCompiler()
        self.vanilla_exultia = vanilla_exultia
        self.injector = injector

    def manifest_for(self, snapshot: RoomSnapshot) -> SeedManifest:
        return SeedManifest.from_room(snapshot, self.compiler.known_location_ids())

    def execute(self, snapshot: RoomSnapshot, install_root: Path, endpoint: str = "") -> InstallRecord:
        manifest = self.manifest_for(snapshot)
        existing_path = install_root / InstallPlan.RECORD_NAME
        if existing_path.is_file():
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if existing.get("state") == "active" and existing.get("manifest_hash") != manifest.manifest_hash:
                # Different identity is valid, but always produces explicit plan/record.
                pass
        identity = release_identity()
        with tempfile.TemporaryDirectory(prefix="doom-ap-compile-") as temporary:
            compiled = self.compiler.compile(manifest, Path(temporary))
            if self.vanilla_exultia is not None:
                self.compiler.compile_map(
                    manifest,
                    self.vanilla_exultia,
                    compiled / "e1m2_war.entities",
                )
            manifest_path = compiled / "seed_manifest.json"
            if not manifest_path.is_file() or json.loads(manifest_path.read_text())["manifest_hash"] != manifest.manifest_hash:
                raise ValueError("compiler output manifest validation failed")
            record = InstallRecord(
                state="installing",
                game_path=str(install_root),
                game_fingerprint="directory-backend",
                endpoint=endpoint,
                manifest_hash=manifest.manifest_hash,
                installed_files={},
                rollback_path=None,
                apworld_revision=identity["apworld_revision"],
                content_revision=identity["content_revision"],
                compiler_revision=identity["compiler_revision"],
            )
            plan = InstallPlan(install_root, compiled)
            installed = plan.install(record)
            if self.injector is not None:
                try:
                    self.injector.activate(install_root, installed)
                except Exception as error:
                    plan.rollback(installed, error)
                    raise
            return installed

    @staticmethod
    def write_client_config(client_dir: Path, *, endpoint: str, manifest_hash: str) -> Path:
        client_dir.mkdir(parents=True, exist_ok=True)
        path = client_dir / "ap_config.json"
        config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        config.update({"server_address": endpoint, "seed_manifest_hash": manifest_hash})
        temporary = client_dir / ".ap_config.json.tmp"
        temporary.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return path

    def join(self, room: dict[str, Any], client_dir: Path, endpoint: str) -> SeedManifest:
        """Offline simulation adapter. Production Join consumes RoomSnapshot."""
        options = room.get("options", {})
        dash = bool(options.get("randomize_dash", False))
        active = self.compiler.active_location_ids(dash)
        snapshot = RoomSnapshot.from_packets(
            {"seed_name": room["seed_name"]},
            {
                "team": room["team"], "slot": room["slot"],
                "slot_data": {"randomize_dash": dash},
                "missing_locations": active, "checked_locations": [],
            },
        )
        manifest = self.manifest_for(snapshot)
        self.execute(snapshot, client_dir / "compiled_mod", endpoint)
        self.write_client_config(client_dir, endpoint=endpoint, manifest_hash=manifest.manifest_hash)
        return manifest


def validate_game(game_root: Path, meathook_path: Path, client_dir: Path, saves_dir: Path) -> None:
    required = [game_root / "DOOMEternalx64vk.exe", game_root / "base" / "classicwads", meathook_path, client_dir / "bridge_client.py", saves_dir]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError("missing required game/install paths: " + ", ".join(missing))
