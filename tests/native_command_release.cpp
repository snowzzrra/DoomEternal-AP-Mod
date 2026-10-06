#undef NDEBUG
#include "sentinel_command_client.h"
#include "sentinel_version.h"
#include <cassert>
#include <cstring>

namespace {
unsigned retained = 0, released = 0;
uint32_t terminal = SC_DIAGNOSTIC_EXECUTED;
sc_command_request submitted{};
const char* nativeVersion = SC_PRODUCT_VERSION;
sentinel::ProbeResult nativeResult = sentinel::ProbeResult::ok;
uint32_t nativeAvailability = SC_NATIVE_ENABLED;
}
namespace sentinel {
Inspection query_native(uint32_t, uint32_t, uint64_t) {
    Inspection reply{};
    reply.result = nativeResult;
    std::strcpy(reply.snapshot.core.version, nativeVersion);
    reply.native.availability = nativeAvailability;
    return reply;
}
Inspection query_save_admission(uint32_t, uint32_t) {
    Inspection reply{};
    reply.result = ProbeResult::ok;
    reply.admission.state = SC_SAVE_SESSION_ADMITTED;
    reply.admission.flags = SC_SAVE_SESSION_ACCEPTING;
    std::memset(reply.admission.namespace_id, 'a', 64);
    return reply;
}
Inspection query_command(uint32_t, uint32_t, uint16_t operation, const sc_command_request& request) {
    Inspection reply{};
    reply.result = ProbeResult::ok;
    reply.command.execution.state = terminal;
    reply.command.outcome = SC_COMMAND_DISPATCHED;
    if (operation == command_submit_operation) {
        assert(retained < SC_DIAGNOSTIC_CAPACITY);
        submitted = request;
        ++retained;
    } else if (operation == command_release_operation) {
        assert(retained && std::memcmp(&submitted, &request, sizeof(request)) == 0);
        --retained;
        ++released;
    } else assert(false);
    return reply;
}
}
int main() {
    SentinelCommandClient client;
    client.SetTargetProcess(42);
    for (const char* version : {"1.0.0", "1.0.1", "1.0.2", "1.0.3", "1.0.4", "1.1.0", "1.0.0-rc-3", "1.0.1-rc-1", "1.0.2-rc-10", "1.0.3-rc-1"}) {
        nativeVersion = version;
        assert(client.PollHealth() && client.Ready() && client.LastResult() == AP_RPC_DELIVERED);
        const auto before = released;
        assert(client.ExecuteConsoleCommand("give ammo"));
        assert(retained == 0 && released == before + 1 && submitted.kind == SC_COMMAND_AMMO_REFILL);
    }
    nativeVersion = SC_PRODUCT_VERSION;
    assert(client.ExecuteConsoleCommand("ai_ScriptCmdEnt AP_DEATHLINK_KILL activate player1"));
    assert(retained == 0 && submitted.kind == SC_COMMAND_ACTIVATE);
    nativeResult = sentinel::ProbeResult::endpoint_absent;
    assert(!client.PollHealth() && client.LastResult() == AP_RPC_PIPE_MISSING);
    nativeResult = sentinel::ProbeResult::ok;
    nativeAvailability = SC_NATIVE_DISABLED;
    assert(!client.PollHealth() && !client.Ready());
    nativeAvailability = SC_NATIVE_ENABLED;
    assert(client.PollHealth() && client.Ready());
    released = 0;
    for (unsigned i = 0; i < SC_DIAGNOSTIC_CAPACITY * 3; ++i) {
        assert(client.ExecuteConsoleCommand("echo receipt"));
        assert(retained == 0 && released == i + 1);
    }
    terminal = SC_DIAGNOSTIC_REJECTED;
    assert(!client.ExecuteConsoleCommand("echo refused"));
    assert(retained == 0 && released == SC_DIAGNOSTIC_CAPACITY * 3 + 1);
    client.SetTargetProcess(0);
    assert(!client.PollHealth() && !client.Ready() && client.LastResult() == AP_RPC_PIPE_MISSING);
}
