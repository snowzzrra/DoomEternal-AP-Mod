"""Launcher language and text formatting; game language selection stays separate."""

from doom_eap.strings import load_catalog

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


def checkpoint_blocked(reason):
    return text("launcher.save", "unverified", reason=reason)


def injector_recovery(running, *, windows):
    key = "injector_running" if running else "injector_unknown"
    return text("launcher.setup", key if windows else key + "_linux")


def installer_instruction(*, windows):
    return text("launcher.setup", "action_finish_install" if windows else "action_wait")
