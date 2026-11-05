"""Protect translation fallback, validation, native limits and standalone projections."""

import json
import shutil
import subprocess
import sys

import pytest

from doom_eap.strings import CATALOG_PATH, CatalogError, load_catalog, read_catalog
from tools.content import compile_player_strings as compiler


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture
def catalog_dir(tmp_path):
    path = tmp_path / "strings"
    shutil.copytree(CATALOG_PATH, path)
    shutil.copyfile(CATALOG_PATH.parent / "options_schema.json", tmp_path / "options_schema.json")
    return path


def change(path, filename, key, value):
    document = json.loads((path / filename).read_text(encoding="utf-8"))
    document[key] = value
    write_json(path / filename, document)


def test_fallback_unicode_parameters_and_immutable_catalog(catalog_dir):
    change(catalog_dir, "en.json", "test.received", "Received {item}")
    change(catalog_dir, "pt-BR.json", "test.received", "Recebido: {item}")
    catalog = load_catalog(catalog_dir)
    assert catalog.text("test", "received", locale="pt-BR", item="Canhão {player} 日本語 🛡") == (
        "Recebido: Canhão {player} 日本語 🛡"
    )
    assert load_catalog(catalog_dir) is catalog
    with pytest.raises(TypeError):
        catalog.categories["test"]["received"].translations["en"] = "changed"
    with pytest.raises(TypeError):
        catalog.languages["fr"] = catalog.languages["en"]
    with pytest.raises(KeyError):
        catalog.text("test", "absent")
    with pytest.raises(CatalogError, match="unsupported locale"):
        catalog.text("test", "received", locale="fr", item="Dash")
    document = json.loads((catalog_dir / "pt-BR.json").read_text(encoding="utf-8"))
    del document["test.received"]
    write_json(catalog_dir / "pt-BR.json", document)
    assert read_catalog(catalog_dir).text("test", "received", locale="pt-BR", item="Dash") == "Received Dash"


@pytest.mark.parametrize(
    "template",
    [
        "{other}",
        "{}",
        "{0}",
        "{item.name}",
        "{item[0]}",
        "{item!r}",
        "{item:>10}",
        "{item:{width}}",
        "{item",
        "{item}\x00",
        "{item}\x85",
        "{item}\ud800",
        "",
    ],
)
def test_invalid_translation_templates_fail(catalog_dir, template):
    change(catalog_dir, "en.json", "test.received", "Received {item}")
    change(catalog_dir, "pt-BR.json", "test.received", template)
    with pytest.raises(CatalogError):
        read_catalog(catalog_dir)


@pytest.mark.parametrize("values", [{}, {"count": True}, {"count": []}, {"count": 2, "extra": "x"}])
def test_format_checks_inferred_fields_and_value_types(catalog_dir, values):
    change(catalog_dir, "en.json", "test.count", "{{{count}}}")
    catalog = read_catalog(catalog_dir)
    assert catalog.text("test", "count", count=2) == "{2}"
    with pytest.raises(CatalogError):
        catalog.text("test", "count", **values)


def test_duplicate_keys_text_and_unknown_translation_fail(catalog_dir):
    (catalog_dir / "pt-BR.json").write_text('{"test.x": "a", "test.x": "b"}', encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate key"):
        read_catalog(catalog_dir)
    write_json(catalog_dir / "pt-BR.json", {"test.x": "a"})
    with pytest.raises(CatalogError, match="unknown English keys"):
        read_catalog(catalog_dir)
    write_json(catalog_dir / "pt-BR.json", {})
    change(catalog_dir, "en.json", "test.one", "Duplicate test text")
    change(catalog_dir, "en.json", "test.two", "Duplicate test text")
    with pytest.raises(CatalogError, match="duplicate English text"):
        read_catalog(catalog_dir)


@pytest.mark.parametrize(
    "details",
    [
        {"code": "../fr", "name": "Français"},
        {"code": "fr", "name": ""},
        {"code": "fr", "name": "Français", "flag": "../fr"},
        {"code": "fr", "name": "Français", "flag": None},
        {"code": "en", "name": "Duplicate"},
    ],
)
def test_invalid_language_declarations_fail(catalog_dir, details):
    document = json.loads((catalog_dir / "languages.json").read_text(encoding="utf-8"))
    document["languages"].append(details)
    write_json(catalog_dir / "languages.json", document)
    with pytest.raises(CatalogError):
        read_catalog(catalog_dir)


def test_descriptions_allow_newlines_but_reject_other_controls(catalog_dir):
    description = "**Choose** a goal.\n\n- Win with `Unmaykr`."
    change(catalog_dir, "en.json", "options.test.description", description)
    assert read_catalog(catalog_dir).text("options.test", "description") == description
    change(catalog_dir, "en.json", "options.test.description", "Win.\tNow.")
    with pytest.raises(CatalogError, match="controls"):
        read_catalog(catalog_dir)


def test_projection_check_is_standalone_deterministic_and_does_not_write(catalog_dir, tmp_path):
    apworld, core = tmp_path / "apworld", tmp_path / "core"
    output = apworld / "player_strings_generated.py"
    header = core / "src/player_strings_generated.h"
    with pytest.raises(ValueError, match="missing or stale"):
        compiler.compile_player_strings(catalog_dir, check=True, apworld_root=apworld, sentinel_root=core)
    assert not output.exists()
    assert set(compiler.compile_player_strings(catalog_dir, apworld_root=apworld, sentinel_root=core)) == {
        output,
        header,
    }
    expected = output.read_bytes(), header.read_bytes()
    assert compiler.compile_player_strings(catalog_dir, check=True, apworld_root=apworld, sentinel_root=core) == ()
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import runpy, sys; m = runpy.run_path(sys.argv[1]); "
            "assert m['text']('apworld.notices', 'speed_supported', species='Imp'); "
            "assert not any(name.startswith('doom_eap') for name in sys.modules)",
            str(output),
        ],
        check=True,
        cwd=tmp_path,
    )
    header.write_bytes(b"stale")
    with pytest.raises(ValueError, match="missing or stale"):
        compiler.compile_player_strings(catalog_dir, check=True, apworld_root=apworld, sentinel_root=core)
    assert header.read_bytes() == b"stale"
    compiler.compile_player_strings(catalog_dir, apworld_root=apworld, sentinel_root=core)
    assert (output.read_bytes(), header.read_bytes()) == expected


@pytest.mark.parametrize("drift", ["catalog", "schema", "choice_added"])
def test_option_english_drift_fails_without_writing(catalog_dir, tmp_path, drift):
    if drift == "catalog":
        change(catalog_dir, "en.json", "options.goal.name", "Changed goal")
    else:
        schema = json.loads((tmp_path / "options_schema.json").read_text(encoding="utf-8"))
        if drift == "schema":
            schema["options"][0]["description"] += " Changed."
        else:
            schema["options"][0].setdefault("choices", []).append({"key": "new", "label": "New choice"})
        write_json(tmp_path / "options_schema.json", schema)
    with pytest.raises(ValueError, match="English text missing or differs"):
        compiler.compile_player_strings(catalog_dir, check=True, apworld_root=tmp_path, sentinel_root=tmp_path)


@pytest.mark.parametrize(
    "template,capacity,arguments",
    [
        ("X" * 159, 160, {}),
        ("á" * 79 + "X", 160, {}),
        ("{key}" + "X" * 16, 32, {"key": ("str", "%s", 15)}),
        ("{count} / {target}" + "X" * 36, 80, {"count": ("int", "%llu", 20), "target": ("int", "%llu", 20)}),
    ],
)
def test_native_buffers_count_bytes_and_formatted_values(template, capacity, arguments):
    compiler.native_translation(template, "test", capacity, arguments)
    with pytest.raises(ValueError, match="fewer than"):
        compiler.native_translation(template + "X", "test", capacity, arguments)


def test_native_parameter_order_is_checked(catalog_dir, tmp_path):
    change(catalog_dir, "en.json", "game.hud.charges", "{count} {count} CHARGES")
    with pytest.raises(ValueError, match="native parameters"):
        compiler.project_sentinel(read_catalog(catalog_dir), tmp_path)


def test_combined_controls_reserve_utf8_bytes(catalog_dir, tmp_path):
    change(catalog_dir, "en.json", "common.ammo_refill", "A" * 80)
    change(catalog_dir, "pt-BR.json", "common.ammo_refill", "á" * 40)
    change(catalog_dir, "pt-BR.json", "game.controls.key_conflict", "é" * 38)
    compiler.project_sentinel(read_catalog(catalog_dir), tmp_path)
    change(catalog_dir, "pt-BR.json", "game.controls.key_conflict", "é" * 38 + "X")
    with pytest.raises(ValueError, match="fewer than 160 bytes"):
        compiler.project_sentinel(read_catalog(catalog_dir), tmp_path)


@pytest.mark.parametrize(
    "key,value",
    [
        ("fortress_title", "Á"),
        ("fortress_title", "X" * 96),
        ("reward_unknown", "á" * 256),
        ("local_item", "YOUR {item}" + "X" * 501),
    ],
)
def test_native_menu_wire_limits_are_validated(catalog_dir, key, value):
    change(catalog_dir, "en.json", "notifications." + key, value)
    with pytest.raises(CatalogError, match="native field"):
        read_catalog(catalog_dir)
