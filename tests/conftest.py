import importlib
import json
import os
from functools import partial
from pathlib import Path

import pytest

from tests.generation_support import ap_source as ap_source
from tests.generation_support import compile_once
from tests.generation_support import compiler as compiler
from tests.generation_support import generated as generated
from tests.generation_support import scratch as scratch

# importing Archipelago clients otherwise checks, and may try to install, every world's requirements
os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")


@pytest.fixture(scope="session")
def compiled_room(request):
    return partial(compile_once, request.getfixturevalue("compiler"), request.getfixturevalue("generated"),
                   request.getfixturevalue("scratch"))


def pytest_sessionstart(session):
    root = session.config._tmp_path_factory.getbasetemp() / "runtime-config"
    config = {
        "doom_base_dir": str(root / "game/base"),
        "save_games_dir": str(root / "saves"),
        "steam_remote_dir": str(root / "Steam/userdata/1/782330/remote"),
        "client_state_file": str(root / "state.json"),
        "bridge_log_path": str(root / "bridge.log"),
    }
    for key in ("doom_base_dir", "save_games_dir", "steam_remote_dir"):
        Path(config[key]).mkdir(parents=True)
    (root / "game/DOOMEternalx64vk.exe").touch()
    path = root / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    os.environ["DOOM_AP_CONFIG_FILE"] = str(path)
    os.environ["DOOM_AP_APPLICATION_DIR"] = str(root)


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config = {
        "doom_base_dir": str(tmp_path / "game/base"),
        "save_games_dir": str(tmp_path),
        "steam_remote_dir": str(tmp_path / "Steam/userdata/1/782330/remote"),
        "client_state_file": str(tmp_path / "state.json"),
        "bridge_log_path": str(tmp_path / "bridge.log"),
    }
    for key in ("doom_base_dir", "steam_remote_dir"):
        Path(config[key]).mkdir(parents=True)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(path))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    configuration = importlib.import_module("doom_eap.runtime.bridge_config")
    for name, value in {
        "DOOM_BASE_DIR": config["doom_base_dir"],
        "INV_DUMP_DIR": str(tmp_path),
        "CLIENT_STATE_FILE": tmp_path / "state.json",
        "CONFIG_FILE": path,
        "ENABLE_ITEM_NOTIFICATIONS": False,
    }.items():
        monkeypatch.setattr(configuration, name, value)
    return module
