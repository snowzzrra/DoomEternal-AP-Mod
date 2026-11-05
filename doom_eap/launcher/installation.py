"""Installation sequencing and recovery policy; platform actions own tools and saves."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from doom_eap.launcher.ui.strings import text as player_text

from .install_journal import InstallationRecoveryRequired, InstallJournal, InstallPhase, file_digest
from .installation_adapters import (
    InjectorState,
    record_injector_settings,
    recover_external_execution,
    require_injector_stopped,
)
from .launcher_core_install import game_write_lock, recover_interrupted
from .launcher_platform import AdapterResult, recover_injector_resources, require_doom_closed


@dataclass(frozen=True)
class InstallationPlan:
    game_root: Path
    generated: Path
    receipt_path: Path
    config_path: Path
    state_dir: Path
    identity: dict

    def journal(self) -> InstallJournal:
        return InstallJournal(self.game_root, (self.receipt_path, self.config_path))


@dataclass(frozen=True)
class InstallationOutcome:
    adapter: AdapterResult
    steam_option: str = ""
    steam_diff: str = ""


@dataclass(frozen=True)
class InstallationActions:
    prepare: Callable[[InstallJournal], None]
    inject: Callable[[], InstallationOutcome]
    confirm: Callable[[], bool]
    finalize: Callable[[InstallationOutcome], InstallationOutcome]
    verify: Callable[[InstallationOutcome], InstallationOutcome]
    session: Callable[[InstallJournal], None]
    receipt: Callable[[InstallationOutcome], dict]
    check: Callable[[], None]


def pending_outcome(outcome: InstallationOutcome) -> InstallationOutcome:
    result = outcome.adapter
    return InstallationOutcome(
        AdapterResult(
            state="manual_install_required",
            message=result.message,
            command=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            details={**result.details, "user_confirmed": False},
        ),
        outcome.steam_option,
        outcome.steam_diff,
    )


class InstallationService:
    def execute(
        self,
        plan: InstallationPlan,
        actions: InstallationActions,
        *,
        manual: bool = False,
        transaction_id: str | None = None,
        retry: bool = False,
    ) -> InstallationOutcome:
        with game_write_lock(plan.game_root):
            require_doom_closed(player_text('launcher.installation', 'installing_a_room_mod'))
            journal = plan.journal()
            self._check_inputs(plan)
            if self._open(plan, journal, manual, transaction_id, retry):
                self._verify_resources(plan, journal)
                outcome = actions.verify(self._outcome(journal))
                if outcome.adapter.state == "applied":
                    self._cleanup(plan, journal)
                return outcome
            try:
                return self._advance(plan, journal, actions, manual, retry)
            except Exception as error:
                self._record_failure(journal, error)
                raise

    def _open(self, plan, journal, manual, transaction_id, retry):
        exists = journal.load()
        self._check_confirmation(journal, exists, manual, transaction_id)
        self._check_identity(plan, journal, exists)
        if self._committed(plan, journal, exists, manual, retry):
            return True
        if exists and not self._resolve_external(journal, retry):
            self._recover_local(plan, journal)
        if not exists or journal.phase in {InstallPhase.ROLLED_BACK, InstallPhase.COMPLETE}:
            if manual:
                raise InstallationRecoveryRequired('installation is not awaiting confirmation')
            if exists:
                try:
                    journal.cleanup()
                except (OSError, InstallationRecoveryRequired):
                    journal.archive()
            journal.begin(plan.identity)
        return False

    @staticmethod
    def _committed(plan, journal, exists, manual, retry):
        return (
            exists
            and not manual
            and not retry
            and journal.phase is InstallPhase.COMPLETE
            and journal.document["identity"] == plan.identity
        )

    @staticmethod
    def _check_identity(plan, journal, exists):
        if not exists or journal.phase in {InstallPhase.COMPLETE, InstallPhase.ROLLED_BACK}:
            return
        if journal.document["identity"] != plan.identity:
            raise InstallationRecoveryRequired('installation belongs to another room, package or executable')

    @staticmethod
    def _check_confirmation(journal, exists, manual, transaction_id):
        if manual and (not exists or transaction_id != journal.transaction_id):
            raise InstallationRecoveryRequired('stale installation confirmation')

    @staticmethod
    def _resolve_external(journal, retry):
        if journal.document["evidence"].get("recovery_phase") == InstallPhase.ROLLING_BACK.value:
            return
        if journal.phase not in {InstallPhase.INJECTING, InstallPhase.RECOVERY_REQUIRED}:
            return
        external = journal.phase is InstallPhase.INJECTING or journal.document["evidence"].get("external_uncertain")
        if not external:
            raise InstallationRecoveryRequired('external execution or recovery needs explicit inspection')
        if not retry:
            raise InstallationRecoveryRequired('injector result is unknown; re-run installation')
        recover_external_execution(journal)
        journal.transition(InstallPhase.PREPARED, external_uncertain=False)
        return True

    @staticmethod
    def _recover_local(plan, journal):
        local_recovery = journal.document["evidence"].get("recovery_phase") == InstallPhase.ROLLING_BACK.value
        if local_recovery or journal.phase in {
            InstallPhase.PREPARING,
            InstallPhase.PREPARED,
            InstallPhase.ROLLING_BACK,
        }:
            require_injector_stopped(journal)
            recover_interrupted(plan.game_root, journal.document["evidence"].get("core_state_dir", plan.state_dir))
            recover_injector_resources(plan.game_root)
            journal.rollback()

    @staticmethod
    def _check_inputs(plan):
        expected = plan.identity
        if file_digest(plan.game_root / "DOOMEternalx64vk.exe") != expected["executable_sha256"]:
            raise InstallationRecoveryRequired('game executable changed')
        if file_digest(plan.generated) != expected["generated_sha256"]:
            raise InstallationRecoveryRequired('generated package changed')

    def _advance(self, plan, journal, actions, manual, retry):
        if journal.phase is InstallPhase.PREPARING:
            actions.check()
            require_injector_stopped(journal)
            actions.prepare(journal)
            journal.transition(InstallPhase.PREPARED)
        if journal.phase is InstallPhase.PREPARED:
            actions.check()
            require_injector_stopped(journal)
            journal.document["evidence"].pop("injector_outcome", None)
            journal.transition(InstallPhase.INJECTING, injector_state="unknown", injector_finished=False)
            outcome = actions.inject()
            journal.load()
            self._record_injector_outcome(journal, outcome)
            journal.transition(
            InstallPhase.AWAITING_CONFIRMATION, outcome=asdict(outcome), injector_outcome=asdict(outcome)
        )
        else:
            outcome = self._outcome(journal)
        if journal.phase is InstallPhase.AWAITING_CONFIRMATION:
            outcome = self._retry_injector(journal, actions, outcome, retry)
            actions.check()
            if outcome.adapter.details.get("recovery_required"):
                journal.transition(InstallPhase.RECOVERY_REQUIRED, external_uncertain=True)
                raise InstallationRecoveryRequired('injector may still be running')
            if not self._confirmed(outcome, actions, manual):
                return pending_outcome(outcome)
            if manual:
                require_injector_stopped(journal)
            outcome = self._confirmed_outcome(outcome, manual)
            journal.transition(InstallPhase.CONFIRMED, outcome=asdict(outcome), injector_outcome=asdict(outcome))
        return self._finish(plan, journal, actions, outcome)

    @staticmethod
    def _retry_injector(journal, actions, outcome, retry):
        if not retry or outcome.adapter.state == "applied" or outcome.adapter.details.get("recovery_required"):
            return outcome
        require_injector_stopped(journal)
        actions.check()
        journal.document["evidence"].pop("injector_outcome", None)
        journal.transition(InstallPhase.INJECTING, injector_state="unknown", injector_finished=False)
        outcome = actions.inject()
        journal.load()
        InstallationService._record_injector_outcome(journal, outcome)
        journal.transition(
            InstallPhase.AWAITING_CONFIRMATION, outcome=asdict(outcome), injector_outcome=asdict(outcome)
        )
        return outcome

    @staticmethod
    def _record_injector_outcome(journal, outcome):
        if not outcome.adapter.details.get("recovery_required"):
            require_injector_stopped(journal)
            record_injector_settings(journal)
        result = outcome.adapter
        state = (
            InjectorState.UNKNOWN if result.details.get("recovery_required") or result.returncode is None
            else InjectorState.FINISHED if result.returncode == 0 else InjectorState.FAILED
        )
        journal.document["evidence"].update(injector_state=state.value, injector_finished=state is not InjectorState.UNKNOWN)

    @staticmethod
    def _confirmed_outcome(outcome, manual):
        details = dict(outcome.adapter.details)
        if manual or details.get("installation_mode") == "windows_injector_assisted":
            details["user_confirmed"] = True
        if manual:
            details["installation_mode"] = "manual_fallback"
        return replace(outcome, adapter=replace(outcome.adapter, state="applied", details=details))

    @staticmethod
    def _confirmed(outcome, actions, manual):
        if manual:
            return True
        if outcome.adapter.state != "applied":
            return False
        if outcome.adapter.details.get("installation_mode") == "windows_injector_assisted":
            return actions.confirm()
        return True

    def _finish(self, plan, journal, actions, outcome):
        if journal.phase is InstallPhase.PREPARING_SESSION:
            self._verify_resources(plan, journal)
            outcome = actions.verify(outcome)
            if outcome.adapter.state != "applied":
                journal.transition(InstallPhase.VERIFYING, outcome=asdict(outcome))
                return outcome
        if journal.phase in {InstallPhase.CONFIRMED, InstallPhase.FINALIZING_RESOURCES}:
            actions.check()
            journal.transition(InstallPhase.FINALIZING_RESOURCES)
            outcome = actions.finalize(self._outcome(journal, "injector_outcome"))
            journal.load()
            if outcome.adapter.state != "applied":
                journal.transition(InstallPhase.FINALIZING_RESOURCES, outcome=asdict(outcome))
                return outcome
            journal.transition(InstallPhase.VERIFYING, outcome=asdict(outcome), resources_outcome=asdict(outcome))
        if journal.phase is InstallPhase.VERIFYING:
            actions.check()
            self._verify_resources(plan, journal)
            outcome = actions.verify(self._outcome(journal, "resources_outcome"))
            if outcome.adapter.state != "applied":
                journal.transition(InstallPhase.VERIFYING, outcome=asdict(outcome))
                return outcome
            journal.transition(InstallPhase.PREPARING_SESSION, outcome=asdict(outcome))
        actions.check()
        actions.session(journal)
        actions.check()
        payload = actions.receipt(outcome)
        journal.replace(
            plan.receipt_path,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
            operation="setup_receipt_publish",
        )
        journal.transition(InstallPhase.COMPLETE)
        self._cleanup(plan, journal)
        return outcome

    @staticmethod
    def _verify_resources(plan, journal):
        from .campaign_resources import verify_campaign_resources, verify_installed_resources

        resources = journal.document["evidence"].get("resource_plan")
        if resources is not None:
            verify_campaign_resources(plan.game_root, resources)
        details = journal.document["evidence"]["outcome"]["adapter"]["details"]
        if details.get("installed_resources"):
            verify_installed_resources(plan.game_root, details)

    @staticmethod
    def _cleanup(plan, journal):
        try:
            from .campaign_resources import _cleanup_resource_banks, cleanup_resource_staging

            resources = journal.document["evidence"].get("resource_plan")
            if resources is not None:
                cleanup = _cleanup_resource_banks(plan.game_root / "base", resources["obsolete"])
                journal.transition(InstallPhase.COMPLETE, bank_cleanup=cleanup)
                if cleanup["errors"]:
                    raise OSError("; ".join(cleanup["errors"]))
                cleanup_resource_staging(journal, resources)
            journal.cleanup()
            journal.document["evidence"].pop("cleanup_error", None)
            journal.save()
        except (OSError, InstallationRecoveryRequired) as error:
            journal.transition(InstallPhase.COMPLETE, cleanup_error=str(error))

    @staticmethod
    def _outcome(journal, key="outcome"):
        value = journal.document["evidence"][key]
        adapter = dict(value["adapter"])
        adapter["command"] = tuple(adapter["command"])
        return InstallationOutcome(AdapterResult(**adapter), value["steam_option"], value["steam_diff"])

    @staticmethod
    def _record_failure(journal, error):
        journal.load()
        journal.document["failure"] = {
            "code": getattr(error, "code", type(error).__name__),
            "phase": journal.phase.value,
            "details": getattr(error, "details", {}),
            "message": str(error),
        }
        journal.save()
        if journal.phase in {InstallPhase.PREPARING, InstallPhase.PREPARED}:
            try:
                require_doom_closed('recovering a room installation')
                require_injector_stopped(journal)
                journal.rollback()
            except (OSError, RuntimeError) as recovery_error:
                journal.transition(
                    InstallPhase.RECOVERY_REQUIRED,
                    recovery_error=getattr(recovery_error, "details", {}).get("recovery_error", str(recovery_error)),
                    recovery_phase=InstallPhase.ROLLING_BACK.value,
                )
                raise
