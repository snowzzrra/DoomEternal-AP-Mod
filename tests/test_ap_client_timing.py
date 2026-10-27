"""Tests for 32-bit tick deadline and sentinel timing semantics in the native AP client."""

import shutil
import subprocess
import tempfile
import os
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_console_transport_accepts_qualified_core_and_releases_native_capacity(tmp_path):
    compiler = shutil.which("cl.exe")
    core = Path(os.environ.get("SENTINEL_CORE_SOURCE", str(REPO_ROOT.parent / "Sentinel-Core")))
    if not compiler or not (core / "include/sentinel_inspection.h").is_file():
        pytest.skip("Windows MSVC and Core SDK source required")
    executable = tmp_path / "command-release.exe"
    subprocess.run([
        compiler, "/nologo", "/std:c++17", "/EHsc", "/DNOMINMAX",
        f"/I{REPO_ROOT / 'native/client'}", f"/I{core / 'include'}",
        f"/I{core / 'src'}", f"/I{core / 'build/generated'}",
        str(REPO_ROOT / "tests/native_command_release.cpp"),
        str(REPO_ROOT / "native/client/sentinel_command_client.cpp"),
        str(core / "src/commands.cpp"), f"/Fe{executable}", "bcrypt.lib",
    ], check=True, cwd=tmp_path, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True)


@pytest.mark.unit
class TestNativeRpcTimingSemantics:


    def test_cpp_queue_policy_native_execution(self):
        """Compile and execute rpc_queue_policy.h against native C++ types if g++ is available."""
        msvc = shutil.which("cl.exe")
        cxx = msvc or shutil.which("g++")
        if not cxx:
            pytest.skip("MSVC or g++ not available on host")

        source = """#include <cassert>
#include <cstdint>
#include "rpc_queue_policy.h"

int main() {
    // Representative tick values
    const std::uint32_t ticks[] = {
        0u, 100u, 1000u,
        0x7FFFFFFEu, 0x7FFFFFFFu,
        0x80000000u, 0x80000001u,
        0xEE000000u, 0xFFFFFFFEu, 0xFFFFFFFFu
    };

    for (std::uint32_t tick : ticks) {
        // Initial sentinel nextAttempt=0 must always be ready immediately
        assert(ReceiptDispatchReady(tick, 0));
    }

    // High-bit uptime: nextAttempt=0 must be ready
    const std::uint32_t pilzkopf = 0xEE000000u;
    assert(ReceiptDispatchReady(pilzkopf, 0));

    // Scheduled deadline must not be ready before delay
    assert(!ReceiptDispatchReady(pilzkopf, pilzkopf + 250));
    assert(ReceiptDispatchReady(pilzkopf + 250, pilzkopf + 250));

    // DWORD wrap handling
    const std::uint32_t wrap_now = 0xFFFFFFFEu;
    const std::uint32_t wrap_deadline = wrap_now + 250; // wraps to 248
    assert(!ReceiptDispatchReady(100u, wrap_deadline));
    assert(ReceiptDispatchReady(248u, wrap_deadline));
    assert(ReceiptDispatchReady(300u, wrap_deadline));

    return 0;
}
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_file = temp_path / "test_main.cpp"
            binary_file = temp_path / ("test_bin.exe" if msvc else "test_bin")
            source_file.write_text(source, encoding="utf-8")

            build_res = subprocess.run(
                ([cxx, "/nologo", "/std:c++17", "/EHsc", f"/I{REPO_ROOT}", str(source_file),
                  f"/Fe{binary_file}", f"/Fo{temp_path / 'test_main.obj'}"] if msvc else
                 [cxx, "-std=c++17", f"-I{REPO_ROOT}", str(source_file), "-o", str(binary_file)]),
                capture_output=True,
                text=True,
            )
            assert build_res.returncode == 0, f"C++ compilation failed: {build_res.stdout}\n{build_res.stderr}"

            run_res = subprocess.run([str(binary_file)], capture_output=True, text=True)
            assert run_res.returncode == 0, f"C++ test execution failed: {run_res.stderr}"
