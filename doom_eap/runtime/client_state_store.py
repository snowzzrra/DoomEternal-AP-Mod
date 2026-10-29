"""Serialization and commit owner for the existing client-state document."""

import copy
import json
import os
import shutil
import time
import uuid

from doom_eap.runtime.item_reconciliation import project_receipt_boundary


class ClientStateLoadError(RuntimeError):
    pass


def atomic_write_json(path, state, *, indent=2, sort_keys=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(state, file, indent=indent, sort_keys=sort_keys)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def client_state_metrics(state):
    sessions = state.get("sessions", {}) if isinstance(state, dict) else {}
    if not isinstance(sessions, dict):
        return {"session_count": 0, "processed_count": 0, "receipt_count": 0}
    processed_count = 0
    receipt_count = 0
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        processed = session.get("processed_items")
        if isinstance(processed, int) and not isinstance(processed, bool) and processed >= 0:
            processed_count += processed
        history = session.get("receipt_history")
        if isinstance(history, dict):
            receipt_ids = history.get("receipt_ids")
            if isinstance(receipt_ids, list):
                receipt_count += len(receipt_ids)
    return {
        "session_count": len(sessions),
        "processed_count": processed_count,
        "receipt_count": receipt_count,
    }


class ClientStateStore:
    def __init__(self, path, *, version, migrate, log_event, logger, observer=lambda *args, **kwargs: None):
        self.path = path
        self.version = version
        self.migrate = migrate
        self.log_event = log_event
        self.logger = logger
        self._load_error = None
        self._observe = observer

    def load(self):
        if self._load_error is not None:
            raise self._load_error
        empty_state = {"version": self.version, "sessions": {}}
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.log_event(
                "ITEM_STATE_LOAD", path=str(self.path.resolve()), status="missing",
                boundary_before=0, boundary_after=0, success=True,
                **client_state_metrics(empty_state),
            )
            return empty_state
        except (OSError, UnicodeError) as error:
            self._block_load(error)
        try:
            raw_state = json.loads(raw_text)
            if isinstance(raw_state, dict) and isinstance(raw_state.get("sessions"), dict):
                for session in raw_state["sessions"].values():
                    if not isinstance(session, dict):
                        raise ValueError("client state session must be an object")
                    boundary = session.get("processed_items", 0)
                    if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
                        raise ValueError("client state receipt boundary is invalid")
            state, migrated = self.migrate(raw_state)
            metrics = client_state_metrics(state)
            self.log_event(
                "ITEM_STATE_LOAD", path=str(self.path.resolve()),
                status="migrated" if migrated else "loaded", version=state.get("version"),
                boundary_before=0, boundary_after=metrics["processed_count"], success=True,
                **metrics,
            )
            if migrated:
                self.log_event(
                    "ITEM_STATE_MIGRATION", path=str(self.path.resolve()),
                    reason="state_schema_migration", boundary_before=0,
                    boundary_after=metrics["processed_count"], success=True, **metrics,
                )
                self.logger.info(
                    "[State] STATE_MIGRATED from=1 to=%s sessions=%s",
                    self.version, len(state["sessions"]),
                )
                self.commit(state, reason="state_migration")
            return state
        except (OSError, ValueError, TypeError) as error:
            self._block_load(error)

    def _block_load(self, error):
        quarantine = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
        try:
            shutil.copy2(self.path, quarantine)
            preservation = f"Original retained; quarantine copy: {quarantine}"
        except OSError as copy_error:
            preservation = f"Original retained at {self.path}; quarantine copy failed: {copy_error}"
        message = (f"Item delivery blocked: cannot load client state {self.path}: {error}. "
                   f"{preservation}. Restore readable state and restart the bridge.")
        self._load_error = ClientStateLoadError(message)
        self.logger.error("[State] %s", message)
        self.log_event("ITEM_STATE_LOAD", path=str(self.path), status="blocked",
                       reason=str(error), success=False, quarantine=str(quarantine))
        raise self._load_error from error

    def commit(self, state, *, reason="state_update", boundary=None, boundary_before=None):
        if self._load_error is not None:
            raise self._load_error
        if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
            boundaries = []
            sessions = state.get("sessions", {}) if isinstance(state, dict) else {}
            if isinstance(sessions, dict):
                for session in sessions.values():
                    candidate = session.get("processed_items") if isinstance(session, dict) else None
                    if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                        boundaries.append(candidate)
            boundary = max(boundaries, default=0)
        if (
            isinstance(boundary_before, bool)
            or not isinstance(boundary_before, int)
            or boundary_before < 0
        ):
            boundary_before = boundary
        metrics = client_state_metrics(state)
        try:
            atomic_write_json(self.path, state)
        except Exception as error:
            self._observe("legacy_commit", state=state, reason=reason, boundary=boundary, success=False)
            self.log_event(
                "ITEM_STATE_SAVE", path=str(self.path.resolve()), reason=reason,
                boundary_before=boundary_before, boundary_after=boundary,
                success=False, error=str(error), **metrics,
            )
            raise
        self._observe("legacy_commit", state=state, reason=reason, boundary=boundary, success=True)
        self.log_event(
            "ITEM_STATE_SAVE", path=str(self.path.resolve()), reason=reason,
            boundary_before=boundary_before, boundary_after=boundary, success=True,
            **metrics,
        )

    def commit_session(self, document, session, *, processed_items, cultist_autosave_path,
                       received_deathlink_event_ids, save_slot_observations):
        original_session = session
        session = dict(session)
        session["receipt_history"] = copy.deepcopy(session.get("receipt_history"))
        project_receipt_boundary(session, processed_items)
        session["cultist_autosave_path"] = cultist_autosave_path
        session.pop("deathlinked", None)
        session["received_deathlink_event_ids"] = sorted(received_deathlink_event_ids)[-64:]
        session["save_slot_observations"] = save_slot_observations
        session.pop("automap_cleanup", None)
        session.pop("fast_travel_delivered", None)
        session.pop("sticky_mastery_observed", None)
        session.pop("weapon_masteries_observed", None)
        candidate = dict(document, sessions={
            key: session if value is original_session else value
            for key, value in document["sessions"].items()
        })
        self.commit(candidate, reason="session_persist", boundary=processed_items)
        for key in original_session.keys() - session.keys():
            original_session.pop(key)
        original_session.update(session)
