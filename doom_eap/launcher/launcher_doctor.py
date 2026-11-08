from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from doom_eap.launcher.launcher_injector import (
    LINUX_MOD_INJECTOR,
    WINDOWS_INJECTOR_REQUIRED_MEMBERS,
    WINDOWS_MOD_INJECTOR,
    DependencyManager,
    PrerequisiteStatus,
    idfile_decompressor_spec,
    installation_error_details,
    probe_core,
    verify_linux_mod_installation,
)
from doom_eap.launcher.launcher_platform import (
    AMMO_HOTKEY_STATE_FILENAME,
    AMMO_REFILL_BIND_COMMAND,
    ProcessInspectionError,
    SavedGamesSelection,
    detect_doom_processes,
    doom_saved_games_base,
    publish_file,
    resolve_doom_config_path,
    saved_games_shape_verdict,
    select_saved_games_dir,
    validate_game_root,
)
from doom_eap.launcher.launcher_support import (
    SUPPORT_DIAGNOSTIC_MAX_BYTES,
    SUPPORT_DIAGNOSTIC_MAX_ITEMS,
    SUPPORT_LOG_MAX_BYTES,
    _marker_files,
    _mtime_document,
    _native_lifecycle_summary,
    _recent_relevant_files,
    _sanitize_support_text,
    sanitize_support_value,
)
from doom_eap.launcher.ui.strings import text as player_text

from .launcher_integration import UNINSTALL_OWNED_STATES
from .launcher_native_health import NativeHealthReader, doom_base_dir_from_config
from .sentinel_diagnostics import diagnostics_directory


@dataclass(frozen=True)
class Diagnostic:
    key: str
    status: str
    message: str
    details: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RepairAction:
    """Small, previewable repair limited to launcher-owned state."""

    action_id: str
    title: str
    changes: tuple[str, ...]
    requires_confirmation: bool = False
    rollback: str = ""

    @property
    def key(self) -> str:
        return self.action_id


@dataclass(frozen=True)
class DoctorReport:
    version: str
    diagnostics: tuple[Diagnostic, ...]
    summary_status: str = "ready"
    failure_domain: str = ""
    recovery_action: str = ""
    summary_message: str = ""
    support_diagnostics: Mapping[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.summary_status == "ready" and all(item.status not in {"error", "invalid", "missing", "incompatible", "failed", "attention"} for item in self.diagnostics)

    def document(self) -> dict[str, object]:
        return {
            'version': self.version, 'ok': self.ok, 'status': self.summary_status,
            'summary_status': self.summary_status, 'failure_domain': self.failure_domain,
            'recovery_action': self.recovery_action, 'summary_message': self.summary_message,
            'diagnostics': [asdict(item) for item in self.diagnostics],
            'support_diagnostics': dict(self.support_diagnostics),
        }


logger = logging.getLogger(__name__)


def _saved_games_candidates(
    config: Mapping[str, object],
    root: Path | None,
    *,
    known: Path | None = None,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = config.get("save_games_dir")
    if configured:
        try:
            selected = Path(str(configured)).expanduser()
            candidates.extend((selected, selected.parent, selected.parent.parent))
        except (OSError, TypeError, ValueError):
            pass
    if known is not None:
        candidates.append(known)
    candidates.append(Path.home() / 'Saved Games' / 'id Software' / "DOOMEternal" / "base")
    if root is not None:
        for parent in (root, *root.parents):
            if parent.name.casefold() == "steamapps":
                candidates.append(
                    parent.parent / 'steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base'
                )
                break
    return tuple(dict.fromkeys(path.expanduser() for path in candidates))


def _saved_games_selection(configured_value, known, app_dir, client_dir, game_root, game_detected):
    selection: SavedGamesSelection | None = None
    try:
        selection = select_saved_games_dir(
            str(configured_value) if configured_value else None, known_base=known, app_dir=app_dir,
            client_dir=client_dir, game_root=game_root, game_running=game_detected,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        selection = None
        selection_error = f"Saved Games selection unavailable: {type(error).__name__}: {error}"[:256]
    else:
        selection_error = None
    return selection, selection_error


def _save_candidate_state(path, processes):
        try:
            is_dir = path.is_dir()
        except (OSError, ValueError, RuntimeError):
            is_dir = False
        recent = _recent_relevant_files(path) if is_dir else []
        game_detected = any(
        str(item.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"}
        for item in processes
    )
        return is_dir, recent, game_detected


def _save_candidate_document(path, is_dir, selected, selection, recent, game_detected, candidates):
    candidates.append({
        "path": str(path),
        "exists": is_dir,
        "selected": selected is not None and path == selected,
        "relationship": _save_relationship(path, selected, selection),
        "shape_verdict": saved_games_shape_verdict(path),
        "markers": {
            "ap_active_map": _marker_files(path, "ap_active_map") if is_dir else [{"path": str(path), "exists": False, "error": 'candidate is not an accessible directory'}],
            "ap_telemetry": _marker_files(path, "ap_telemetry") if is_dir else [{"path": str(path), "exists": False, "error": 'candidate is not an accessible directory'}],
            "ap_event": _marker_files(path, "ap_event") if is_dir else [{"path": str(path), "exists": False, "error": 'candidate is not an accessible directory'}],
        },
        "recent_writes": recent,
        "live_game_detected": game_detected,
        "live_game_writing": bool(recent) and game_detected,
        "live_game_writing_reason": (
            'recent AP/save write while DOOM process detected'
            if recent and game_detected else
            'no recent relevant write observed' if game_detected else
            'DOOM process not detected'
        ),
    })


def _save_candidate_details(config, root, known, selected, selection, processes, candidates):
    for path in _saved_games_candidates(config, root, known=known)[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]:
        is_dir, recent, game_detected = _save_candidate_state(path, processes)
        _save_candidate_document(path, is_dir, selected, selection, recent, game_detected, candidates)


def _save_selection_document(selected, configured_value, selection, candidates):
    result: dict[str, object] = {
        'effective_path': str(selected) if selected else None,
        'configured_path': str(configured_value) if configured_value else None,
        'source': selection.source if selection is not None else 'unresolved',
        'reason': selection.reason if selection is not None else 'no effective Saved Games path',
        'repaired': bool(selection is not None and selection.repaired),
        'previous_path': selection.previous_path if selection is not None else None,
        'live_evidence': list(selection.live_evidence) if selection is not None else [],
        'rejected': selection.rejected if selection is not None else [], 'candidates': candidates,
    }
    return result


def _saved_games_diagnostics(
    config: Mapping[str, object],
    root: Path | None,
    processes: Sequence[Mapping[str, object]],
    *,
    app_dir: Path | str | None = None,
    client_dir: Path | str | None = None,
) -> dict[str, object]:
    configured_value = config.get("save_games_dir")
    game_root = root.parent if root is not None and root.name.casefold() == "base" else root
    known: Path | None = None
    try:
        known = doom_saved_games_base(game_root=game_root)
    except (OSError, TypeError, ValueError, RuntimeError):
        known = None
    game_detected = any(
        str(item.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"}
        for item in processes
    )
    selection, selection_error = _saved_games_selection(configured_value, known, app_dir, client_dir, game_root, game_detected)
    selected = Path(str(selection.path)) if selection is not None and selection.path else None
    candidates = []
    _save_candidate_details(config, root, known, selected, selection, processes, candidates)
    result = _save_selection_document(selected, configured_value, selection, candidates)
    if selection_error:
        result["selection_error"] = selection_error
    return result


def _delivery_diagnostics(config, config_path=None, *, live=None):
    from doom_eap.runtime.ledger import ItemStatus, RoomKey

    from .items import ItemsQuery
    from .items_reader import LedgerReadAdapter

    metadata = live.get("delivery_ledger", {}) if live else {}
    path = Path(metadata.get("path") or config.get("delivery_ledger_file") or
                _client_state_path(config, config_path).with_name("delivery.sqlite3"))
    result = {"source": "ledger", "path": str(path), "status": "inactive"}
    if not metadata.get("room"):
        return result
    try:
        query = ItemsQuery(RoomKey(**metadata["room"]), "support-report", limit=SUPPORT_DIAGNOSTIC_MAX_ITEMS)
        reader = LedgerReadAdapter(path, expected=bool(config.get("delivery_ledger_expected")))
        snapshot = asyncio.run(reader.read(query))
    except (TypeError, ValueError, OSError) as error:
        return {**result, "status": "unavailable", "reason": type(error).__name__}
    result.update(status=snapshot.availability.value, authority=snapshot.authority.value if snapshot.authority else None)
    if snapshot.page is not None:
        result.update(watermark=snapshot.page.watermark, sampled=len(snapshot.page),
                      truncated=len(snapshot.page) == query.limit,
                      counts={state.value: sum(row.status is state for row in snapshot.page) for state in ItemStatus})
    return result


def _delivery_verdict(delivery):
    if delivery.get("status") != "available" or delivery.get("authority") != "ledger":
        return None
    counts = delivery.get("counts", {})
    if counts.get("blocked", 0):
        return False
    if delivery.get("truncated") or counts.get("received", 0) or counts.get("waiting", 0):
        return None
    return True


def _diagnostic_client_path(config, app_dir):
    save = config.get("save_games_dir")
    client_dir: Path | str | None = None
    if app_dir is not None:
        try:
            packaged_client = Path(str(app_dir)).expanduser() / "client"
            if packaged_client.is_dir():
                client_dir = packaged_client
        except (OSError, TypeError, ValueError, RuntimeError):
            client_dir = None
    return save, client_dir


def _runtime_path_fields(config_path, base, save, config, processes, app_dir, client_dir):
    details = {
        'config_file': str(config_path) if config_path else None, 'doom_base_dir': str(base) if base else None,
        'save_games_dir': str(save) if save else None, 'INV_DUMP_DIR': str(save) if save else None,
        'STEAM_REMOTE_DIR': str(config.get('steam_remote_dir')) if config.get('steam_remote_dir') else None,
        'STEAM_ID3': config.get('steam_id3'),
        'path_selection': _saved_games_diagnostics(config, base, processes, app_dir=app_dir, client_dir=client_dir),
        'markers': {}, 'materialization': {},
    }
    return details


def _runtime_markers(save, details):
    if save:
        try:
            marker_root = Path(str(save)).expanduser()
            details["markers"] = {
                'ap_active_map': _marker_files(marker_root, 'ap_active_map'),
                'ap_telemetry': _marker_files(marker_root, 'ap_telemetry'),
                'ap_event': _marker_files(marker_root, 'ap_event'),
            }
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            details["markers"] = {'error': f'marker path unavailable: {type(error).__name__}: {error}'[:256]}


def _diagnostic_game_base(config):
    base_error = None
    try:
        base = doom_base_dir_from_config(config)
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        base = None
        base_error = f"base path unavailable: {type(error).__name__}: {error}"[:256]
    return base, base_error


def _launcher_path_evidence(paths, details):
    if paths is not None:
        details["launcher_paths"] = {name: str(getattr(paths, name)) for name in ("config_dir", "state_dir", "data_dir") if getattr(paths, name, None)}


def _runtime_diagnostics(config: Mapping[str, object], config_path: Path | None, paths: object | None, processes: Sequence[Mapping[str, object]], *, app_dir: Path | str | None = None) -> dict[str, object]:
    base_error: str | None = None
    base, base_error = _diagnostic_game_base(config)
    save, client_dir = _diagnostic_client_path(config, app_dir)
    details = _runtime_path_fields(config_path, base, save, config, processes, app_dir, client_dir)
    if base_error:
        details["doom_base_dir_error"] = base_error
    _runtime_markers(save, details)
    if base is not None:
        materialization = {'ap_gameplay_save.state': base / 'ap_gameplay_save.state'}
        details["materialization"] = {name: _mtime_document(path) for name, path in materialization.items()}
    try:
        health_path = base / "ap_rpc_health.state" if base else Path()
        details["native"] = NativeHealthReader(health_path).read(force=True).document() if base else {"state": "not_ready", "reason": "base_directory_unconfigured"}
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        details["native"] = {"state": "unavailable", "reason": str(error)}
    details["native_lifecycle"] = _native_lifecycle_summary(
        config, processes, details['native'] if isinstance(details['native'], Mapping) else {},
    )
    _launcher_path_evidence(paths, details)
    return details


def _client_state_path(config: Mapping[str, object], config_path: Path | None=None) -> Path:
    configured = config.get("client_state_file")
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute() and config_path is not None:
            path = config_path.expanduser().resolve().parent / path
        return path.resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "doom-eternal-ap" / "client_state.json"


def _expected_room_compiler_digest(application_dir: Path | None = None) -> str | None:
    try:
        from .launcher_core import ROOT, RoomCompiler
        candidates = []
        if application_dir:
            candidates.append(Path(application_dir) / "resources")
        candidates.append(ROOT / "build" / "release" / "client" / "resources")
        candidates.append(ROOT / "resources")
        for res_dir in candidates:
            base = res_dir / "base_mod.zip"
            payload = res_dir / "room_payloads.zip"
            manifest = res_dir / "room_payload_manifest.json"
            if base.is_file() and payload.is_file() and manifest.is_file():
                compiler = RoomCompiler(base, payload, manifest)
                return compiler.static_content_digest
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile):
        pass
    return None


def _preparation_failure_summary(state_dir, *, filename="session_prepare_failure.json", source="last_preparation_failure"):
    if not state_dir:
        return {"status": "unavailable"}
    try:
        path = state_dir / filename
        with path.open("rb") as stream:
            raw = stream.read(SUPPORT_DIAGNOSTIC_MAX_BYTES + 1)
        if len(raw) > SUPPORT_DIAGNOSTIC_MAX_BYTES:
            return {"status": "unavailable", "reason": "diagnostic_exceeds_support_limit"}
        document = json.loads(raw)
        modified = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
    except (OSError, UnicodeError, ValueError):
        return {"status": "unavailable"}
    return {"status": "retained", "source": source, "modified_utc": modified, "evidence": sanitize_support_value(document)}


def _receipt_content_identity(application_dir, document, result):
    expected_digest = _expected_room_compiler_digest(application_dir)
    recorded_digest = str(document.get("static_content_digest", "")) if document.get("static_content_digest") else ""
    if expected_digest:
        result["expected_static_content_digest"] = expected_digest
        result["receipt_static_content_digest"] = recorded_digest
        result["content_identity_match"] = bool(recorded_digest and recorded_digest == expected_digest)
        if not recorded_digest or recorded_digest != expected_digest:
            result["content_status"] = "stale_content_identity"
    else:
        result["content_identity_match"] = True
    return expected_digest, recorded_digest


def _staged_receipt_hash(staged, expected, expected_digest, recorded_digest, result):
    if isinstance(staged, str) and staged and isinstance(expected, str) and expected:
        staged_path = Path(staged)
        result["staged_exists"] = staged_path.is_file()
        if staged_path.is_file():
            try:
                actual = hashlib.sha256(staged_path.read_bytes()).hexdigest()
                result["staged_sha256_actual"] = actual
                content_fresh = not expected_digest or recorded_digest == expected_digest
                result["hash_match"] = (actual == expected) and content_fresh
            except OSError:
                result["hash_match"] = False


def _record_generated_hash(generated, result, expected_digest, recorded_digest, generated_expected, expected):
    generated_path = Path(generated)
    result["generated_exists"] = generated_path.is_file()
    if generated_path.is_file():
        try:
            generated_actual = hashlib.sha256(generated_path.read_bytes()).hexdigest()
            result["generated_sha256_actual"] = generated_actual
            content_fresh = not expected_digest or recorded_digest == expected_digest
            result["generated_hash_match"] = (generated_actual == generated_expected) and content_fresh
            if isinstance(expected, str) and expected and "staged_sha256_actual" in result:
                result["generated_stage_match"] = (generated_actual == result["staged_sha256_actual"]) and content_fresh
        except OSError:
            result["generated_hash_match"] = False


def _generated_receipt_hash(document, result, expected_digest, recorded_digest, expected):
    generated = document.get("generated_mod")
    generated_expected = document.get("generated_sha256")
    if isinstance(generated, str) and generated and isinstance(generated_expected, str) and generated_expected:
        _record_generated_hash(generated, result, expected_digest, recorded_digest, generated_expected, expected)


def _receipt_support_fields(document, result):
    for key in (
        'schema', 'manifest_hash', 'static_content_digest', 'staged_mod', 'staged_sha256', 'room_zip_sha256',
        'generated_mod', 'generated_sha256', 'mods_path', 'injector_version', 'injector_command', 'adapter_command',
        'injector_exit_code', 'required_resources', 'post_install_verification', 'adapter_details', 'adapter_message',
        'adapter_state', 'installation_mode', 'steam_launch_option',
    ):
        if key in document:
            result[key] = document[key]


def _room_install_receipt_summary(state_dir: Path | None, application_dir: Path | None=None) -> dict[str, object]:
    path = state_dir / "launcher_setup.json" if state_dir else None
    result: dict[str, object] = {
        'status': 'unavailable', 'source': 'launcher_install_receipt', 'path': str(path) if path else None,
    }
    if path is None:
        result["reason"] = "state_directory_unconfigured"
        return result
    try:
        if not path.is_file():
            result["reason"] = "not_found"
            return result
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        result["reason"] = f"unreadable: {type(error).__name__}"
        return result
    if not isinstance(document, Mapping):
        result["reason"] = "malformed"
        return result
    _receipt_support_fields(document, result)
    expected_digest, recorded_digest = _receipt_content_identity(application_dir, document, result)
    staged = document.get("staged_mod")
    expected = document.get("staged_sha256")
    _staged_receipt_hash(staged, expected, expected_digest, recorded_digest, result)
    result["support_evidence"] = {
        key: result[key]
        for key in (
            'room_zip_sha256', 'generated_sha256', 'mods_path', 'injector_version', 'injector_command',
            'adapter_command', 'injector_exit_code', 'required_resources', 'post_install_verification',
            'adapter_details', 'adapter_message',
        )
        if key in result
    }
    _generated_receipt_hash(document, result, expected_digest, recorded_digest, expected)
    result["status"] = "available"
    return result


def _verify_receipt_zip(receipt):
    staged = Path(str(receipt.get("staged_mod", ""))).expanduser()
    staged_expected = str(receipt.get("staged_sha256", ""))
    room_zip_expected = str(receipt.get("room_zip_sha256", staged_expected))
    generated_path = Path(str(receipt.get("generated_mod", ""))).expanduser()
    generated_expected = str(receipt.get("generated_sha256", ""))
    if not staged_expected or not room_zip_expected or not generated_expected or not generated_path.is_file():
        raise ValueError('Linux install record lacks complete room ZIP hash evidence')
    staged_actual = hashlib.sha256(staged.read_bytes()).hexdigest()
    generated_actual = hashlib.sha256(generated_path.read_bytes()).hexdigest()
    if not (staged_actual == staged_expected == room_zip_expected == generated_expected == generated_actual):
        raise ValueError('generated and staged room ZIP hashes differ')
    return staged


def _verified_resource_evidence(resources):
    return isinstance(resources, list | tuple) and len(resources) in (3, 4) and all(
        isinstance(item, Mapping) and item.get("status") == "verified" for item in resources)


def _verify_injector_receipt(receipt, root):
    raw_command = receipt.get("injector_command", receipt.get("adapter_command", ()))
    raw_resources = receipt.get("required_resources", ())
    expected_mods = str((root / "Mods").resolve())
    if (
        not str(receipt.get("injector_version", ""))
        or not isinstance(raw_command, list | tuple) or not raw_command
        or receipt.get("injector_exit_code") != 0
        or not _verified_resource_evidence(raw_resources)
        or str(receipt.get("mods_path", "")) != expected_mods
    ):
        raise ValueError('Linux install record lacks complete injector or resource evidence')


def _verify_linux_receipt(
    receipt: Mapping[str, object],
    config: Mapping[str, object],
    *,
    application_dir: Path | None = None,
) -> dict[str, object]:
    if os.name == "nt":
        return {"state": "not_applicable"}
    base = Path(str(config.get("doom_base_dir", ""))).expanduser().resolve()
    root = base.parent if base.name.casefold() == "base" else base
    staged = _verify_receipt_zip(receipt)
    expected_digest = _expected_room_compiler_digest(application_dir)
    if expected_digest:
        recorded_digest = str(receipt.get("static_content_digest", ""))
        if not recorded_digest or recorded_digest != expected_digest:
            raise ValueError('installed package static content is stale ({value_0} != {value_1})'.format(value_0=f"{recorded_digest or 'missing'}", value_1=f'{expected_digest}'))
    _verify_injector_receipt(receipt, root)
    verification = verify_linux_mod_installation(root, staged)
    if str(receipt.get("post_install_verification", "")) != "verified":
        raise ValueError('Linux post-install verification is not recorded')
    return verification


def _support_bridge_paths(config, config_path, paths, runtime, live):
    config_paths: dict[str, object] = {
        'config_file': str(config_path) if config_path else None,
        'application_dir': str(getattr(paths, 'application_dir', '')) if paths else None,
        'client_state_file': str(_client_state_path(config, config_path)),
        'doom_base_dir': runtime.get('doom_base_dir'), 'save_games_dir': runtime.get('save_games_dir'),
        'delivery_ledger_file': config.get('delivery_ledger_file'),
    }
    live_paths = live.get("config_paths") if live else None
    if isinstance(live_paths, Mapping):
        config_paths.update(dict(live_paths))
    configured_supervisor = live.get("supervisor") if live else None
    if isinstance(configured_supervisor, Mapping):
        supervisor = dict(configured_supervisor)
        bridge_status = str(supervisor.get("status", "unknown"))
    else:
        supervisor = {"status": "unknown", "reason": "live_supervisor_not_provided"}
        bridge_status = "unknown"
    bridge = {"status": bridge_status, "supervisor": supervisor}
    return config_paths, supervisor, bridge_status, bridge


def _support_native_rpc(runtime, live):
    direct_native = runtime.get("native")
    direct_native = dict(direct_native) if isinstance(direct_native, Mapping) else {}
    native = live.get("native_rpc") if live else None
    native_rpc = dict(native) if isinstance(native, Mapping) else {
        'source': 'direct' if direct_native.get('native_state') else 'unknown',
        'evidence': 'direct' if direct_native.get('native_state') else 'unavailable', 'health': direct_native,
    }
    native_rpc["lifecycle"] = dict(runtime.get("native_lifecycle", {}))
    native_rpc.setdefault(
            "evidence",
            "direct" if native_rpc.get("source") == "direct" else (
                "last_known" if native_rpc.get("source") == "last_known" else "unavailable"
            ),
        )
    native_status = str(native_rpc.get("health", {}).get("state", "unknown")) if isinstance(native_rpc.get("health"), Mapping) else "unknown"
    return native_rpc, native_status


def _telemetry_marker_evidence(runtime):
    markers = runtime.get("markers")
    markers = markers if isinstance(markers, Mapping) else {}
    telemetry_files = sum(
        1
        for value in markers.values()
        if isinstance(value, list)
        for entry in value
        if isinstance(entry, Mapping) and entry.get("exists") is True
    )
    telemetry = {
        'status': 'available' if telemetry_files else 'unavailable', 'marker_files': telemetry_files,
        'reason': 'marker_evidence_observed' if telemetry_files else 'no_marker_evidence',
    }
    return telemetry


def _support_telemetry_safety(runtime, native_rpc, config, config_path, live):
    telemetry = _telemetry_marker_evidence(runtime)
    native_safe = native_rpc["lifecycle"].get("native_safe")
    current_health = native_rpc.get("health", {})
    safety = {
        'status': 'enabled' if native_safe in (True, 'yes', '1') and current_health.get('ready') is True else 'unknown',
        'reason': 'native_safety_observation',
    }
    telemetry_safety = {
        'status': 'ready' if telemetry['status'] == 'available' and safety['status'] == 'enabled' else 'degraded',
        'telemetry': telemetry, 'safety': safety,
    }
    delivery = _delivery_diagnostics(config, config_path, live=live)
    return telemetry_safety, delivery


def _delivery_link_issues(delivery, issues):
    if delivery["status"] in {"missing", "unavailable", "incompatible"}:
        issues.append("delivery_ledger_unavailable")
    if delivery.get("counts", {}).get("blocked", 0):
        issues.append("delivery_has_blocked_items")


def _game_link_issues(meathook, game_running, bridge_status, native_status, telemetry_safety, delivery):
    meathook_status = str(meathook.get("status", "unknown"))
    issues: list[str] = []
    if meathook_status not in {"ok", "verified"}:
        issues.append("game_link_runtime_not_verified")
    if game_running and bridge_status in {"unavailable", "stopped", "failed", "unknown"}:
        issues.append("bridge_not_available_for_running_game")
    if game_running and native_status in {"degraded", "not_ready", "unknown"}:
        issues.append("native_rpc_not_ready")
    if telemetry_safety["status"] != "ready" and game_running:
        issues.append("telemetry_or_safety_evidence_not_ready")
    _delivery_link_issues(delivery, issues)
    if issues:
        overall_status = "attention" if "game_link_runtime_not_verified" in issues else "degraded"
        overall_reason = issues[0]
    else:
        overall_status = "ready"
        overall_reason = "game_link_evidence_consistent"
    return overall_status, overall_reason, issues


def _stale_hotkey_bind(config):
    stale_config_bind_present = False
    cfg_path = resolve_doom_config_path(config)
    if cfg_path is not None and cfg_path.is_file():
        try:
            cfg_text = cfg_path.read_text(encoding="utf-8", errors="replace")
            stale_config_bind_present = AMMO_REFILL_BIND_COMMAND in cfg_text
        except (OSError, UnicodeError, IndexError) as error:
            logger.warning("Hotkey diagnostic unavailable: %s", error)
    return stale_config_bind_present


def _support_hotkey_state(config):
    base_dir = doom_base_dir_from_config(config)
    hotkey_file = (base_dir / "ap_queue" / AMMO_HOTKEY_STATE_FILENAME) if base_dir else None
    hotkey_exists = hotkey_file is not None and hotkey_file.is_file()
    configured_ammo_key = str(config.get("ammo_refill_keybind", "F9"))
    parsed_key = 'UNBOUND'
    if hotkey_exists and hotkey_file is not None:
        try:
            first_line = hotkey_file.read_text(encoding="utf-8").splitlines()[0].strip()
            parts = first_line.split()
            if len(parts) >= 2 and parts[0] == "AP_AMMO_REFILL_HOTKEY_V1":
                parsed_key = parts[1]
            elif len(parts) == 1:
                parsed_key = parts[0]
        except (OSError, UnicodeError, IndexError) as error:
            logger.warning("Hotkey diagnostic unavailable: %s", error)

    stale_config_bind_present = _stale_hotkey_bind(config)

    return configured_ammo_key, hotkey_file, hotkey_exists, parsed_key, stale_config_bind_present


def _support_game_link_diagnostics(
    config: Mapping[str, object],
    config_path: Path | None,
    paths: object | None,
    processes: Sequence[Mapping[str, object]],
    runtime: Mapping[str, object],
    meathook: Mapping[str, object],
    *,
    live: Mapping[str, object] | None = None,
) -> dict[str, object]:
    game_running = any(
        str(item.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"}
        for item in processes
    )
    process = {
        'status': 'running' if game_running else 'not_running', 'game_detected': game_running,
        'items': [dict(item) for item in processes[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]],
    }
    config_paths, supervisor, bridge_status, bridge = _support_bridge_paths(config, config_path, paths, runtime, live)

    native_rpc, native_status = _support_native_rpc(runtime, live)

    telemetry_safety, delivery = _support_telemetry_safety(runtime, native_rpc, config, config_path, live)

    overall_status, overall_reason, issues = _game_link_issues(meathook, game_running, bridge_status, native_status, telemetry_safety, delivery)

    configured_ammo_key, hotkey_file, hotkey_exists, parsed_key, stale_config_bind_present = _support_hotkey_state(config)
    ammo_hotkey = {
        'configured_key': configured_ammo_key, 'state_file_path': str(hotkey_file) if hotkey_file else None,
        'state_file_exists': hotkey_exists, 'parsed_key': parsed_key,
        'stale_config_bind_present': stale_config_bind_present,
    }

    return {
        "runtime": dict(meathook),
        "process": process,
        "bridge": bridge,
        "supervisor": supervisor,
        "config_paths": config_paths,
        "native_rpc": native_rpc,
        "telemetry_safety": telemetry_safety,
        "delivery": delivery,
        "ammo_hotkey": ammo_hotkey,
        "overall": {
            'status': overall_status, 'reason': overall_reason, 'issues': issues,
            'derived_from': ['process', 'bridge', 'native_rpc', 'telemetry_safety', 'delivery', 'ammo_hotkey'],
        },
    }


def installation_diagnostics(config):
    from .install_journal import InstallJournal
    base = config.get("doom_base_dir") or config.get("game_root")
    if not base:
        return {"status": "unavailable"}
    path = Path(str(base)).expanduser()
    root = path.parent if path.name.casefold() == "base" else path
    try:
        return InstallJournal(root).summary()
    except (OSError, RuntimeError) as error:
        return {"status": "unreadable", "error": str(error)}


def _idfile_diagnostics(data_dir):
    selected_spec = idfile_decompressor_spec()
    idfile_diagnostics: dict[str, object] = {"status": "unsupported" if selected_spec is None else "unknown"}
    if selected_spec is not None:
        idfile_diagnostics.update({
            'expected_version': selected_spec.version, 'expected_commit': selected_spec.commit,
            'source_url': selected_spec.url, 'expected_size': selected_spec.expected_size,
            'expected_sha256': selected_spec.sha256,
        })
        idfile_diagnostics["selected_spec"] = {
            'name': selected_spec.name, 'version': selected_spec.version, 'commit': selected_spec.commit,
            'url': selected_spec.url, 'sha256': selected_spec.sha256, 'executable': selected_spec.executable_glob,
            'platforms': sorted(selected_spec.platforms),
        }
    if data_dir is not None:
        cache_dir = data_dir / "dependencies"
        idfile_diagnostics["cache_dir"] = cache_dir
        idfile_diagnostics["cache_dir_display"] = str(cache_dir)
        idfile_diagnostics["cache_dir_canonical"] = str(cache_dir.resolve())
        manager = DependencyManager(cache_dir)
        if selected_spec is None or not manager.platform_supported(selected_spec):
            idfile_diagnostics.update({
                'status': 'unsupported', 'platform': manager.current_platform_name(),
                'supported_platforms': ['linux', 'windows'],
            })
        else:
            try:
                cache_details = manager.cache_diagnostics(selected_spec)
            except OSError as error:
                idfile_diagnostics.update({"status": "unreadable", "error": str(error)})
            else:
                idfile_diagnostics.update(cache_details)
    return idfile_diagnostics


def _support_directories(paths):
    state_value = getattr(paths, "state_dir", None) if paths else None
    try:
        state_dir = Path(state_value).expanduser() if state_value else None
    except (OSError, TypeError, ValueError, RuntimeError):
        state_dir = None
    data_value = getattr(paths, "data_dir", None) if paths else None
    try:
        data_dir = Path(data_value).expanduser() if data_value else None
    except (OSError, TypeError, ValueError, RuntimeError):
        data_dir = None
    return state_dir, data_dir


def _support_diagnostic_document(live, game_link, state_dir, app_dir, config, idfile_diagnostics, ledger_timeline):
    return {
        "ledger_timeline": ledger_timeline(live.get("delivery_ledger", {}) if live else {}),
        **{key: live[key] for key in ("ap_session", "native_diagnostics", "native_helper_startup")
           if live and key in live},
        "item_state": game_link["delivery"],
        "room_install_receipt": _room_install_receipt_summary(state_dir, app_dir),
        "installation": installation_diagnostics(config),
        "session_preparation_failure": _preparation_failure_summary(state_dir),
        "session_admission_failure": _preparation_failure_summary(
            state_dir, filename="session_admission_failure.json", source="last_admission_failure"),
        "game_link": game_link,
        "idfile_decompressor": idfile_diagnostics,
        "mod_injector": _injector_support_details(),
    }


def build_support_diagnostics(
    config: Mapping[str, object],
    config_path: Path | None,
    paths: object | None,
    processes: Sequence[Mapping[str, object]],
    runtime: Mapping[str, object],
    meathook: Mapping[str, object],
    *,
    live: Mapping[str, object] | None = None,
) -> dict[str, object]:
    state_dir, data_dir = _support_directories(paths)
    idfile_diagnostics = _idfile_diagnostics(data_dir)
    app_val = config.get("application_dir") or getattr(paths, "application_dir", None)
    app_dir = Path(app_val).expanduser() if app_val else None
    game_link = _support_game_link_diagnostics(config, config_path, paths, processes, runtime, meathook, live=live)
    from doom_eap.launcher.launcher_support import ledger_timeline

    return _support_diagnostic_document(live, game_link, state_dir, app_dir, config, idfile_diagnostics, ledger_timeline)


def _read_native_startup_record(path, identity):
    with path.open("rb") as stream:
        raw = stream.read(SUPPORT_DIAGNOSTIC_MAX_BYTES + 1)
    if len(raw) > SUPPORT_DIAGNOSTIC_MAX_BYTES:
        raise ValueError('native diagnostic exceeds support limit')
    data = json.loads(raw)
    if not isinstance(data, dict) or [str(data.get("pid")), str(data.get("process_created"))] != identity:
        raise ValueError('native diagnostic process identity mismatch')
    return data


def _matching_startup_record(data, build, session, identity, records, path):
    if data.get("build_id") != build:
        return
    namespace = session.get("namespace_id")
    if namespace and data.get("admission", {}).get("namespace_id") != namespace:
        return
    current = [str(session.get("pid")), str(session.get("process_created"))] == identity
    records.append({"source_filename": path.name, "evidence": "current_process" if current else "last_known",
                    "diagnostic": sanitize_support_value(data)})


def _collect_startup_records(candidates, build, session, records, errors):
    for path in candidates[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]:
        identity = path.name.removesuffix(".latest.json").split("-")
        if len(identity) != 2 or not all(part.isdecimal() for part in identity):
            continue
        try:
            data = _read_native_startup_record(path, identity)
            _matching_startup_record(data, build, session, identity, records, path)
            if len(records) == 2:
                break
        except (OSError, ValueError, TypeError, AttributeError) as error:
            errors.append({"source_filename": path.name, "reason": type(error).__name__})


def _matching_startup_diagnostics(candidates, build, session):
    records = []
    errors = []
    _collect_startup_records(candidates, build, session, records, errors)
    return {"status": "available" if records else "unavailable", "records": records,
            "errors": errors, "reason": None if records else "no_matching_native_diagnostic"}


def _native_startup_diagnostics(config, session, install):
    directory = diagnostics_directory(config or {})
    if directory is None:
        return {"status": "unavailable", "reason": "native_log_location_unavailable"}
    manifest = install["manifest"].get("document")
    build = manifest.get("build_id") if isinstance(manifest, dict) else None
    if not build:
        return {"status": "unavailable", "reason": "installed_build_unknown"}
    try:
        candidates = sorted(directory.glob("*.latest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "unavailable", "reason": f"native_log_inspection_failed: {type(error).__name__}"}

    return _matching_startup_diagnostics(candidates, build, session)


def _collect_darklord_logs(candidates, limit, session, logs, records):
    for path in candidates[:4]:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > limit:
                stream.seek(size - limit)
                stream.readline()
            raw = stream.read(limit)
        lines = [line for line in raw.decode("utf-8", errors="replace").splitlines()
                 if line.startswith("AP_DARKLORD ")]
        if not lines:
            continue
        identity = path.name.removesuffix(".darklord.log").split("-")
        current = identity == [str(session.get("pid")), str(session.get("process_created"))]
        name = "darklord/" + path.name
        logs[name] = _sanitize_support_text("\n".join(lines)) + "\n"
        records.append({"archive_member": name, "pid": int(identity[0]),
                        "process_created": int(identity[1]), "line_count": len(lines),
                        "source_size": size, "truncated": size > limit,
                        "evidence": "current_process" if current else "last_known"})


def _darklord_diagnostics(config, session):
    directory = diagnostics_directory(config or {})
    if directory is None:
        return {}, {"status": "unavailable", "reason": "native_log_location_unavailable"}
    records, errors, logs = [], [], {}
    limit = SUPPORT_LOG_MAX_BYTES
    try:
        candidates = sorted(
            (path for path in directory.glob("*.darklord.log")
             if re.fullmatch(r"\d+-\d+\.darklord\.log", path.name) and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        _collect_darklord_logs(candidates, limit, session, logs, records)
    except OSError as error:
        errors.append(type(error).__name__)
    return logs, {"status": "available" if logs else "unavailable", "records": records,
                  "errors": errors, "reason": None if logs else "no_darklord_telemetry"}


def _file_identity(path, name):
    result = {"name": name}
    try:
        if path.is_symlink():
            return {**result, "status": "omitted", "reason": "symbolic_link"}
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        result.update(status="available", size=size, sha256=digest.hexdigest())
    except FileNotFoundError:
        result["status"] = "missing"
    except OSError as error:
        result.update(status="unreadable", reason=str(error))
    return result


def _install_file_inventory(root, base, result):
    try:
        result["game_found"] = (root / "DOOMEternalx64vk.exe").is_file() and base.is_dir()
        names = {"sentinel_core.dll", "msimg32.dll", "XINPUT1_3.dll"}
        names.update(path.name for path in root.iterdir() if path.suffix.casefold() == ".dll")
        result["dlls"] = [_file_identity(root / name, name) for name in sorted(names)]
        mods = root / "Mods"
        result["mods_status"] = "available" if mods.is_dir() else "missing"
        result["mods"] = [_file_identity(path, path.relative_to(mods).as_posix())
                          for path in sorted(mods.rglob("*")) if path.is_file() or path.is_symlink()]
    except OSError as error:
        result["issues"].append(str(error))


def _install_ownership(document, result):
    artifacts = {item["path"].casefold(): item for item in document["artifacts"]}
    installed = {item["name"].casefold(): item for item in result["dlls"]}
    pair = [installed.get(name, {}) for name in ("sentinel_core.dll", "msimg32.dll")]
    matches = all(item.get("status") == "available" and
                  all(item[field] == artifacts[name][field] for field in ("size", "sha256"))
                  for name, item in zip(("sentinel_core.dll", "msimg32.dll"), pair, strict=False))
    result["ownership"] = {"status": "owned" if matches else "unverified",
                           "reason": 'Core and bootstrap hashes match manifest' if matches else 'Core or bootstrap missing or different'}


def _install_manifest_diagnostics(result, root, _unique, validate_manifest):
    manifest = result["manifest"]
    try:
        with (root / "sentinel-distribution.json").open("rb") as stream:
            raw = stream.read(SUPPORT_DIAGNOSTIC_MAX_BYTES + 1)
        if len(raw) > SUPPORT_DIAGNOSTIC_MAX_BYTES:
            manifest.update(status="omitted", reason='Manifest exceeds 256 KiB')
            return result
        document = json.loads(raw, object_pairs_hook=_unique)
    except FileNotFoundError:
        manifest.update(status="missing")
        return result
    except (OSError, ValueError, UnicodeError) as error:
        manifest.update(parses=False, status="unreadable", reason=str(error))
        return result
    manifest.update(parses=True, status="available", document=document)
    try:
        validate_manifest(document, require_compatible=False)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        result["structure"] = {"status": "invalid", "reason": str(error)}
        return result
    result["structure"] = {"status": "valid"}
    _install_ownership(document, result)
    try:
        validate_manifest(document)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        result["compatibility"] = {"status": "incompatible", "reason": str(error)}
    else:
        result["compatibility"] = {"status": "compatible"}
    return result


def _install_diagnostics(config):
    from ..contracts.core_distribution import _unique, validate_manifest

    base = doom_base_dir_from_config(config or {})
    result = {"game_found": None, "path_type": "Proton" if os.name != "nt" or (config or {}).get("proton_compat_data_dir") else "Windows",
              "game_root": str(base.parent) if base else None, "dlls": [], "mods": [],
              "manifest": {"parses": None}, "structure": {"status": "unknown"},
              "ownership": {"status": "unknown"}, "compatibility": {"status": "unknown"}, "issues": []}
    if base is None:
        result["issues"].append('Game directory is not configured')
        return result
    root = base.parent
    _install_file_inventory(root, base, result)
    return _install_manifest_diagnostics(result, root, _unique, validate_manifest)


def _setup_failure_summary(payload, support):
    failure = payload.get("last_setup_failure") or support.get("session_preparation_failure", {}).get("evidence") or {}
    if failure:
        return ('FAIL Last setup failure: {value_0} | {value_1} | date={value_2}'.format(value_0=f"{failure.get('failure_domain', 'unknown')}", value_1=f"{failure.get('user_message') or failure.get('message') or failure.get('technical_message') or 'unknown'}", value_2=f"{failure.get('at_utc') or failure.get('timestamp') or support.get("session_preparation_failure", {}).get('modified_utc', 'unknown')}"))
    else:
        return ('UNKNOWN Last setup failure: none recorded')


def _save_relationship(path, selected, selection):
    if selection is None:
        return "discovered_candidate"
    if selected is not None and path == selected:
        if selection.source == "configured_saved_games_dir":
            return "configured"
        if selection.source in {"canonical_saved_games_base", "live_writer_evidence", "single_viable_candidate"}:
            return "canonical_known_folder"
    if selection.previous_path is not None and str(path) == selection.previous_path:
        return "rejected_stale_config"
    return "discovered_candidate"


def _injector_support_details():
    spec = WINDOWS_MOD_INJECTOR if os.name == "nt" else LINUX_MOD_INJECTOR
    return {"name": spec.name, "version": spec.version, "url": spec.url,
            "expected_size": spec.expected_size, "expected_sha256": spec.sha256,
            "required_members": list(WINDOWS_INJECTOR_REQUIRED_MEMBERS) if os.name == "nt" else [LINUX_MOD_INJECTOR.executable_glob]}


def _installation_receipt_message(receipt):
    return str(receipt.get("adapter_message") or 'Install the room mod before playing.')


class LauncherDoctor:
    VERSION = "0.8.0"

    def __init__(
        self,
        *,
        config: Mapping[str, object] | None = None,
        paths: object | None = None,
        config_path: Path | None = None,
        last_setup_failure: Mapping[str, object] | None = None,
        last_room_package_issue: Mapping[str, object] | None = None,
        live_support: Mapping[str, object] | None = None,
    ):
        self.config = dict(config or {})
        self.paths = paths
        self.config_path = config_path
        self.last_setup_failure = dict(last_setup_failure or {})
        self.last_room_package_issue = dict(last_room_package_issue or {})
        self.live_support = dict(live_support or {})

    def _current_issue(self) -> dict[str, object] | None:
        for issue in (self.last_room_package_issue, self.last_setup_failure):
            if issue.get("failure_domain") in {"room_package", "installed_room_package"}:
                return issue
        return None

    def _state_dir(self) -> Path | None:
        value = getattr(self.paths, "state_dir", None)
        return Path(value).expanduser().resolve() if value else None

    def _application_dir(self) -> Path | None:
        value = self.config.get("application_dir") or getattr(self.paths, "application_dir", None)
        return Path(str(value)).expanduser().resolve() if value else None

    def _game_link_repairs(self, actions):
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        if game_root:
            try:
                root = Path(str(game_root))
                root = validate_game_root(root.parent if root.name.lower() == "base" else root)
                meathook_probe = probe_core(root)
                if meathook_probe.status == PrerequisiteStatus.MISSING:
                    actions.append(RepairAction(
                        'install_game_link', 'Set up game connection',
                        ('Download and install the matching game connection files.',), False,
                        'Requires matching game connection files.',
                    ))
                elif meathook_probe.status in {PrerequisiteStatus.INCOMPATIBLE, PrerequisiteStatus.INVALID}:
                    actions.append(RepairAction(
                        'repair_game_link', 'Repair game connection',
                        ('Check existing files', 'Reinstall matching connection files'), True,
                        'Other mods are kept. Previous files are restored if installation fails.',
                    ))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                logger.warning("Game connection repair inspection failed: %s", error)

        return game_root

    def _codec_repair_actions(self, codec_supported, codec_inspected, actions, selected_spec, codec_legacy_present):
        if codec_supported:
            if codec_inspected is None:
                actions.append(RepairAction(
                    "repair_idfile_decompressor",
                    'Repair mod-building tool',
                    (
                        'Download and check version {value_0}'.format(value_0=f'{selected_spec.version}'),
                        'Verify SHA-256: {value_0}'.format(value_0=f'{selected_spec.sha256}'),
                    ),
                    True,
                    'Store the checked tool for reuse.',
                ))
            else:
                actions.append(RepairAction(
                    'remove_idfile_decompressor', 'Remove downloaded mod-building tool',
                    ('Remove the downloaded tool.',), True, 'Setup will ask before downloading it again.',
                ))
        elif codec_legacy_present:
            actions.append(RepairAction(
                'remove_idfile_decompressor', 'Remove incompatible mod-building tool',
                ('Remove the downloaded version that cannot run on this system.',), True,
                'This downloaded version cannot run on Windows.',
            ))

    def _codec_repairs(self, actions):
        data_value = getattr(self.paths, "data_dir", None)
        data_dir = Path(data_value).expanduser() if data_value else None
        selected_spec = idfile_decompressor_spec()
        if data_dir is not None and selected_spec is not None:
            codec_manager = DependencyManager(data_dir / "dependencies")
            try:
                codec_supported = codec_manager.platform_supported(selected_spec)
                codec_inspected = codec_manager.inspect(selected_spec) if codec_supported else None
                codec_legacy_present = (
                    not codec_supported and codec_manager.cache_present(selected_spec)
                )
            except (OSError, TimeoutError, RuntimeError):
                codec_supported = False
                codec_inspected = None
                codec_legacy_present = False
            self._codec_repair_actions(codec_supported, codec_inspected, actions, selected_spec, codec_legacy_present)

    def _windows_resource_repair(self, adapter_state, root, receipt, actions, staged):
        if os.name == "nt" and adapter_state == "applied" and root is not None:
            try:
                from .campaign_resources import verify_installed_resources
                verify_installed_resources(root, receipt)
            except (OSError, ValueError) as error:
                actions.append(RepairAction(
                    'reinstall_room_mod', 'Repair changed game files', ('Reinstall ' + staged.name,), True, str(error),
                ))
                return True
        return False

    def _linux_resource_repair(self, adapter_state, receipt, actions, staged):
        if os.name != "nt" and adapter_state == "applied":
            try:
                _verify_linux_receipt(receipt, self.config, application_dir=self._application_dir())
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                actions.append(RepairAction(
                    "reinstall_room_mod", 'Repair room mod',
                    ('Reinstall and check {value_0}'.format(value_0=f'{staged.name}'),), True,
                    'Installation failed at verify: {value_0}: {value_1}'.format(value_0=f'{type(error).__name__}', value_1=f'{error}'),
                ))
                return True
        return False

    def _missing_room_mod_repair(self, staged, actions):
        if not staged.is_file():
            actions.append(RepairAction(
                'reinstall_room_mod', 'Reinstall missing room mod', ('Install ' + staged.name,), True,
                'Previous files are backed up.',
            ))
            return True
        return False

    def _repair_receipt_identity(self, receipt_path, root):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError('record must be an object')
        staged = Path(str(receipt["staged_mod"])).resolve()
        digest = str(receipt["staged_sha256"])
        adapter_state = str(receipt.get("adapter_state", ""))
        if root is not None:
            owned_location = staged.parent == root / "Mods"
            if not owned_location:
                raise ValueError('recorded package is outside configured Mods folder')
        return receipt, staged, digest, adapter_state

    def _changed_room_mod_repair(self, staged, digest, actions):
        actual = hashlib.sha256(staged.read_bytes()).hexdigest()
        if actual != digest:
            actions.append(RepairAction(
                "reinstall_room_mod", 'Reinstall changed room mod',
                ('Replace ' + staged.name, 'SHA-256: {value_0} → {value_1}'.format(value_0=f'{actual}', value_1=f'{digest}')), True,
                'Previous files are backed up.',
            ))
            return True
        return False

    def _repair_game_root(self, game_root):
        root: Path | None = None
        if game_root:
            root = Path(str(game_root)).expanduser().resolve()
            if root.name.casefold() == "base":
                root = root.parent
        return root

    def _receipt_repairs(self, game_root, state_dir, actions, issue):
        root = self._repair_game_root(game_root)

        receipt_path = state_dir / "launcher_setup.json"
        if not receipt_path.is_file():
            return tuple(actions)
        try:
            receipt, staged, digest, adapter_state = self._repair_receipt_identity(receipt_path, root)
            if adapter_state in UNINSTALL_OWNED_STATES:
                return tuple(actions)
            missing = self._missing_room_mod_repair(staged, actions)
            if missing:
                return tuple(actions)
            changed = self._changed_room_mod_repair(staged, digest, actions)
            if changed:
                return tuple(actions)
            needs_repair = self._windows_resource_repair(adapter_state, root, receipt, actions, staged)
            if needs_repair:
                return tuple(actions)
            needs_repair = self._linux_resource_repair(adapter_state, receipt, actions, staged)
            if needs_repair:
                return tuple(actions)
            if adapter_state != "applied":
                actions.append(RepairAction(
                    'reinstall_room_mod', player_text('common', 'install_room_mod'),
                    ('Complete installation in EternalModInjector.',), True, 'Confirm installation when prompted.',
                ))
                return tuple(actions)
        except (OSError, RuntimeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            if issue is not None:
                return tuple(actions)
            actions.append(RepairAction(
                'archive_stale_install_record', 'Reset saved installation details',
                ('Back up installation details: {value_0}'.format(value_0=f'{receipt_path.name}'),), False,
                'Previous details can be restored from the backup.',
            ))
            return tuple(actions)
        return tuple(actions)

    def repair_actions(self) -> tuple[RepairAction, ...]:
        """Offer actions only when launcher receipt proves ownership."""
        actions: list[RepairAction] = []
        game_root = self._game_link_repairs(actions)
        state_dir = self._state_dir()
        self._codec_repairs(actions)
        issue = self._current_issue()
        if issue is not None:
            action_id = (
                "update_room_package"
                if issue.get("recovery_action") == "update_room_package"
                else "rebuild_room_package"
            )
            actions.append(RepairAction(
                action_id, 'Update room mod' if action_id == 'update_room_package' else 'Rebuild room mod',
                ('Build and check the mod for this room.',), True, 'Existing saves and received items are kept.',
            ))
        if state_dir is None:
            return tuple(actions)

        return self._receipt_repairs(game_root, state_dir, actions, issue)

    def archive_stale_install_record(self) -> Path:
        """Move only launcher state receipt into rollback storage."""
        state_dir = self._state_dir()
        if state_dir is None:
            raise ValueError('launcher state directory is unavailable')
        receipt = state_dir / "launcher_setup.json"
        if not receipt.is_file():
            raise ValueError('launcher install record is unavailable')
        backups = state_dir / "repair-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / "launcher_setup.json"
        if backup.exists():
            raise ValueError('repair backup already exists; restore or remove it first')
        publish_file(receipt, backup, operation="archive_install_record")
        return backup

    def restore_archived_install_record(self) -> Path:
        state_dir = self._state_dir()
        if state_dir is None:
            raise ValueError('launcher state directory is unavailable')
        backup = state_dir / "repair-backups" / "launcher_setup.json"
        receipt = state_dir / "launcher_setup.json"
        if not backup.is_file() or receipt.exists():
            raise ValueError('install record rollback is unavailable')
        publish_file(backup, receipt, operation="restore_install_record")
        return receipt

    def _game_diagnostic(self, checks):
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        root: Path | None = None
        if game_root:
            try:
                root = validate_game_root(Path(str(game_root)))
                checks.append(Diagnostic("game", "ok", 'Game folder found.', {"path": str(root)}))
            except ValueError as error:
                checks.append(Diagnostic("game", "invalid", str(error)))
        else:
            game_discovery = self.config.get("game_discovery")
            details: dict[str, object] | None = None
            if isinstance(game_discovery, dict):
                details = {"discovery": game_discovery}
            checks.append(Diagnostic("game", "missing", 'Choose the folder containing DOOMEternalx64vk.exe.', details))

        return root

    def _admission_diagnostic(self, support_diagnostics, processes, checks):
        health = support_diagnostics["game_link"]["native_rpc"].get("health")
        admission = health.get("session") if isinstance(health, Mapping) else None
        game_pids = {str(process.get("pid")) for process in processes
                     if str(process.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"}}
        if (isinstance(admission, Mapping) and admission.get("state") == "admission_refused"
                and str(admission.get("pid")) in game_pids):
            checks.append(Diagnostic("ap_session", "attention",
                'Save setup failed. Do not load the save yet. Select Help -> SAVE SUPPORT REPORT.',
                dict(admission)))

    def _linux_install_diagnostic(self, receipt, adapter_state, mode, checks, room_receipt_applied):
        try:
            verification = _verify_linux_receipt(receipt, self.config, application_dir=self._application_dir())
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            details = {
                'adapter_state': adapter_state, 'installation_mode': mode,
                'post_install_verification': receipt.get('post_install_verification', ''),
                'verification_error': str(error), **installation_error_details(error, 'verify'),
            }
            checks.append(Diagnostic("mod_injection", "failed", 'Installation failed at verify: {value_0}: {value_1}'.format(value_0=f'{type(error).__name__}', value_1=f'{error}'), details))
        else:
            details = {
                'adapter_state': adapter_state, 'installation_mode': mode,
                'post_install_verification': verification.get('state', ''),
                'required_resources': verification.get('required_resources', ()),
            }
            checks.append(Diagnostic("mod_injection", "ok", 'Mod installed and checked.', details))
            room_receipt_applied = True
        return room_receipt_applied

    def _windows_install_diagnostic(self, root, receipt, adapter_state, mode, checks, room_receipt_applied):
        from .campaign_resources import verify_installed_resources
        try:
            if root is None:
                raise ValueError('Game files could not be found. Verify the game in Steam, then retry setup.')
            verify_installed_resources(root, receipt)
        except (OSError, ValueError) as error:
            for key in ("mod_injection", "windows_mod_installer"):
                checks.append(Diagnostic(key, "failed", str(error), {"adapter_state": adapter_state}))
        else:
            checks.append(Diagnostic("mod_injection", "ok", 'Game files checked.', {"adapter_state": adapter_state, "installation_mode": mode}))
            checks.append(Diagnostic("windows_mod_installer", "ok", 'Mod installed and checked.', {"installation_mode": mode, "adapter_state": adapter_state}))
            room_receipt_applied = True
        return room_receipt_applied

    def _applied_receipt_diagnostic(self, receipt, adapter_state, mode, root, checks, room_receipt_applied):
        if os.name != "nt":
            room_receipt_applied = self._linux_install_diagnostic(receipt, adapter_state, mode, checks, room_receipt_applied)
        else:
            room_receipt_applied = self._windows_install_diagnostic(root, receipt, adapter_state, mode, checks, room_receipt_applied)
        return room_receipt_applied

    def _receipt_adapter_diagnostic(self, adapter_state, receipt, mode, root, checks, room_receipt_applied):
        if adapter_state == "applied":
            room_receipt_applied = self._applied_receipt_diagnostic(receipt, adapter_state, mode, root, checks, room_receipt_applied)
        elif adapter_state in UNINSTALL_OWNED_STATES:
            details = {"adapter_state": adapter_state, "installation_mode": mode}
            if adapter_state == "uninstalled":
                status = "not_applicable"
                message = 'Room mod is not installed.'
            else:
                status = "attention"
                message = 'Removal failed. Retry uninstall or select Help -> SAVE SUPPORT REPORT.'
            checks.append(Diagnostic("mod_injection", status, message, details))
            if os.name == "nt":
                checks.append(Diagnostic("windows_mod_installer", status, message, details))
        elif adapter_state == "manual_install_required":
            checks.append(Diagnostic("mod_injection", "failed", 'Follow Windows manual installation in INSTALL.md.', {"adapter_state": adapter_state, "installation_mode": mode}))
            if os.name == "nt":
                checks.append(Diagnostic("windows_mod_installer", "failed", 'Follow Windows manual installation in INSTALL.md.', {"installation_mode": mode, "adapter_state": adapter_state}))
        else:
            details = {'adapter_state': adapter_state, 'installation_mode': mode, **receipt.get('adapter_details', {})}
            message = _installation_receipt_message(receipt)
            checks.append(Diagnostic("mod_injection", "failed", message, details))
            if os.name == "nt":
                checks.append(Diagnostic("windows_mod_installer", "failed", 'Installer is not ready. Retry setup.', {"installation_mode": mode, "adapter_state": adapter_state}))
        return room_receipt_applied

    def _receipt_diagnostic(self, receipt_path, root, checks, room_uninstall_state, room_receipt_applied):
        if receipt_path and receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                adapter_state = str(receipt.get("adapter_state", ""))
                room_uninstall_state = adapter_state if adapter_state in UNINSTALL_OWNED_STATES else ""
                mode = str(receipt.get("installation_mode", ""))
                room_receipt_applied = self._receipt_adapter_diagnostic(adapter_state, receipt, mode, root, checks, room_receipt_applied)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                checks.append(Diagnostic("mod_injection", "invalid", 'Saved installation details could not be read. Select Fix Setup.'))
                if os.name == "nt":
                    checks.append(Diagnostic("windows_mod_installer", "invalid", 'Saved installation details could not be read. Select Fix Setup.'))
        else:
            checks.append(Diagnostic("mod_injection", "not_applicable", 'Installation details are missing. Select Check Setup.'))
            if os.name == "nt":
                checks.append(Diagnostic("windows_mod_installer", "not_applicable", 'Installation details are missing. Select Check Setup.'))
        return room_uninstall_state, room_receipt_applied

    def _installation_diagnostics(self, root, checks):
        state_dir = self._state_dir()
        receipt_path = (state_dir / "launcher_setup.json") if state_dir else None
        room_uninstall_state = ""
        room_receipt_applied = False
        room_uninstall_state, room_receipt_applied = self._receipt_diagnostic(receipt_path, root, checks, room_uninstall_state, room_receipt_applied)

        return room_uninstall_state, room_receipt_applied, receipt_path

    def _environment_checks(self, checks, root, meathook_probe):
        try:
            processes = list(detect_doom_processes())
            checks.append(Diagnostic("processes", "ok", 'process probe complete', {"items": processes}))
        except ProcessInspectionError as error:
            processes = []
            checks.append(Diagnostic("processes", "attention", str(error)))
        checks.append(Diagnostic("config", "ok", 'launcher configuration loaded', {"keys": sorted(self.config)}))
        checks.append(Diagnostic(
            'runtime_paths', 'ok', 'effective runtime paths and path-selection evidence collected',
            _runtime_diagnostics(self.config, self.config_path, self.paths, processes, app_dir=self._application_dir()),
        ))
        runtime_details = checks[-1].details if isinstance(checks[-1].details, Mapping) else {}
        support_diagnostics = build_support_diagnostics(
            self.config, self.config_path, self.paths, processes, runtime_details,
            {'status': meathook_probe.status.value, **dict(meathook_probe.details)}, live=self.live_support,
        )
        self._admission_diagnostic(support_diagnostics, processes, checks)

        room_uninstall_state, room_receipt_applied, receipt_path = self._installation_diagnostics(root, checks)
        if os.name != "nt":
            checks.append(Diagnostic("windows_mod_installer", "not_applicable", 'Windows-only diagnostic'))

        return support_diagnostics, room_uninstall_state, room_receipt_applied, receipt_path

    def _room_repair_actions(self, checks):
        try:
            actions = self.repair_actions()
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            actions = ()
            checks.append(Diagnostic(
                "room_mod",
                "invalid",
                'room repair inspection is unavailable: {value_0}: {value_1}'.format(value_0=f'{type(error).__name__}', value_1=f'{error}'),
            ))
        room_actions = tuple(
            action for action in actions
            if action.action_id in {
                'archive_stale_install_record', 'reinstall_room_mod', 'rebuild_room_package', 'update_room_package',
            }
        )
        return room_actions

    def _room_repair_diagnostic(self, checks, room_uninstall_state, room_receipt_applied, receipt_path):
        room_actions = self._room_repair_actions(checks)
        if room_actions:
            checks.append(Diagnostic(
                'room_mod', 'invalid', 'Room mod needs repair. Select Fix Setup.',
                {'actions': [asdict(action) for action in room_actions]},
            ))
        elif room_uninstall_state:
            checks.append(Diagnostic(
                "room_mod",
                "not_applicable" if room_uninstall_state == "uninstalled" else "attention",
                (
                    'Room package is intentionally uninstalled'
                    if room_uninstall_state == "uninstalled"
                    else 'Room package uninstall requires attention; automatic reinstall is disabled'
                ),
                {"adapter_state": room_uninstall_state},
            ))
        elif room_receipt_applied:
            checks.append(Diagnostic(
                'room_mod', 'ok', 'Room mod checked.',
                {'adapter_state': 'applied', 'receipt': str(receipt_path) if receipt_path else None},
            ))
        else:
            checks.append(Diagnostic("room_mod", "not_applicable", 'Installation could not be checked. Select Check Setup.'))

    def _overall_report_status(self, support_diagnostics, checks):
        overall = support_diagnostics.get("game_link", {}).get("overall", {})
        overall_status = str(overall.get("status", "ready")) if isinstance(overall, Mapping) else "ready"
        overall_reason = str(overall.get("reason", "")) if isinstance(overall, Mapping) else ""
        checks_ok = all(
            item.status not in {"error", "invalid", "missing", "incompatible", "failed", "attention"}
            for item in checks
        )
        if not checks_ok or overall_status == "attention":
            summary_status = "needs_attention"
            summary_message = overall_reason if checks_ok else ""
        elif overall_status == "degraded":
            summary_status = "degraded"
            summary_message = overall_reason
        else:
            summary_status = "ready"
            summary_message = ""
        return summary_status, summary_message

    def _report_summary(self, checks, support_diagnostics):
        issue = self._current_issue()
        if issue is not None:
            domain = str(issue.get("failure_domain", "room_package"))
            checks.append(Diagnostic(
                "current_room_package",
                "attention",
                str(issue.get("user_message") or issue.get("technical_message") or 'Room mod needs repair. Select Fix Setup.'),
                dict(issue),
            ))
            summary_status = "room_package_needs_attention"
            summary_message = str(issue.get("user_message") or 'Room mod needs repair. Select Fix Setup.')
            recovery_action = str(issue.get("recovery_action") or "rebuild_room_package")
        else:
            domain = ""
            recovery_action = ""
            summary_status, summary_message = self._overall_report_status(support_diagnostics, checks)
        return DoctorReport(
            self.VERSION, tuple(checks), summary_status, domain, recovery_action, summary_message, support_diagnostics,
        )

    def run(self) -> DoctorReport:
        checks: list[Diagnostic] = []
        platform_name = 'windows' if os.name == "nt" else 'linux'
        checks.append(Diagnostic("platform", "ok", platform_name))
        root = self._game_diagnostic(checks)
        meathook_probe = probe_core(root)
        checks.append(Diagnostic(
            'meathook', meathook_probe.status.value, meathook_probe.message, meathook_probe.details,
        ))

        support_diagnostics, room_uninstall_state, room_receipt_applied, receipt_path = self._environment_checks(checks, root, meathook_probe)
        self._room_repair_diagnostic(checks, room_uninstall_state, room_receipt_applied, receipt_path)
        return self._report_summary(checks, support_diagnostics)
