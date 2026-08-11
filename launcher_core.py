"""Headless seed compiler and install workflow. UI/CLI are adapters only."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from physical_options import (
    PHYSICAL_OPTION_KEYS,
    physical_location_ids,
    physical_signature,
    project_map_config,
)
from tools.decls.devinv_builder import build_devinv_loadout, output_path_for_map

ROOT = Path(__file__).resolve().parent
DASH_LOCATION_ID = 7770083
DASH_ENTITY = "AP_CHECK_CAPITOL_PROGRESS_DASH_1"
MANIFEST_SCHEMA_VERSION = 2
MOD_CONTRACT_REVISION = 1
SUPPORTED_CAPABILITIES = frozenset({
    "room_mod_v1",
    "randomize_dash_v1",
    "starting_inventory_v1",
    "starting_weapon_v1",
    "physical_options_v1",
})
CLIENT_CONFIG_FIELDS = frozenset({
    "steam_remote_dir",
    "steam_id3",
    "doom_base_dir",
    "save_games_dir",
    "server_address",
    "seed_manifest_hash",
})


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
        for key in PHYSICAL_OPTION_KEYS:
            if not isinstance(slot_data.get(key), bool):
                raise ValueError(f"Connected.slot_data.{key} must be boolean")

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
    manifest_schema_version: int
    mod_contract_revision: int
    required_capabilities: tuple[str, ...]
    options: dict[str, Any]
    active_location_ids: tuple[int, ...]
    manifest_hash: str

    @classmethod
    def create(cls, *, seed_name: str, team: int, slot: int, options: dict[str, Any], active_location_ids: list[int]) -> SeedManifest:
        identity = release_identity()
        normalized_options = {
            key: options[key]
            for key in sorted(options)
        }
        for key in PHYSICAL_OPTION_KEYS:
            normalized_options.setdefault(key, False)
            if not isinstance(normalized_options[key], bool):
                raise ValueError(f"manifest option {key} must be boolean")
        if "starting_inventory" in normalized_options:
            inventory = normalized_options["starting_inventory"]
            if not isinstance(inventory, dict):
                raise ValueError("manifest starting_inventory must be an object")
            for name, quantity in inventory.items():
                if not isinstance(name, str) or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
                    raise ValueError("manifest starting_inventory has invalid name or quantity")
        if "starting_weapon" in normalized_options and normalized_options["starting_weapon"] is not None and not isinstance(normalized_options["starting_weapon"], str):
            raise ValueError("manifest starting_weapon must be string or null")
        required_capabilities = {"room_mod_v1"}
        required_capabilities.add("physical_options_v1")
        if normalized_options.get("randomize_dash"):
            required_capabilities.add("randomize_dash_v1")
        if normalized_options.get("starting_inventory"):
            required_capabilities.add("starting_inventory_v1")
        if normalized_options.get("starting_weapon"):
            required_capabilities.add("starting_weapon_v1")
        payload = {
            "schema": MANIFEST_SCHEMA_VERSION,
            "game": identity["game"],
            "seed_name": seed_name,
            "team": int(team),
            "slot": int(slot),
            "bridge_protocol": identity["bridge_protocol_version"],
            "apworld_revision": identity["apworld_revision"],
            "content_revision": identity["content_revision"],
            "compiler_revision": identity["compiler_revision"],
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "mod_contract_revision": MOD_CONTRACT_REVISION,
            "required_capabilities": sorted(required_capabilities),
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
        if "bridge_protocol" in slot_data and slot_data["bridge_protocol"] != identity["bridge_protocol_version"]:
            raise ValueError(f"session bridge_protocol is incompatible: {slot_data['bridge_protocol']!r} != {identity['bridge_protocol_version']!r}")
        schema = slot_data.get("manifest_schema_version", 1)
        contract = slot_data.get("mod_contract_revision", 1)
        if not isinstance(schema, int) or schema < 1 or schema > MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"session manifest schema is unsupported: {schema!r}")
        if not isinstance(contract, int) or contract < 1 or contract > MOD_CONTRACT_REVISION:
            raise ValueError(f"session mod contract is unsupported: {contract!r}")
        required = slot_data.get("required_capabilities")
        if required is None:
            required_capabilities = {"room_mod_v1"}
            if slot_data.get("randomize_dash"):
                required_capabilities.add("randomize_dash_v1")
        elif isinstance(required, list) and all(isinstance(value, str) for value in required):
            required_capabilities = set(required)
        else:
            raise ValueError("session required_capabilities must be a list of strings")
        unsupported = sorted(required_capabilities - SUPPORTED_CAPABILITIES)
        if unsupported:
            raise ValueError(f"session requires unsupported capabilities: {unsupported}")
        if "compiler_revision" in slot_data and slot_data["compiler_revision"] != identity["compiler_revision"]:
            # Implementation metadata only. Compatibility is decided above.
            import logging
            logging.getLogger(__name__).info("session compiler_revision differs: room=%r local=%r", slot_data["compiler_revision"], identity["compiler_revision"])
        unknown = sorted(set(snapshot.active_location_ids) - known_location_ids)
        if unknown:
            raise ValueError(f"session contains unknown DOOM Eternal location IDs: {unknown}")
        return cls.create(
            seed_name=snapshot.seed_name,
            team=snapshot.team,
            slot=snapshot.slot,
            options={
                "randomize_dash": slot_data["randomize_dash"],
                **{key: slot_data[key] for key in PHYSICAL_OPTION_KEYS},
                **({"starting_inventory": slot_data["starting_inventory"]} if "starting_inventory" in slot_data else {}),
                **({"starting_weapon": slot_data["starting_weapon"]} if "starting_weapon" in slot_data else {}),
            },
            active_location_ids=list(snapshot.active_location_ids),
        )


class RoomModPackageBuilder:
    """Select a verified physical template, then bind it to one room manifest."""

    INDEX_NAME = "index.json"
    DEVINV_MAP_KEY = "e1m1_intro"

    def __init__(self, templates_root: Path):
        self.templates_root = templates_root

    def _template(self, options: dict[str, Any]) -> tuple[bytes, str, dict[str, Any]]:
        if self.templates_root.is_file():
            with zipfile.ZipFile(self.templates_root) as archive:
                document = json.loads(archive.read(self.INDEX_NAME))
                template_reader = archive.read
                source_name = self.templates_root.name
                return self._select_template(document, template_reader, source_name, options)
        document = json.loads((self.templates_root / self.INDEX_NAME).read_text(encoding="utf-8"))
        return self._select_template(
            document,
            lambda name: (self.templates_root / name).read_bytes(),
            str(self.templates_root), options,
        )

    @staticmethod
    def _select_template(document, read_member, source_name, options):
        if document.get("schema") != 2 or not isinstance(document.get("variants"), dict):
            raise ValueError("invalid physical template index")
        if document.get("physical_options") != list(PHYSICAL_OPTION_KEYS):
            raise ValueError("physical template option contract mismatch")
        key = physical_signature(options)
        entry = document["variants"].get(key)
        if not isinstance(entry, dict):
            raise ValueError(f"missing physical template variant: {key}")
        filename = entry.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError(f"unsafe physical template filename: {filename!r}")
        try:
            payload = read_member(filename)
        except (KeyError, OSError) as error:
            raise ValueError(f"physical template is missing: {filename} in {source_name}") from error
        if not zipfile.is_zipfile(io.BytesIO(payload)):
            raise ValueError(f"physical template is not a ZIP: {filename}")
        expected = entry.get("sha256")
        actual = hashlib.sha256(payload).hexdigest()
        if expected != actual:
            raise ValueError(f"physical template SHA-256 mismatch: expected {expected}, got {actual}")
        return payload, filename, entry

    def build(self, manifest: SeedManifest, output_root: Path) -> Path:
        physical_options = {key: manifest.options[key] for key in PHYSICAL_OPTION_KEYS}
        template_payload, template_name, template_entry = self._template(manifest.options)
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / f"DoomEternalArchipelago-{manifest.manifest_hash[:16]}.zip"
        temporary = destination.with_name(f".{destination.name}.incoming")
        seed_document = manifest.document()
        receipt = {
            "schema": 1,
            "manifest_hash": manifest.manifest_hash,
            "physical_options": physical_options,
            "physical_signature": physical_signature(physical_options),
            "template": template_name,
            "template_sha256": template_entry["sha256"],
            "starting_inventory": manifest.options.get("starting_inventory", {}),
            "starting_weapon": manifest.options.get("starting_weapon"),
        }
        devinv_path = output_path_for_map(
            Path("."), ROOT / "data" / "map_sources.json", self.DEVINV_MAP_KEY
        ).as_posix()
        devinv_source = build_devinv_loadout(
            manifest.options.get("starting_inventory", {}),
            manifest.options.get("starting_weapon"),
        )
        with zipfile.ZipFile(io.BytesIO(template_payload)) as source, zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as output:
            seen: set[str] = set()
            for info in source.infolist():
                path = Path(info.filename)
                if path.is_absolute() or ".." in path.parts or info.filename in seen:
                    raise ValueError(f"unsafe or duplicate template member: {info.filename}")
                seen.add(info.filename)
                if info.filename in {"seed_manifest.json", "seed_receipt.json", devinv_path}:
                    continue
                output.writestr(info, source.read(info))
            output.writestr(devinv_path, devinv_source)
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

    def active_location_ids(self, options: dict[str, Any] | bool) -> list[int]:
        names = json.loads((self.root / "data" / "location_names.json").read_text(encoding="utf-8"))["locations"]
        ids = sorted(int(location_id) for location_id in names)
        if isinstance(options, bool):
            options = {
                "randomize_chainsaw": False,
                "randomize_dash": options,
                "randomize_first_battery": False,
            }
        return [location_id for location_id in ids if location_id not in (
            set(site_id for site_id in (7770001, DASH_LOCATION_ID, 7770084))
            - physical_location_ids(options)
        )]

    def known_location_ids(self) -> set[int]:
        return set(self.active_location_ids({key: True for key in PHYSICAL_OPTION_KEYS}))

    def compile(self, manifest: SeedManifest, output_root: Path) -> Path:
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "seed_manifest.json").write_text(
            json.dumps(manifest.document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        for map_key in ("e1m1_intro", "e1m2_war"):
            package = self.root / f"content/maps/{map_key}"
            config = json.loads((package / "locations.json").read_text(encoding="utf-8"))
            descriptor = json.loads((package / "descriptor.json").read_text(encoding="utf-8"))
            config.setdefault("map_key", descriptor["key"])
            config.setdefault("runtime_map", descriptor["runtime_map"])
            assets_path = package / "assets.json"
            if assets_path.is_file():
                assets = json.loads(assets_path.read_text(encoding="utf-8"))
                config = {
                    **config,
                    "assets": assets.get("assets", []),
                    "default_visual_asset": assets.get("default_visual_asset"),
                }
            projected = project_map_config(config, manifest.options)
            (output_root / f"{map_key}.locations.json").write_text(
                json.dumps(projected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return output_root

    def compile_map(self, manifest: SeedManifest, vanilla_entities: Path, output_entities: Path,
                    map_key: str = "e1m2_war") -> Path:
        """Compile one physical-option map. Caller supplies legal local vanilla dump."""
        from item_classification import load_item_classifications
        from item_reconciliation import load_policy_registry
        from tools.maps.ap_map_generator import generate_map

        item_definitions = json.loads(
            (self.root / "data/items.json").read_text(encoding="utf-8")
        )
        policies = load_policy_registry(
            self.root / "data/item_replay_policies.json",
            {int(item_id): definition for item_id, definition in item_definitions.items()},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staged = self.compile(manifest, Path(temporary))
            generate_map(
                vanilla_entities,
                output_entities,
                staged / f"{map_key}.locations.json",
                output_entities.with_suffix(".manifest.json"),
                item_definitions,
                item_names={item_id: policy.name for item_id, policy in policies.items()},
                item_classifications=load_item_classifications(
                    self.root / "data/item_classifications.json"
                ),
                receipt_feedback={
                    item_id: policy.receipt_feedback
                    for item_id, policy in policies.items()
                },
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
    def client_config_path(client_dir: Path) -> Path:
        """Resolve config beside native ap_client, never inside launcher-data."""
        candidate = Path(client_dir)
        if candidate.is_file() or candidate.name.casefold() in {"ap_client", "ap_client.exe"}:
            candidate = candidate.parent
        return candidate / "ap_config.json"

    @staticmethod
    def _steam_id3(steam_remote_dir: object, configured: object = None) -> int:
        remote = Path(str(steam_remote_dir)).expanduser()
        if (
            remote.name.casefold() != "remote"
            or remote.parent.name != "782330"
            or remote.parent.parent.parent.name.casefold() != "userdata"
        ):
            raise ValueError(
                "steam_remote_dir must end with userdata/<STEAM_ID3>/782330/remote"
            )
        account = remote.parent.parent.name
        if not account.isascii() or not account.isdigit() or int(account) <= 0:
            raise ValueError(
                "steam_remote_dir must contain numeric userdata/<STEAM_ID3>"
            )
        inferred = int(account)
        if configured is not None:
            if isinstance(configured, bool) or not isinstance(configured, (int, str)):
                raise ValueError("steam_id3 must be a positive integer")
            try:
                configured_id = int(configured)
            except ValueError as error:
                raise ValueError("steam_id3 must be a positive integer") from error
            if configured_id <= 0 or configured_id != inferred:
                raise ValueError(
                    "steam_id3 does not match userdata directory in steam_remote_dir"
                )
        return inferred

    @staticmethod
    def write_client_config(
        client_dir: Path,
        *,
        endpoint: str | None = None,
        manifest_hash: str | None = None,
        runtime_config: Mapping[str, object] | None = None,
    ) -> Path:
        """Atomically materialize native runtime config from nonsecret launcher state."""
        path = LaunchWorkflow.client_config_path(client_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(config, dict):
            raise ValueError("native client configuration must contain an object")

        source = dict(runtime_config or {})
        for key in CLIENT_CONFIG_FIELDS - {"steam_id3", "steam_remote_dir"}:
            value = source.get(key)
            if key == "server_address" and endpoint is not None:
                value = endpoint
            elif key == "seed_manifest_hash" and manifest_hash is not None:
                value = manifest_hash
            if value is not None:
                config[key] = value

        remote = source.get("steam_remote_dir") or config.get("steam_remote_dir")
        configured_id = source.get("steam_id3")
        if configured_id is None and not source.get("steam_remote_dir"):
            configured_id = config.get("steam_id3")
        if remote:
            config["steam_remote_dir"] = str(Path(str(remote)).expanduser())
            config["steam_id3"] = LaunchWorkflow._steam_id3(
                config["steam_remote_dir"], configured_id
            )
        config.pop("password", None)

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(json.dumps(config, indent=2, sort_keys=True) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return path

    def join(self, room: dict[str, Any], client_dir: Path, endpoint: str) -> SeedManifest:
        """Offline simulation adapter. Production Join consumes RoomSnapshot."""
        options = room.get("options", {})
        dash = bool(options.get("randomize_dash", False))
        physical_options = {
            "randomize_chainsaw": bool(options.get("randomize_chainsaw", False)),
            "randomize_dash": dash,
            "randomize_first_battery": bool(options.get("randomize_first_battery", False)),
        }
        active = self.compiler.active_location_ids(physical_options)
        slot_data = {
            **physical_options,
            **({"starting_inventory": options["starting_inventory"]} if "starting_inventory" in options else {}),
            **({"starting_weapon": options["starting_weapon"]} if "starting_weapon" in options else {}),
        }
        snapshot = RoomSnapshot.from_packets(
            {"seed_name": room["seed_name"]},
            {
                "team": room["team"], "slot": room["slot"],
                "slot_data": slot_data,
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
