"""Actual Windows file ownership and scheduler with a deterministic transport port."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_native_queue_contract(tmp_path):
    compiler = shutil.which("cl.exe")
    if compiler is None:
        pytest.skip("native queue harness requires the supported MSVC environment")
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path / "queue_contract.exe"
    result = subprocess.run([
        compiler, "/nologo", "/std:c++17", "/EHsc", "/DNOMINMAX", f"/I{root}",
        str(root / "tests/native_queue_contract.cpp"), str(root / "native/client/command_queue.cpp"),
        f"/Fe{executable}", f"/Fo{tmp_path}\\",
    ], cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(executable)], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
