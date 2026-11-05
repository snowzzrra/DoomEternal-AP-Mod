"""Launcher language and text formatting; game language selection stays separate."""

from doom_eap.content.option_text import ENEMY_ATTRIBUTES, option_catalog_key, option_choice_key, translated_choice
from doom_eap.strings import PlayerError, load_catalog

_locale = "en"


def selected_language(saved: object, system: str) -> str:
    languages = load_catalog().languages
    if isinstance(saved, str) and saved in languages:
        return saved
    system = system.replace("_", "-").casefold()
    codes = {code.casefold(): code for code in languages}
    return codes.get(system, codes.get(system.split("-")[0], "en"))


def configure_language(saved: object, system: str) -> str:
    global _locale
    _locale = selected_language(saved, system)
    return _locale


def text(category: str, key: str, **values: str | int) -> str:
    return load_catalog().text(category, key, locale=_locale, **values)


def failure_text(operation: str, error: object) -> str:
    if isinstance(error, PlayerError):
        return text(error.category, error.key, **error.values)
    return text("launcher.failures", operation)


def record_failure(controller, operation, error) -> str:
    technical = (error.get("technical_message") or error.get("raw_message") or error.get("message", "")
                 if isinstance(error, dict) else str(error))
    controller.last_operation_failure = {"operation": operation, "technical_message": str(technical)}
    return str(technical)


def option_text(option_key: str, field: str, fallback: str, *, choice_key: object = None) -> str:
    if field == "choice":
        if option_key in ENEMY_ATTRIBUTES:
            return enemy_attribute_label(fallback, str(choice_key))
        if not translated_choice(option_key, choice_key):
            return fallback
    key = option_choice_key(option_key, choice_key) if field == "choice" else f"{option_key}.{field}"
    category, name = option_catalog_key(key).rsplit(".", 1)
    return text(category, name)


def enemy_attribute_label(schema_label: str, choice_key: str) -> str:
    bound = choice_key.rsplit(".", 1)[-1]
    limit = text("options.enemy_limit", bound)
    if "." not in choice_key:
        return limit
    name = schema_label.split(" — ", 1)[0]
    return name + text("options.enemy_limit", "separator") + limit


def checkpoint_blocked(reason):
    return text("launcher.save", "unverified", reason=reason)


def injector_recovery(running, *, windows):
    key = "injector_running" if running else "injector_unknown"
    return text("launcher.setup", key if windows else key + "_linux")


def installer_instruction(*, windows):
    return text("launcher.setup", "action_finish_install" if windows else "action_wait")
