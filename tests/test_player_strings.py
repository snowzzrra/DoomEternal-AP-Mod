"""Protect catalog validation, explicit translation gaps and projection checks."""

import json
import subprocess
import sys

import pytest

from doom_eap.strings import CatalogError, load_catalog, read_catalog
from tools.content import compile_player_strings as compiler


@pytest.fixture
def document():
    return {
        "schema_version": 2,
        "default_locale": "en",
        "languages": {"en": {"name": "English"}, "pt-BR": {"name": "Português (Brasil)"}},
        "categories": {
            "notifications": {
                "received": {
                    "parameters": {"item": "str"},
                    "en": "Received {item}",
                    "pt-BR": "Recebido: {item}",
                }
            }
        },
    }


def write_catalog(tmp_path, document):
    path = tmp_path / "player_strings.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_unicode_and_inserted_braces_are_preserved(tmp_path, document):
    path = write_catalog(tmp_path, document)
    catalog = load_catalog(path)
    assert catalog.text("notifications", "received", locale="pt-BR", item="Canhão {player} 日本語 🛡") == (
        "Recebido: Canhão {player} 日本語 🛡"
    )
    assert catalog.text("notifications", "received", item="Canhão") == "Received Canhão"
    assert load_catalog(path) is catalog
    with pytest.raises(TypeError):
        catalog.categories["notifications"]["new"] = catalog.categories["notifications"]["received"]
    with pytest.raises(TypeError):
        catalog.categories["notifications"]["received"].translations["en"] = "changed"


def test_only_explicit_translation_gaps_use_english(tmp_path, document):
    entry = document["categories"]["notifications"]["received"]
    entry["pt-BR"] = None
    path = write_catalog(tmp_path, document)
    catalog = read_catalog(path)
    assert catalog.text("notifications", "received", locale="pt-BR", item="Dash") == "Received Dash"
    with pytest.raises(CatalogError, match="unsupported locale"):
        catalog.text("notifications", "received", locale="spanish", item="Dash")
    del entry["pt-BR"]
    write_catalog(tmp_path, document)
    with pytest.raises(CatalogError, match="declare every locale"):
        read_catalog(path)


@pytest.mark.parametrize("values", [{}, {"item": 7}, {"item": "Dash", "player": "Slayer"}])
def test_format_requires_declared_parameters_and_types(tmp_path, document, values):
    catalog = read_catalog(write_catalog(tmp_path, document))
    with pytest.raises(CatalogError):
        catalog.text("notifications", "received", **values)


def test_integer_parameter_rejects_boolean_and_literal_braces_work(tmp_path, document):
    entry = document["categories"]["notifications"]["received"]
    entry.update(parameters={"count": "int"}, **{"en": "{{{count}}}", "pt-BR": None})
    catalog = read_catalog(write_catalog(tmp_path, document))
    assert catalog.text("notifications", "received", count=2) == "{2}"
    with pytest.raises(CatalogError, match="must be int"):
        catalog.text("notifications", "received", count=True)


@pytest.mark.parametrize("template", [
    "{player}", "{}", "{0}", "{item.name}", "{item[0]}", "{item!r}",
    "{item:>10}", "{item:{width}}", "{item", "{item}\x00", "{item}\x85", "{item}\ud800", "",
])
def test_invalid_translation_templates_fail(tmp_path, document, template):
    document["categories"]["notifications"]["received"]["pt-BR"] = template
    # escaped JSON allows the invalid surrogate to reach the catalog validator
    path = tmp_path / "player_strings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CatalogError):
        read_catalog(path)


@pytest.mark.parametrize("parameters", [{"item": "float"}, {"item": []}, {"item.name": "str"}, []])
def test_invalid_parameter_declarations_fail(tmp_path, document, parameters):
    document["categories"]["notifications"]["received"]["parameters"] = parameters
    with pytest.raises(CatalogError):
        read_catalog(write_catalog(tmp_path, document))


@pytest.mark.parametrize("field,value", [
    ("schema_version", 1), ("schema_version", True), ("default_locale", "pt-BR"),
    ("unexpected", True), ("categories", []),
])
def test_invalid_catalog_structure_fails(tmp_path, document, field, value):
    document[field] = value
    with pytest.raises(CatalogError):
        read_catalog(write_catalog(tmp_path, document))


@pytest.mark.parametrize("field,value", [("en", None), ("pt-BR", 4), ("comment", "extra")])
def test_invalid_entry_fields_fail(tmp_path, document, field, value):
    document["categories"]["notifications"]["received"][field] = value
    with pytest.raises(CatalogError):
        read_catalog(write_catalog(tmp_path, document))


@pytest.mark.parametrize("payload", [
    '{"schema_version": 2, "schema_version": 1}',
    '{"en": "first", "en": "second"}',
    '{"categories": {"notifications": {}, "notifications": {}}}',
])
def test_duplicate_json_keys_fail(tmp_path, payload):
    path = tmp_path / "player_strings.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate key"):
        read_catalog(path)


@pytest.mark.parametrize("languages", [
    {}, [], {"fr": {"name": "Français"}}, {"en": {"name": ""}},
    {"en_US": {"name": "English"}}, {"en": {"name": "English", "extra": True}},
    {"en": {"name": "English", "flag": "../en.svg"}},
    {"en": {"name": "English", "flag": None}},
])
def test_invalid_language_declarations_fail(tmp_path, document, languages):
    document["languages"] = languages
    with pytest.raises(CatalogError):
        read_catalog(write_catalog(tmp_path, document))


def test_new_language_requires_declared_keys_and_explicit_fallbacks(tmp_path, document):
    document["languages"]["fr"] = {"name": "Français"}
    entry = document["categories"]["notifications"]["received"]
    entry["fr"] = "Reçu : {item}"
    path = write_catalog(tmp_path, document)
    catalog = read_catalog(path)
    assert catalog.text("notifications", "received", locale="fr", item="Dash") == "Reçu : Dash"
    assert catalog.languages["fr"].flag is None
    with pytest.raises(TypeError):
        catalog.languages["fr"] = catalog.languages["en"]
    del document["languages"]["fr"]
    with pytest.raises(CatalogError, match="unknown entry fields"):
        read_catalog(write_catalog(tmp_path, document))


def test_projection_check_detects_missing_and_stale_files_without_writing(tmp_path, document):
    document["categories"]["apworld.errors"] = document["categories"]["notifications"]
    path = write_catalog(tmp_path, document)
    root = tmp_path / "apworld"
    output = root / "player_strings_generated.py"
    expected = compiler.project_catalog(read_catalog(path), root)[output]
    with pytest.raises(ValueError, match="missing or stale"):
        compiler.compile_player_strings(path, check=True, apworld_root=root, sentinel_root=tmp_path / 'core')
    assert not output.exists()
    assert compiler.compile_player_strings(path, apworld_root=root, sentinel_root=tmp_path / 'core') == (output, tmp_path / 'core/src/player_strings_generated.h')
    assert compiler.compile_player_strings(path, check=True, apworld_root=root, sentinel_root=tmp_path / 'core') == ()
    output.write_bytes(b"stale")
    assert compiler.main(["--catalog", str(path), "--apworld-root", str(root), "--sentinel-root", str(tmp_path / 'core'), "--check"]) == 1
    assert output.read_bytes() == b"stale"
    assert compiler.compile_player_strings(path, apworld_root=root, sentinel_root=tmp_path / 'core') == (output,)
    assert output.read_bytes() == expected
    del document["categories"]["apworld.errors"]
    write_catalog(tmp_path, document)
    with pytest.raises(ValueError, match="missing or stale"):
        compiler.compile_player_strings(path, check=True, apworld_root=root, sentinel_root=tmp_path / 'core')
    assert output.read_bytes() == expected
    assert compiler.compile_player_strings(path, apworld_root=root, sentinel_root=tmp_path / 'core') == (output,)
    assert compiler.compile_player_strings(path, check=True, apworld_root=root, sentinel_root=tmp_path / 'core') == ()


def test_apworld_projection_is_standalone_and_order_independent(tmp_path, document):
    document["categories"]["apworld.web"] = {
        "title": {"en": "Canhão 日本語 🛡", "pt-BR": None},
    }
    document["categories"]["apworld.errors"] = document["categories"]["notifications"]
    path = write_catalog(tmp_path, document)
    output, _header = compiler.compile_player_strings(path, apworld_root=tmp_path, sentinel_root=tmp_path / 'core')
    expected = output.read_bytes()
    document["categories"] = dict(reversed(list(document["categories"].items())))
    write_catalog(tmp_path, document)
    assert compiler.compile_player_strings(path, check=True, apworld_root=tmp_path, sentinel_root=tmp_path / 'core') == ()
    assert output.read_bytes() == expected
    subprocess.run([
        sys.executable, "-I", "-X", "utf8", "-c",
        "import runpy, sys; m = runpy.run_path(sys.argv[1]); "
        "assert m['text']('apworld.errors', 'received', item='日本語 {player}') == 'Received 日本語 {player}'; "
        "assert m['text']('apworld.web', 'title') == 'Canhão 日本語 🛡'; "
        "assert not any(name.startswith('doom_eap') for name in sys.modules)",
        str(output),
    ], cwd=tmp_path, check=True)


def test_compiler_reads_catalog_changes_and_reports_invalid_input(tmp_path, document):
    path = write_catalog(tmp_path, document)
    load_catalog(path)
    path.write_text('{"schema_version":', encoding="utf-8")
    assert compiler.main(["--catalog", str(path), "--check"]) == 1
    path.write_bytes(b"\xff")
    assert compiler.main(["--catalog", str(path)]) == 1
    assert compiler.main(["--catalog", str(tmp_path / "missing.json")]) == 1


def test_sentinel_projection_is_deterministic_and_check_does_not_write(tmp_path, document):
    document["categories"]["game.controls"] = {
        "clear": {"en": "Clear", "pt-BR": None},
        "group": {"en": "AP Controls", "pt-BR": None},
    }
    path = write_catalog(tmp_path, document)
    apworld, sentinel = tmp_path / "apworld", tmp_path / "sentinel"
    header = sentinel / "src/player_strings_generated.h"
    compiler.compile_player_strings(path, apworld_root=apworld, sentinel_root=sentinel)
    expected = header.read_bytes()
    document["categories"]["game.controls"] = dict(reversed(list(document["categories"]["game.controls"].items())))
    write_catalog(tmp_path, document)
    assert compiler.compile_player_strings(path, check=True, apworld_root=apworld, sentinel_root=sentinel) == ()
    assert header.read_bytes() == expected
    header.write_bytes(b"stale")
    with pytest.raises(ValueError, match="missing or stale"):
        compiler.compile_player_strings(path, check=True, apworld_root=apworld, sentinel_root=sentinel)
    assert header.read_bytes() == b"stale"
    header.unlink()
    with pytest.raises(ValueError, match="missing or stale"):
        compiler.compile_player_strings(path, check=True, apworld_root=apworld, sentinel_root=sentinel)
    assert not header.exists()


@pytest.mark.parametrize("locale", ["en", "pt-BR"])
def test_sentinel_ascii_and_controls_buffer_limits(tmp_path, document, locale):
    entry = {"en": "Clear", "pt-BR": None}
    document["categories"]["game.controls"] = {"clear": entry}
    entry[locale] = "X" * 159
    path = write_catalog(tmp_path, document)
    compiler.project_sentinel(read_catalog(path), tmp_path)
    entry[locale] += "X"
    write_catalog(tmp_path, document)
    with pytest.raises(ValueError, match="fewer than 160 bytes"):
        compiler.project_sentinel(read_catalog(path), tmp_path)
    entry[locale] = "Cléar"
    write_catalog(tmp_path, document)
    with pytest.raises(ValueError, match="requires ascii"):
        compiler.project_sentinel(read_catalog(path), tmp_path)


def test_controls_combined_label_reserves_action_and_error_bytes(tmp_path, document):
    document["categories"]["game.controls"] = {
        "ammo_refill": {"en": "A" * 80, "pt-BR": None},
        "key_conflict": {"en": "E" * 76, "pt-BR": None},
        "binding_error": {"parameters": {"action": "str", "error": "str"},
                          "en": "{action} - {error}", "pt-BR": None},
    }
    path = write_catalog(tmp_path, document)
    compiler.project_sentinel(read_catalog(path), tmp_path)
    document["categories"]["game.controls"]["key_conflict"]["en"] += "E"
    write_catalog(tmp_path, document)
    with pytest.raises(ValueError, match="fewer than 160 bytes"):
        compiler.project_sentinel(read_catalog(path), tmp_path)


@pytest.mark.parametrize("key,parameters,template,capacity", [
    ("resource_unknown", {}, "", 32),
    ("crucible_selected", {}, "", 96),
    ("key_toggle", {"key": "str"}, "{key}" + "X" * 15, 32),
])
def test_hud_buffers_reserve_formatted_values(tmp_path, document, key, parameters, template, capacity):
    template = template or "X" * (capacity - 2)
    entry = {"parameters": parameters, "en": template + "X", "pt-BR": None}
    document["categories"]["game.hud"] = {key: entry}
    path = write_catalog(tmp_path, document)
    compiler.project_sentinel(read_catalog(path), tmp_path)
    entry["en"] += "X"
    write_catalog(tmp_path, document)
    with pytest.raises(ValueError, match=f"fewer than {capacity} bytes"):
        compiler.project_sentinel(read_catalog(path), tmp_path)


@pytest.mark.parametrize("parameters,template", [
    ({"count": "str"}, "{count} CHARGES"),
    ({"count": "int"}, "{count} {count} CHARGES"),
])
def test_native_parameter_changes_fail_before_compilation(tmp_path, document, parameters, template):
    document["categories"]["game.hud"] = {
        "charges": {"parameters": parameters, "en": template, "pt-BR": None},
    }
    path = write_catalog(tmp_path, document)
    with pytest.raises(ValueError, match="native parameters"):
        compiler.project_sentinel(read_catalog(path), tmp_path)


def test_menu_title_buffer_counts_terminating_nul(tmp_path, document):
    entry = {"en": "X" * 95, "pt-BR": None}
    document["categories"]["game.menu"] = {"choose_mission": entry}
    path = write_catalog(tmp_path, document)
    compiler.project_sentinel(read_catalog(path), tmp_path)
    entry["en"] += "X"
    write_catalog(tmp_path, document)
    with pytest.raises(ValueError, match="fewer than 96 bytes"):
        compiler.project_sentinel(read_catalog(path), tmp_path)


@pytest.mark.parametrize("key,english,portuguese", [
    ("fortress_title", "FORTRESS OF DOOM", "FORTALEZA DA PERDIÇÃO"),
    ("fortress_title", "X" * 96, None),
    ("reward_unknown", "X" * 512, None),
    ("reward_unknown", "REWARD UNKNOWN", "á" * 256),
    ("local_item", "YOUR {item}" + "X" * 501, None),
    ("remote_item", "{item} FOR {recipient}" + "X" * 491, None),
    ("trap_sentence", "X" * 512, None),
])
def test_native_catalog_fields_reject_invalid_translations(tmp_path, document, key, english, portuguese):
    parameters = {name: "str" for name in ("item", "recipient") if "{" + name + "}" in english}
    document["categories"]["notifications"] = {
        key: {"parameters": parameters, "en": english, "pt-BR": portuguese},
    }
    with pytest.raises(CatalogError, match="native field"):
        read_catalog(write_catalog(tmp_path, document))


def test_native_catalog_fields_accept_byte_boundary_and_missing_translation(tmp_path, document):
    document["categories"]["notifications"] = {
        "fortress_title": {"en": "X" * 95, "pt-BR": None},
        "reward_unknown": {"en": "á" * 255 + "X", "pt-BR": None},
    }
    catalog = read_catalog(write_catalog(tmp_path, document))
    assert len(catalog.text("notifications", "fortress_title").encode("ascii")) == 95
    assert len(catalog.text("notifications", "reward_unknown", locale="pt-BR").encode("utf-8")) == 511
