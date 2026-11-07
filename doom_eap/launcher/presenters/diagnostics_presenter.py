"""Reduce diagnostic codes into Help text; no Qt or filesystem access."""

from dataclasses import dataclass

from ..ui.strings import failure_text, text


@dataclass(frozen=True)
class DiagnosticsModel:
    title: str
    color: str
    summary: str
    action: str
    technical_lines: tuple[str, ...]
    can_fix: bool = False


class DiagnosticsPresenter:
    @staticmethod
    def group(key):
        if "save" in key or "queue" in key:
            return "saved_games"
        if any(token in key for token in ("mod", "inject", "receipt")):
            return "room_mod"
        if any(token in key for token in ("native", "xinput", "bridge", "runtime", "meathook")):
            return "game_connection"
        return "game"

    @staticmethod
    def state(statuses):
        bad = {"failed", "error", "missing", "invalid", "incompatible", "blocked", "attention"}
        if any(status in bad for status in statuses):
            return text("launcher.support", "needs_attention")
        if any(status in {"ok", "ready", "passed", "present", "applied"} for status in statuses):
            return text('common', 'ready')
        return (text('launcher.support', 'not_required') if statuses else text("launcher.support", "not_checked"))

    @classmethod
    def present(cls, report, *, failure, connected, room_issue):
        groups = {key: [] for key in ("game", "game_connection", "room_mod", "saved_games")}
        technical = []
        for item in report.get("diagnostics", []):
            key, status, message = item["key"], item["status"], item["message"].strip()
            technical.append(
                f"[{status.replace('_', ' ').upper()}] {key.replace('_', ' ')}" + (f"\n  {message}" if message else "")
            )
            groups[cls.group(key.casefold())].append(status.casefold())
        summary = cls.summary(groups, failure, connected)
        ready = bool(report.get("ok")) and not failure
        return DiagnosticsModel(
            (text('common', 'ready') if ready else text("launcher.support", "needs_attention")),
            "accent" if ready else "warn",
            "\n".join(summary),
            cls.action(failure, room_issue, ready),
            tuple(technical),
            not ready,
        )

    @staticmethod
    def room_status(state):
        if state == "ready":
            return text('common', 'ready'), "accent"
        if state in {"package_incompatible", "package_failed", "install_needed", "update_required", "failed"}:
            return text("launcher.support", "needs_attention"), "warn"
        return text("launcher.support", "not_checked"), "muted"

    @classmethod
    def summary(cls, groups, failure, connected):
        summary = [f"{text(*{'game': ('common', 'game_title'), 'room_mod': ('common', 'room_mod'), 'ap_save': ('common', 'ap_save')}.get(key, ('launcher.support', key)))}: {cls.state(statuses)}" for key, statuses in groups.items()]
        if failure:
            key = "ap_save" if failure.get("failure_domain") == "campaign_session" else "setup"
            summary.append(f"{text(*{'game': ('common', 'game_title'), 'room_mod': ('common', 'room_mod'), 'ap_save': ('common', 'ap_save')}.get(key, ('launcher.support', key)))}: {text("launcher.support", "needs_attention")}")
        summary.append(
            text(
                "launcher.support",
                "current_room",
                state=(text('common', 'connected') if connected else text('launcher.support', 'not_connected')),
            )
        )
        return summary

    @staticmethod
    def action(failure, room_issue, ready):
        if failure:
            operation = "saves" if failure.get("failure_domain") == "campaign_session" else "repair"
            return failure_text(operation, failure)
        if room_issue:
            return text("launcher.ui", "open_session_and_select_rebuild_room_package")
        return (text('launcher.ui', 'you_are_ready_to_play') if ready else text('launcher.ui', 'repair_retry_help'))
