"""Headless seed compiler and install workflow. UI/CLI are adapters only."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
    def create(cls, *, seed_name: str, team: int, slot: int, options: dict[str, Any], active_location_ids: list[int]) -> "SeedManifest":
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


@dataclass
class InstallRecord:
    state: str
    game_path: str
    game_fingerprint: str
    endpoint: str
    manifest_hash: str
    installed_files: dict[str, str]
    rollback_path: str | None


class InstallPlan:
    """Atomic directory replacement. Injector integration remains an adapter."""

    def __init__(self, target: Path, source: Path):
        self.target = target
        self.source = source

    def install(self, record: InstallRecord) -> InstallRecord:
        staging = self.target.with_name(f".{self.target.name}.installing")
        rollback = self.target.with_name(f".{self.target.name}.rollback")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(self.source, staging)
        if rollback.exists():
            shutil.rmtree(rollback)
        if self.target.exists():
            self.target.replace(rollback)
        staging.replace(self.target)
        record.state = "active"
        record.rollback_path = str(rollback) if rollback.exists() else None
        record.installed_files = {
            str(path.relative_to(self.target)): _sha256(path)
            for path in self.target.rglob("*") if path.is_file()
        }
        return record


class ModCompiler:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def active_location_ids(self, randomize_dash: bool) -> list[int]:
        names = json.loads((self.root / "data" / "location_names.json").read_text(encoding="utf-8"))["locations"]
        ids = sorted(int(location_id) for location_id in names)
        return ids if randomize_dash else [location_id for location_id in ids if location_id != DASH_LOCATION_ID]

    def compile(self, manifest: SeedManifest, output_root: Path) -> Path:
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "seed_manifest.json").write_text(
            json.dumps(manifest.document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        dash_enabled = manifest.options.get("randomize_dash", False)
        e1m2 = json.loads((self.root / "content/maps/e1m2_war/locations.json").read_text(encoding="utf-8"))
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
        return output_entities


class LaunchWorkflow:
    def __init__(self, compiler: ModCompiler | None = None):
        self.compiler = compiler or ModCompiler()

    def join(self, room: dict[str, Any], client_dir: Path, endpoint: str) -> SeedManifest:
        """Room values are authoritative; callers must render options read-only."""
        options = room.get("options", {})
        dash = bool(options.get("randomize_dash", False))
        manifest = SeedManifest.create(
            seed_name=str(room["seed_name"]), team=int(room["team"]), slot=int(room["slot"]),
            options={"randomize_dash": dash}, active_location_ids=self.compiler.active_location_ids(dash),
        )
        client_dir.mkdir(parents=True, exist_ok=True)
        config = {"server_address": endpoint, "seed_manifest_hash": manifest.manifest_hash}
        temporary = client_dir / ".ap_config.json.tmp"
        temporary.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(client_dir / "ap_config.json")
        self.compiler.compile(manifest, client_dir / "compiled_mod")
        return manifest


def validate_game(game_root: Path, meathook_path: Path, client_dir: Path, saves_dir: Path) -> None:
    required = [game_root / "DOOMEternalx64vk.exe", game_root / "base" / "classicwads", meathook_path, client_dir / "bridge_client.py", saves_dir]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError("missing required game/install paths: " + ", ".join(missing))
