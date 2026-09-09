"""Existing repair decisions with explicit Doctor/configuration/install adapters."""
from pathlib import Path
from .launcher_platform import GameLinkResult, validate_game_root, probe_meathook

def install_game_link(config, workflow, emit, force_repair: bool = False) -> GameLinkResult:
    """Acquire, verify, and install supported Game Link runtime library."""
    game_root = config.get("game_root") or config.get("doom_base_dir")
    if not game_root:
        raise RuntimeError("DOOM Eternal installation is not configured.")
    root = validate_game_root(Path(str(game_root)))
    local_key = "meathook_dll"
    local_value = config.get(local_key)
    local_artifact = Path(str(local_value)).expanduser() if local_value else None
    result = workflow.ensure_game_link(
        root,
        local_artifact=local_artifact,
        force_repair=force_repair,
    )
    emit(
        "game_link_status",
        state=result.state,
        message=result.message,
        path=result.path,
        sha256=result.sha256,
        ownership=result.ownership,
        backup_path=result.backup_path,
    )
    return result


def repair_idfile_decompressor(workflow, emit) -> str:
    installed = workflow.repair_idfile_decompressor()
    emit(
        "repair_complete",
        action="repair_idfile_decompressor",
        state="verified",
        path=installed.executable,
        sha256=installed.artifact_sha256,
    )
    return f"idFileDeCompressor cache repaired and verified: {installed.executable}"


def remove_idfile_decompressor(workflow, emit) -> str:
    removed = workflow.remove_idfile_decompressor()
    emit("repair_complete", action="remove_idfile_decompressor", removed=removed)
    return "idFileDeCompressor cache removed." if removed else "idFileDeCompressor cache was already absent."


def apply_repair(action_key: str, *, doctor, config, workflow, emit, start_room_setup) -> str:
    """Apply selected Doctor action. Room changes require connected-room setup."""
    actions = {action.key: action for action in doctor.repair_preview()}
    action = actions.get(action_key)
    if action is None:
        raise ValueError("repair action is unavailable")
    if action_key == "archive_stale_install_record":
        backup = doctor.archive_stale_install_record()
        emit("repair_complete", action=action_key, backup=str(backup))
        return str(backup)
    if action_key in {"rebuild_room_package", "update_room_package", "reinstall_room_mod"}:
        if not start_room_setup():
            raise RuntimeError("connect to room before rebuilding its room package")
        emit("repair_started", action=action_key)
        return "Room package rebuild started; installed hash will be checked after setup."
    if action_key in {"install_game_link", "repair_game_link"}:
        result = install_game_link(config, workflow, emit, force_repair=action_key == "repair_game_link")
        game_root = config.get("game_root") or config.get("doom_base_dir")
        root = validate_game_root(Path(str(game_root))) if game_root else None
        post_probe = probe_meathook(root)
        if not post_probe.ok:
            emit(
                "repair_failed",
                action=action_key,
                state=result.state,
                message=f"post-repair probe failed: {post_probe.message}",
            )
            raise RuntimeError(f"Game Link repair was not verified: {post_probe.message}")
        emit(
            "repair_complete",
            action=action_key,
            state="repaired",
            path=result.path,
            sha256=result.sha256,
            probe=post_probe.message,
        )
        return f"Game Link runtime repaired and verified: {post_probe.message}"
    if action_key == "repair_idfile_decompressor":
        return repair_idfile_decompressor(workflow, emit)
    if action_key == "remove_idfile_decompressor":
        return remove_idfile_decompressor(workflow, emit)
    raise ValueError("unsupported repair action")

