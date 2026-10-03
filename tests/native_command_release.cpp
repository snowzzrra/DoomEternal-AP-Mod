#include "sentinel_command_client.h"
#include <cassert>
#include <cstring>

namespace {
unsigned retained = 0, released = 0;
uint32_t terminal = SC_DIAGNOSTIC_EXECUTED;
sc_command_request submitted{};
}
namespace sentinel {
Inspection query_native(uint32_t, uint32_t, uint64_t) {
    Inspection reply{};
    reply.result = ProbeResult::ok;
    std::strcpy(reply.snapshot.core.version, "1.0.0-rc-3");
    reply.native.availability = SC_NATIVE_ENABLED;
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
    for (unsigned i = 0; i < SC_DIAGNOSTIC_CAPACITY * 3; ++i) {
        assert(client.ExecuteConsoleCommand("echo receipt"));
        assert(retained == 0 && released == i + 1);
    }
    terminal = SC_DIAGNOSTIC_REJECTED;
    assert(!client.ExecuteConsoleCommand("echo refused"));
    assert(retained == 0 && released == SC_DIAGNOSTIC_CAPACITY * 3 + 1);
}
