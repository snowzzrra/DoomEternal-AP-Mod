#include "native/client/sentinel_command_client.h"
#include <cassert>
#include <cstdlib>

int main(int argc, char** argv) {
    assert(argc == 2);
    SentinelCommandClient client;
    client.SetTargetProcess(std::strtoul(argv[1], nullptr, 10));
    assert(client.PollHealth());
    assert(client.ExecuteConsoleCommand("g_infiniteAmmo 0"));
    assert(client.LastResult() == AP_RPC_DELIVERED);
    assert(!client.ExecuteConsoleCommand("g_infiniteAmmo 0; give health"));
}
