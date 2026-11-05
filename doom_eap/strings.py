"""Load player text and validate translations before use or projection."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from string import Formatter
from types import MappingProxyType

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "strings"
GAME_LOCALES = MappingProxyType({"english": "en", "portuguese": "pt-BR"})
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_FLAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_PARAMETER = re.compile(r"[a-z][a-z0-9_]*")
# native menu fields include a terminating NUL; remote values are checked at serialization
_NATIVE_FIELDS = {
    "notifications.fortress_title": ("ascii", 96),
    "notifications.reward_unknown": ("utf-8", 512),
    "notifications.local_item": ("utf-8", 512),
    "notifications.remote_item": ("utf-8", 512),
    "notifications.trap_sentence": ("utf-8", 512),
}
DUPLICATE_EXCEPTIONS: dict[frozenset[str], str] = {}


class CatalogError(ValueError):
    pass


class PlayerError(ValueError, RuntimeError):
    """An error with catalog text suitable for a player dialog."""

    def __init__(self, category: str, key: str, **values: str | int):
        self.category, self.key, self.values = category, key, values
        super().__init__(text(category, key, **values))


@dataclass(frozen=True)
class Language:
    name: str
    flag: str | None = None


@dataclass(frozen=True)
class StringEntry:
    translations: Mapping[str, str | None]
    parameters: frozenset[str]

    def format(self, locale: str, default_locale: str, values: Mapping[str, str | int]) -> str:
        if locale not in self.translations:
            raise CatalogError(f"unsupported locale: {locale}")
        if values.keys() != self.parameters:
            raise CatalogError(f"expected parameters: {', '.join(sorted(self.parameters))}")
        if any(type(value) not in (str, int) for value in values.values()):
            raise CatalogError("parameters must be str or int")
        template = self.translations[locale] or self.translations[default_locale]
        return template.format(**values)


@dataclass(frozen=True)
class StringCatalog:
    default_locale: str
    languages: Mapping[str, Language]
    categories: Mapping[str, Mapping[str, StringEntry]]

    def text(self, category: str, key: str, *, locale: str | None = None, **values: str | int) -> str:
        category, key = f"{category}.{key}".rsplit(".", 1)
        return self.categories[category][key].format(locale or self.default_locale, self.default_locale, values)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate key: {key}")
        result[key] = value
    return result


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise CatalogError(f"{path.name}: expected an object")
    return value


def _template_fields(value: str, label: str) -> frozenset[str]:
    fields = set()
    try:
        for _, name, spec, conversion in Formatter().parse(value):
            if name is None:
                continue
            if not _PARAMETER.fullmatch(name) or spec or conversion is not None:
                raise CatalogError(f"{label}: use simple named placeholders without formatting")
            fields.add(name)
    except ValueError as error:
        raise CatalogError(f"{label}: invalid template: {error}") from error
    return frozenset(fields)


def _template(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{label}: translation must be non-empty text")
    checked = value.replace("\n", "") if label.startswith("options.") else value
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in checked):
        raise CatalogError(f"{label}: controls and Unicode surrogates are forbidden")
    return value


def _native_template(value: str, key: str) -> None:
    field = _NATIVE_FIELDS.get(key)
    if field is None:
        return
    encoding, capacity = field
    try:
        size = len(value.encode(encoding))
    except UnicodeEncodeError as error:
        raise CatalogError(f"{key}: native field requires {encoding}") from error
    if size >= capacity:
        raise CatalogError(f"{key}: native field requires fewer than {capacity} bytes")


def _languages(document: dict) -> Mapping[str, Language]:
    if document.keys() != {"default", "languages"} or document["default"] != "en":
        raise CatalogError("languages.json requires default en and languages")
    if not isinstance(document["languages"], list):
        raise CatalogError("languages must be a list")
    result = {}
    for details in document["languages"]:
        code, language = _language(details)
        if code in result:
            raise CatalogError(f"duplicate language: {code}")
        result[code] = language
    if "en" not in result:
        raise CatalogError("languages must declare en")
    return MappingProxyType(result)


def _language(details: object) -> tuple[str, Language]:
    if not isinstance(details, dict) or not {"code", "name"} <= details.keys() <= {"code", "name", "flag"}:
        raise CatalogError("language requires code, name and optional flag")
    code = details["code"]
    if not isinstance(code, str) or not _LANGUAGE.fullmatch(code):
        raise CatalogError(f"invalid language code: {code}")
    flag = details.get("flag")
    if "flag" in details and (not isinstance(flag, str) or not _FLAG.fullmatch(flag)):
        raise CatalogError(f"{code}: invalid flag name")
    return code, Language(_template(details["name"], code), flag)


def _translations(path: Path, locale: str, english: dict) -> dict:
    translations = _read_object(path / f"{locale}.json")
    if translations.keys() - english.keys():
        raise CatalogError(f"{locale}: unknown English keys: {sorted(translations.keys() - english.keys())}")
    for key, value in translations.items():
        value = _template(value, key)
        if _template_fields(value, key) != _template_fields(english[key], key):
            raise CatalogError(f"{locale}.{key}: placeholders must match English")
        _native_template(value, key)
    return translations


def _validate_english(english: dict) -> None:
    texts = {}
    for key, value in english.items():
        if not _KEY.fullmatch(key) or "." not in key:
            raise CatalogError(f"invalid semantic key: {key}")
        _template(value, key)
        _template_fields(value, key)
        _native_template(value, key)
        texts.setdefault(value, set()).add(key)
    for value, keys in texts.items():
        if len(keys) > 1 and not DUPLICATE_EXCEPTIONS.get(frozenset(keys)):
            raise CatalogError(f"duplicate English text {value!r}: {', '.join(sorted(keys))}")


def read_catalog(path: Path = CATALOG_PATH) -> StringCatalog:
    languages = _languages(_read_object(path / "languages.json"))
    english = _read_object(path / "en.json")
    _validate_english(english)
    translations = {code: _translations(path, code, english) for code in languages}
    categories = {}
    for label, value in english.items():
        category, key = label.rsplit(".", 1)
        entry = StringEntry(
            MappingProxyType({code: values.get(label) for code, values in translations.items()}),
            _template_fields(value, label),
        )
        categories.setdefault(category, {})[key] = entry
    return StringCatalog(
        "en", languages, MappingProxyType({key: MappingProxyType(value) for key, value in categories.items()})
    )


@cache
def load_catalog(path: Path = CATALOG_PATH) -> StringCatalog:
    return read_catalog(path)


def text(category: str, key: str, **values: str | int) -> str:
    return load_catalog().text(category, key, **values)
