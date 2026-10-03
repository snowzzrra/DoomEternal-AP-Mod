"""Serialization and commit owner for the existing client-state document."""

import json
import os
import time
import uuid

from doom_eap.runtime.item_reconciliation import project_receipt_boundary


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
    def __init__(self, path, *, version, migrate, log_event, logger):
        self.path = path
        self.version = version
        self.migrate = migrate
        self.log_event = log_event
        self.logger = logger

    def load(self):
        empty_state = {"version": self.version, "sessions": {}}
        if not self.path.is_file():
            self.log_event(
                "ITEM_STATE_LOAD", path=str(self.path.resolve()), status="missing",
                boundary_before=0, boundary_after=0, success=True,
                **client_state_metrics(empty_state),
            )
            return empty_state
        try:
            raw_state = json.loads(self.path.read_text(encoding="utf-8"))
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
                try:
                    self.commit(state, reason="state_migration")
                except OSError as error:
                    self.logger.warning("[State] Could not persist migrated state: %s", error)
            return state
        except Exception as error:
            quarantine = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
            try:
                os.replace(self.path, quarantine)
            except OSError:
                pass
            self.logger.warning(f"[State] Invalid state file quarantined: {error}")
            self.log_event(
                "ITEM_STATE_LOAD", path=str(self.path.resolve()),
                status="invalid_quarantined", reason=str(error), boundary_before=0,
                boundary_after=0, success=False, **client_state_metrics(empty_state),
            )
            return empty_state

    def commit(self, state, *, reason="state_update", boundary=None, boundary_before=None):
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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            with temporary.open("x", encoding="utf-8", newline="\n") as file:
                json.dump(state, file, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except Exception as error:
            self.log_event(
                "ITEM_STATE_SAVE", path=str(self.path.resolve()), reason=reason,
                boundary_before=boundary_before, boundary_after=boundary,
                success=False, error=str(error), **metrics,
            )
            raise
        self.log_event(
            "ITEM_STATE_SAVE", path=str(self.path.resolve()), reason=reason,
            boundary_before=boundary_before, boundary_after=boundary, success=True,
            **metrics,
        )

    def commit_session(self, document, session, *, processed_items, cultist_autosave_path,
                       received_deathlink_event_ids, save_slot_observations):
        project_receipt_boundary(session, processed_items)
        session["cultist_autosave_path"] = cultist_autosave_path
        session.pop("deathlinked", None)
        session["received_deathlink_event_ids"] = sorted(received_deathlink_event_ids)[-64:]
        session["save_slot_observations"] = save_slot_observations
        session.pop("automap_cleanup", None)
        session.pop("fast_travel_delivered", None)
        session.pop("sticky_mastery_observed", None)
        session.pop("weapon_masteries_observed", None)
        self.commit(document, reason="session_persist", boundary=processed_items)
