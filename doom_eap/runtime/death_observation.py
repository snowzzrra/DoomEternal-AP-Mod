"""Process-local death observations; save authority and transport stay outside."""
from types import MappingProxyType


class DeathObservation:
    def __init__(self, logger):
        self._logger = logger
        self._checkpoints = {}
        self._initialized = set()
        self._consumed_tokens = set()
        self._respawn_carryover = {}
        self._consumed_epoch = {}
        self._last_event = {}
        self._warning = None
        self._details_dead = self._details_mtime = self._details_path = None

    @property
    def checkpoints(self):
        return MappingProxyType(dict(self._checkpoints))

    @property
    def warning(self):
        return self._warning

    def probe_failed(self, error):
        warning = str(error)
        if warning != self._warning:
            self._logger.warning("[DeathLink] game_duration probe failed; using game.details fallback: %s", error)
            self._warning = warning

    def probe_succeeded(self):
        self._warning = None

    def observe_checkpoint(self, snapshot, selected, load_epoch, current_map):
        died = bool(snapshot.get("checkpoint_death", False))
        raw_deaths = int(snapshot.get("raw_num_checkpoint_deaths", 1 if died else 0))
        slot_directory = selected.slot_directory
        save_mtime_ns = selected.mtime_ns
        save_snapshot_token = f"{slot_directory}:{selected.path.name}:{save_mtime_ns}"
        event_identity = f"{save_snapshot_token}:deaths={raw_deaths}"

        self._logger.info(
            "[DeathLink] DEATH_DETECTOR_OBSERVATION slot=%s map=%s load_epoch=%s "
            "raw_num_checkpoint_deaths=%s save_snapshot_token=%s save_mtime_ns=%s",
            slot_directory, current_map, load_epoch, raw_deaths, save_snapshot_token, save_mtime_ns,
        )

        if slot_directory not in self._initialized:
            self._initialized.add(slot_directory)
            self._checkpoints[slot_directory] = died
            if died:
                self._consumed_tokens.add(save_snapshot_token)
                self._respawn_carryover[slot_directory] = True
                self._consumed_epoch[slot_directory] = load_epoch
                self._last_event[slot_directory] = event_identity
                self._logger.info(
                    "[DeathLink] DEATH_DETECTOR_BASELINE reason=session_start_preexisting_death"
                )
            else:
                self._respawn_carryover[slot_directory] = False
                self._logger.info(
                    "[DeathLink] DEATH_DETECTOR_BASELINE reason=initial_clean_baseline"
                )
            return None

        self._checkpoints[slot_directory] = died

        if not died:
            self._respawn_carryover[slot_directory] = False
            return None

        if save_snapshot_token in self._consumed_tokens:
            self._logger.info(
                "[DeathLink] DEATH_EVIDENCE_ALREADY_CONSUMED event_identity=%s",
                event_identity,
            )
            return None

        if (
            self._respawn_carryover.get(slot_directory)
            and load_epoch != self._consumed_epoch.get(slot_directory)
        ):
            self._consumed_tokens.add(save_snapshot_token)
            self._respawn_carryover[slot_directory] = False
            previous_death_event = (
                self._last_event.get(slot_directory)
                or "unknown"
            )
            self._logger.info(
                "[DeathLink] POST_DEATH_RESPAWN_CARRYOVER slot=%s previous_death_event=%s "
                "new_epoch=%s raw_num_checkpoint_deaths=%s snapshot=%s action=suppressed",
                slot_directory,
                previous_death_event,
                load_epoch,
                raw_deaths,
                save_snapshot_token,
            )
            return None

        self._consumed_tokens.add(save_snapshot_token)
        self._respawn_carryover[slot_directory] = True
        self._consumed_epoch[slot_directory] = load_epoch
        self._last_event[slot_directory] = event_identity
        return event_identity

    def observe_details(self, details):
        if not details:
            return False
        died = details.get("diedLastGame") == "1"
        mtime = details.get("_mtime_ns")
        details_path = details.get("_path")
        if self._details_dead is None:
            self._details_dead = died
            self._details_mtime = mtime
            self._details_path = details_path
            self._logger.info(
                f"[Save] Monitoring {details.get('_path')} for DeathLink."
            )
            return False

        if details_path != self._details_path:
            self._details_dead = died
            self._details_mtime = mtime
            self._details_path = details_path
            self._logger.info(
                "[Save] Active autosave changed; DeathLink baseline reset to "
                f"{details_path}."
            )
            return False

        changed = mtime != self._details_mtime
        transitioned_to_dead = changed and died and not self._details_dead
        self._details_dead = died
        self._details_mtime = mtime

        return transitioned_to_dead

