"""Tests for 32-bit tick deadline and sentinel timing semantics in the native AP client."""

import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def tick_reached(now: int, deadline: int) -> bool:
    """Signed wrap-safe deadline comparison matching C++ static_cast<LONG>(now - deadline) >= 0."""
    diff = (now - deadline) & 0xFFFFFFFF
    if diff >= 0x80000000:
        diff -= 0x100000000
    return diff >= 0


class HealthPollState:
    """Timing state matching ApRuntimeRpcClient::PollHealth."""

    def __init__(self, interval_ms: int = 1000):
        self.interval_ms = interval_ms
        self.has_next_health_tick = False
        self.next_health_tick = 0

    def should_poll(self, now: int) -> bool:
        if self.has_next_health_tick and not tick_reached(now, self.next_health_tick):
            return False
        self.has_next_health_tick = True
        self.next_health_tick = (now + self.interval_ms) & 0xFFFFFFFF
        return True


def receipt_dispatch_ready(now: int, next_attempt: int) -> bool:
    """Dispatch readiness check matching ReceiptDispatchReady in rpc_queue_policy.h."""
    if next_attempt == 0:
        return True
    return tick_reached(now, next_attempt)


@pytest.mark.unit
class TestNativeRpcTimingSemantics:
    def test_unpatched_sentinel_fails_on_high_bit_tick(self):
        """Demonstrates that an unpatched deadline of 0 evaluates false for high-bit ticks."""
        pilzkopf_tick = 0xEE000000
        assert not tick_reached(pilzkopf_tick, 0), (
            "Signed difference (0xEE000000 - 0) cast to int32 must be negative, reproducing the freeze."
        )

    @pytest.mark.parametrize(
        "tick",
        [
            0,
            100,
            1000,
            0x7FFFFFFE,
            0x7FFFFFFF,
            0x80000000,
            0x80000001,
            0xEE000000,
            0xFFFFFFFE,
            0xFFFFFFFF,
        ],
    )
    def test_first_poll_executes_across_representative_ticks(self, tick: int):
        """First PollHealth invocation must be eligible immediately across representative 32-bit ticks."""
        state = HealthPollState(interval_ms=1000)
        assert state.should_poll(tick), f"First poll must execute immediately at tick 0x{tick:08X}"

    def test_subsequent_poll_throttling_and_execution(self):
        """Subsequent polls remain throttled until interval elapses."""
        now = 0xEE000000
        state = HealthPollState(interval_ms=1000)

        # First poll executes immediately
        assert state.should_poll(now)

        # Immediate repeat calls are throttled
        assert not state.should_poll((now + 100) & 0xFFFFFFFF)
        assert not state.should_poll((now + 500) & 0xFFFFFFFF)
        assert not state.should_poll((now + 999) & 0xFFFFFFFF)

        # Poll executes once interval is reached
        assert state.should_poll((now + 1000) & 0xFFFFFFFF)

    def test_dword_wrap_relative_deadline(self):
        """Post-wrap low tick values evaluate correctly against relative deadlines."""
        now = 0xFFFFFFFE
        state = HealthPollState(interval_ms=1000)

        # First poll at 0xFFFFFFFE schedules next tick for 998 (0x000003E6)
        assert state.should_poll(now)
        expected_deadline = (0xFFFFFFFE + 1000) & 0xFFFFFFFF
        assert expected_deadline == 998

        # Post-wrap tick before deadline does not trigger
        assert not state.should_poll(100)
        assert not state.should_poll(500)
        assert not state.should_poll(997)

        # Post-wrap tick at or after deadline triggers
        assert state.should_poll(998)

    @pytest.mark.parametrize(
        "tick",
        [
            0,
            100,
            0x7FFFFFFF,
            0x80000000,
            0xEE000000,
            0xFFFFFFFF,
        ],
    )
    def test_receipt_dispatch_ready_initial_sentinel(self, tick: int):
        """ReceiptDispatchReady with nextAttempt=0 must evaluate true immediately for any tick."""
        assert receipt_dispatch_ready(tick, 0), f"Initial dispatch must be ready at tick 0x{tick:08X}"

    def test_receipt_dispatch_ready_scheduled_retry_and_wrap(self):
        """Scheduled retry delay and wrap handling in ReceiptDispatchReady."""
        now = 0xFFFFFFFE
        delay = 250
        deadline = (now + delay) & 0xFFFFFFFF
        assert deadline == 248

        # Before deadline (post-wrap)
        assert not receipt_dispatch_ready(100, deadline)
        # At deadline
        assert receipt_dispatch_ready(248, deadline)
        # After deadline
        assert receipt_dispatch_ready(300, deadline)

    def test_cpp_queue_policy_native_execution(self):
        """Compile and execute rpc_queue_policy.h against native C++ types if g++ is available."""
        cxx = shutil.which("g++")
        if not cxx:
            pytest.skip("g++ not available on host")

        source = """#include <cassert>
#include <cstdint>
#include "rpc_queue_policy.h"

static bool TickReached(std::uint32_t now, std::uint32_t deadline) {
    return static_cast<std::int32_t>(now - deadline) >= 0;
}

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

    // Health poll model with has_next_health_tick
    bool has_next_health_tick = false;
    std::uint32_t next_health_tick = 0;

    auto should_poll = [&](std::uint32_t now) -> bool {
        if (has_next_health_tick && !TickReached(now, next_health_tick)) {
            return false;
        }
        has_next_health_tick = true;
        next_health_tick = now + 1000;
        return true;
    };

    assert(should_poll(pilzkopf));
    assert(!should_poll(pilzkopf + 100));
    assert(should_poll(pilzkopf + 1000));

    return 0;
}
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_file = temp_path / "test_main.cpp"
            binary_file = temp_path / "test_bin"
            source_file.write_text(source, encoding="utf-8")

            build_res = subprocess.run(
                [cxx, "-std=c++17", f"-I{REPO_ROOT}", str(source_file), "-o", str(binary_file)],
                capture_output=True,
                text=True,
            )
            assert build_res.returncode == 0, f"C++ compilation failed: {build_res.stderr}"

            run_res = subprocess.run([str(binary_file)], capture_output=True, text=True)
            assert run_res.returncode == 0, f"C++ test execution failed: {run_res.stderr}"
