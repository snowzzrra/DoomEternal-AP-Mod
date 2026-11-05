"""Project key conflicts without changing saved preferences."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingsModel:
    conflict: bool


class SettingsPresenter:
    @staticmethod
    def present(refill: str, special: str) -> SettingsModel:
        return SettingsModel(bool(refill) and refill == special)
