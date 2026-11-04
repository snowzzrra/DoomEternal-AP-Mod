"""Load and format the player text catalog; consumer presentation stays separate."""

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

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "player_strings.json"
GAME_LOCALES = MappingProxyType({"english": "en", "portuguese": "pt-BR"})
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_FLAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.svg")
_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_PARAMETER = re.compile(r"[a-z][a-z0-9_]*")
_TYPES = {"str": str, "int": int}
# native menu fields include a terminating NUL; room-provided names are checked at serialization
_NATIVE_FIELDS = {
    "notifications.fortress_title": ("ascii", 96),
    "notifications.reward_unknown": ("utf-8", 512),
    "notifications.local_item": ("utf-8", 512),
    "notifications.remote_item": ("utf-8", 512),
    "notifications.trap_sentence": ("utf-8", 512),
}


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Language:
    name: str
    flag: str | None = None


@dataclass(frozen=True)
class StringEntry:
    translations: Mapping[str, str | None]
    parameters: Mapping[str, str]

    def format(self, locale: str, default_locale: str, values: Mapping[str, str | int]) -> str:
        if locale not in self.translations:
            raise CatalogError(f"unsupported locale: {locale}")
        if values.keys() != self.parameters.keys():
            raise CatalogError(f"expected parameters: {', '.join(self.parameters)}")
        for name, kind in self.parameters.items():
            if type(values[name]) is not _TYPES[kind]:
                raise CatalogError(f"parameter {name} must be {kind}")
        template = self.translations[locale]
        if template is None:
            template = self.translations[default_locale]
        assert template is not None
        return template.format(**values)


@dataclass(frozen=True)
class StringCatalog:
    default_locale: str
    languages: Mapping[str, Language]
    categories: Mapping[str, Mapping[str, StringEntry]]

    def text(self, category: str, key: str, *, locale: str | None = None, **values: str | int) -> str:
        entry = self.categories[category][key]
        return entry.format(self.default_locale if locale is None else locale, self.default_locale, values)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate key: {key}")
        result[key] = value
    return result


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be an object")
    return value


def _parameters(value: object, label: str) -> dict[str, str]:
    parameters = _object(value, f"{label}.parameters")
    for name, kind in parameters.items():
        if not _PARAMETER.fullmatch(name):
            raise CatalogError(f"{label}: invalid parameter name: {name}")
        if not isinstance(kind, str) or kind not in _TYPES:
            raise CatalogError(f"{label}: parameter {name} requires str or int")
    return parameters


def _template_fields(value: str, label: str) -> set[str]:
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
    return fields


def _template(value: object, parameters: dict[str, str], label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{label}: translation must be non-empty text")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise CatalogError(f"{label}: controls and Unicode surrogates are forbidden")
    if _template_fields(value, label) != parameters.keys():
        raise CatalogError(f"{label}: placeholders must match declared parameters")
    return value


def _native_template(value: str, label: str) -> None:
    field = _NATIVE_FIELDS.get(label.rsplit(".", 1)[0])
    if field is None:
        return
    encoding, capacity = field
    try:
        size = len(value.encode(encoding))
    except UnicodeEncodeError as error:
        raise CatalogError(f"{label}: native field requires {encoding}") from error
    if size >= capacity:
        raise CatalogError(f"{label}: native field requires fewer than {capacity} bytes")


def _entry(value: object, label: str, default_locale: str, languages: Mapping[str, Language]) -> StringEntry:
    document = _object(value, label)
    if document.keys() - (languages.keys() | {"parameters"}):
        raise CatalogError(f"{label}: unknown entry fields")
    if not languages.keys() <= document.keys():
        raise CatalogError(f"{label}: declare every locale; use null for missing translations")
    parameters = _parameters(document.get("parameters", {}), label)
    translations = {}
    for locale in sorted(languages):
        text = document[locale]
        if text is None and locale != default_locale:
            translations[locale] = None
        else:
            translations[locale] = _template(text, parameters, f"{label}.{locale}")
            _native_template(translations[locale], f"{label}.{locale}")
    return StringEntry(MappingProxyType(translations), MappingProxyType(parameters))


def _entries(value: object, category: str, default_locale: str, languages: Mapping[str, Language]) -> Mapping[str, StringEntry]:
    entries = _object(value, category)
    result = {}
    for key, entry in entries.items():
        if not _KEY.fullmatch(key):
            raise CatalogError(f"{category}: invalid semantic key: {key}")
        result[key] = _entry(entry, f"{category}.{key}", default_locale, languages)
    return MappingProxyType(result)


def _languages(value: object) -> Mapping[str, Language]:
    result = {}
    for code, details in _object(value, "languages").items():
        if not _LANGUAGE.fullmatch(code):
            raise CatalogError(f"invalid language code: {code}")
        details = _object(details, f"languages.{code}")
        if details.keys() - {"name", "flag"} or "name" not in details:
            raise CatalogError(f"languages.{code}: requires name and optional flag")
        name = _template(details["name"], {}, f"languages.{code}.name")
        flag = details.get("flag")
        if "flag" in details and (not isinstance(flag, str) or not _FLAG.fullmatch(flag)):
            raise CatalogError(f"languages.{code}: flag must be an SVG filename")
        result[code] = Language(name, flag)
    if "en" not in result:
        raise CatalogError("languages must declare en")
    return MappingProxyType(result)


def read_catalog(path: Path = CATALOG_PATH) -> StringCatalog:
    document = _object(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object), "catalog")
    if document.keys() != {"schema_version", "default_locale", "languages", "categories"}:
        raise CatalogError("catalog requires only schema_version, default_locale, languages and categories")
    if type(document["schema_version"]) is not int or document["schema_version"] != 2:
        raise CatalogError("unsupported catalog schema_version")
    if document["default_locale"] != "en":
        raise CatalogError("default_locale must be en")
    languages = _languages(document["languages"])
    categories = _object(document["categories"], "categories")
    result = {}
    for category, entries in categories.items():
        if not _KEY.fullmatch(category):
            raise CatalogError(f"invalid category: {category}")
        result[category] = _entries(entries, category, document["default_locale"], languages)
    return StringCatalog(document["default_locale"], languages, MappingProxyType(result))


@cache
def load_catalog(path: Path = CATALOG_PATH) -> StringCatalog:
    return read_catalog(path)


def text(category: str, key: str, **values: str | int) -> str:
    return load_catalog().text(category, key, **values)
