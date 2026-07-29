"""Pytest unit tests for telemetry marker discovery and cleanup."""

import os
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch
import pytest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("Utils", types.SimpleNamespace(init_logging=lambda *args, **kwargs: None))
sys.modules.setdefault("colorama", types.SimpleNamespace(init=lambda: None, deinit=lambda: None))

class _DummyCommonContext:
    def __init__(self, server_address=None, password=None):
        self.server_address = server_address
        self.password = password

common_client = types.ModuleType("CommonClient")
common_client.CommonContext = _DummyCommonContext
common_client.server_loop = lambda ctx: None
common_client.gui_enabled = False
common_client.ClientCommandProcessor = object
common_client.get_base_parser = lambda: __import__("argparse").ArgumentParser()
common_client.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
sys.modules.setdefault("CommonClient", common_client)

net_utils = types.ModuleType("NetUtils")
net_utils.ClientStatus = types.SimpleNamespace(CLIENT_GOAL=30)
sys.modules.setdefault("NetUtils", net_utils)

import bridge_client


def test_discover_telemetry_markers_finds_and_sorts_by_mtime():
    with tempfile.TemporaryDirectory() as tmp_dir:
        inv_dump = Path(tmp_dir)
        m1 = inv_dump / "ap_telemetry_ready.txt"
        m2 = inv_dump / "ap_telemetry_ready_0.txt"
        m3 = inv_dump / "ap_telemetry_ready_0_1.txt"
        m4 = inv_dump / "ap_telemetry_ready_0_1_2.txt"

        m1.write_text("ready", encoding="utf-8")
        os.utime(m1, ns=(100, 100))
        m2.write_text("ready", encoding="utf-8")
        os.utime(m2, ns=(200, 200))
        m3.write_text("ready", encoding="utf-8")
        os.utime(m3, ns=(300, 300))
        m4.write_text("ready", encoding="utf-8")
        os.utime(m4, ns=(400, 400))

        with patch.object(bridge_client, "INV_DUMP_DIR", str(inv_dump)):
            markers = bridge_client.discover_telemetry_markers()
            assert len(markers) == 4
            assert markers[-1][1] == str(m4)
            assert markers[-1][0] == 400
