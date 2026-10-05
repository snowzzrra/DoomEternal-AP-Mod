#include "native/client/command_queue.h"
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <vector>

class FakeTransport : public CommandTransport {
public:
    std::vector<std::string> commands;
    bool succeed = true;
    ApRpcResult failure = AP_RPC_PIPE_MISSING;
    std::string deletionLockPath;
    HANDLE deletionLock = INVALID_HANDLE_VALUE;
    bool Ready() const override { return true; }
    bool ExecuteConsoleCommand(const std::string& command) override {
        commands.push_back(command);
        if (!deletionLockPath.empty()) deletionLock = CreateFileA(deletionLockPath.c_str(), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
        return succeed;
    }
    void SetCurrentCommandId(const std::string&) override {}
    ApRpcResult LastResult() const override { return succeed ? AP_RPC_DELIVERED : failure; }
    DWORD LastTransportStatus() const override { return 0; }
};

static void Write(const std::string& path, const std::string& text) {
    std::ofstream stream(path, std::ios::binary);
    stream << text;
    stream.close();
    assert(stream.good());
}

static void Scope(const char* value = "0123456789abcdef") {
    Write(kQueueSessionNamespacePath, value);
}

static bool Dispatch(NativeCommandQueue& queue, FakeTransport& transport,
                     QueueSafetySnapshot safety = {true, true}, bool baseline = true) {
    return queue.Dispatch(GetTickCount(), queue.ArmIfPending(), true, safety, baseline, "ready", &transport);
}

int main() {
    // The harness runs only in pytest's isolated cwd; it never discovers a game path.
    std::filesystem::create_directories("base/ap_queue");
    std::vector<std::string> logs;
    const auto log = [&](const std::string& line) { logs.push_back(line); };
    FakeTransport transport;
    {
        NativeCommandQueue queue(log); queue.Initialize(); Scope();
        const std::string id = "base/ap_queue/recv-0123456789abcdef-fortress-open-reader";
        Write(id + ".cmd", "ai_ScriptCmdEnt ap_fortress_phase_4 activate player1\n");
        queue.Import();
        const HANDLE reader = CreateFileA((id + ".processing").c_str(), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
        assert(reader != INVALID_HANDLE_VALUE);
        Write(id + ".consumer.tmp", "AP_NATIVE_ATTEMPT_V1\n");
        assert(!MoveFileExA((id + ".consumer.tmp").c_str(), (id + ".processing").c_str(),
                           MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH));
        const DWORD replaceError = GetLastError();
        std::printf("Win32 replacement with retained read handle: winerror=%lu\n", replaceError);
        assert(replaceError == ERROR_ACCESS_DENIED);
        assert(DeleteFileA((id + ".consumer.tmp").c_str()));
        Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        assert(std::filesystem::exists(id + ".processing.attempt"));
        Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        CloseHandle(reader);
        Sleep(1050); queue.Import();
        assert(!std::filesystem::exists(id + ".processing"));
        assert(!std::filesystem::exists(id + ".processing.attempt"));
        transport.commands.clear();
    }
    {
        NativeCommandQueue queue(log); queue.Initialize(); Scope();
        const std::string id = "base/ap_queue/recv-0123456789abcdef-persist-denied";
        Write(id + ".cmd", "give ammo\n"); queue.Import();
        assert(CreateDirectoryA((id + ".processing.attempt").c_str(), nullptr));
        Dispatch(queue, transport);
        assert(transport.commands.empty() && queue.PublicationBlocked());
        assert(queue.PublicationError() == ERROR_ACCESS_DENIED);
        assert(RemoveDirectoryA((id + ".processing.attempt").c_str()));
        Sleep(260); Dispatch(queue, transport);
        assert(transport.commands.size() == 1 && !queue.PublicationBlocked());
        transport.commands.clear();
    }
    {
        NativeCommandQueue queue(log); queue.Initialize(); Scope();
        const std::string id = "base/ap_queue/recv-0123456789abcdef-held-journal";
        Write(id + ".cmd", "give ammo\n"); queue.Import();
        transport.deletionLockPath = id + ".processing.attempt";
        Dispatch(queue, transport);
        assert(transport.commands.size() == 1 && !std::filesystem::exists(id + ".processing"));
        assert(std::filesystem::exists(id + ".processing.attempt"));
        Write(id + ".cmd", "give ammo\n"); queue.Import(); Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        CloseHandle(transport.deletionLock); transport.deletionLockPath.clear();
        Sleep(1050); queue.Import();
        assert(!std::filesystem::exists(id + ".processing.attempt"));
        transport.commands.clear();
    }
    {
        NativeCommandQueue queue(log); queue.Initialize(); Scope();
        Write(kMaterializationLeasePath, "AP_MATERIALIZATION_LEASE_V1 1:2\n");
        const std::string headers = "AP_EXECUTION_CLASS_V1 MAP_ENTITY_SAFE\n"
            "AP_MAP_ENTITY_OPERATION_V1 FORTRESS_LAYER\nAP_MATERIALIZATION_LEASE_V1 1:2\n";
        const std::string key = "base/ap_queue/recv-0123456789abcdef-fortress-";
        Write(key + "phase2.cmd", "AP_MATERIALIZATION_LEASE_V1 1:2\n"
              "ai_ScriptCmdEnt ap_fortress_phase_2 activate player1\n");
        Write(key + "phase4.cmd", headers + "ai_ScriptCmdEnt ap_fortress_phase_4 activate player1\n");
        queue.Import(); queue.DiscardInvalidScopes(true); Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        assert(transport.commands.back() == "ai_ScriptCmdEnt ap_fortress_phase_4 activate player1");
        assert(std::filesystem::exists(key + "phase4.result"));
        std::ifstream terminal(key + "phase4.result");
        std::string outcome;
        std::getline(terminal, outcome);
        assert(outcome == "outcome=command_consumed_unverified");
        assert(!std::filesystem::exists(key + "phase2.processing"));
        Write(key + "phase3.cmd", headers + "ai_ScriptCmdEnt ap_fortress_phase_3 activate player1\n");
        queue.Import(); queue.DiscardInvalidScopes(true); Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        const std::string crashed = "base/ap_queue/recv-0123456789abcdef-attempt-crash";
        Write(crashed + ".processing", "give ammo\n");
        Write(crashed + ".processing.attempt", "partial attempt record");
        queue.Import(); assert(std::filesystem::exists(crashed + ".failed"));
        assert(transport.commands.size() == 1);
        transport.commands.clear();
    }
    {
        NativeCommandQueue queue(log);
        queue.Initialize();
        Scope();
        const std::string id = "base/ap_queue/recv-0123456789abcdef-0-item-7770001-cmd-0";
        Write(id + ".cmd", "ai_ScriptCmdEnt ap_rpc_v3_7770001 activate\n");
        queue.Import();
        assert(std::filesystem::exists(id + ".processing"));
        assert(!std::filesystem::exists(id + ".cmd"));
        Dispatch(queue, transport, {false, false});
        assert(transport.commands.empty());

        // Lock deletion but permit reads. Execution succeeds once; later passes only remove.
        transport.deletionLockPath = id + ".processing";
        Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        queue.Import();
        Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        assert(std::filesystem::exists(id + ".processing"));
        assert(transport.deletionLock != INVALID_HANDLE_VALUE);
        CloseHandle(transport.deletionLock);
        transport.deletionLockPath.clear();
        Sleep(1050);
        queue.Import();
        Dispatch(queue, transport);
        assert(!std::filesystem::exists(id + ".processing"));
        assert(transport.commands.size() == 1);

        // A foreign room is held; changing room after import cannot execute its job.
        const std::string foreign = "base/ap_queue/recv-fedcba9876543210-1-item-7770002-cmd-0";
        Write(foreign + ".cmd", "ai_ScriptCmdEnt ap_rpc_v3_7770002 activate\n");
        queue.Import();
        assert(std::filesystem::exists(foreign + ".cmd"));
        Scope("fedcba9876543210");
        queue.Import();
        Scope();
        Dispatch(queue, transport);
        assert(transport.commands.size() == 1);
        assert(std::filesystem::exists(foreign + ".processing"));
    }
    {
        // Restart preserves processing until a matching bridge room is published.
        NativeCommandQueue queue(log);
        queue.Initialize();
        queue.Import();
        assert(transport.commands.size() == 1);
        Scope("fedcba9876543210");
        queue.Import();
        Dispatch(queue, transport);
        assert(transport.commands.size() == 2);

        // First eligible job may bypass an unsafe player job: this is not global FIFO.
        Write("base/ap_queue/recv-fedcba9876543210-2-item-7770003-cmd-0.cmd",
              "ai_ScriptCmdEnt ap_rpc_v3_7770003 activate\n");
        Write("base/ap_queue/z-diagnostic.cmd",
              "AP_DIAGNOSTIC_CONDUMP_V1 AP_SUPPORT_FILE.txt\ncondump AP_SUPPORT_FILE.txt\n");
        queue.Import();
        Dispatch(queue, transport, {false, false});
        assert(transport.commands.back() == "condump AP_SUPPORT_FILE.txt");
        assert(std::filesystem::exists("base/ap_queue/recv-fedcba9876543210-2-item-7770003-cmd-0.processing"));
        Dispatch(queue, transport);
        assert(transport.commands.back() == "ai_ScriptCmdEnt ap_rpc_v3_7770003 activate");

        const auto beforeRetry = transport.commands.size();
        Write("base/ap_queue/recv-fedcba9876543210-3-item-7770004-cmd-0.cmd",
              "ai_ScriptCmdEnt ap_rpc_v3_7770004 activate\n");
        queue.Import();
        transport.succeed = false;
        Dispatch(queue, transport);
        Dispatch(queue, transport);
        assert(transport.commands.size() == beforeRetry + 1);
        Sleep(260);
        transport.succeed = true;
        Dispatch(queue, transport);
        assert(transport.commands.size() == beforeRetry + 2);
        assert(!std::filesystem::exists("base/ap_queue/recv-fedcba9876543210-3-item-7770004-cmd-0.processing"));

        const std::string mapHeaders = "AP_EXECUTION_CLASS_V1 MAP_ENTITY_SAFE\n"
            "AP_MAP_ENTITY_OPERATION_V1 FAST_TRAVEL_UNLOCK\nAP_MATERIALIZATION_LEASE_V1 1:2\n";
        Write(kMaterializationLeasePath, "AP_MATERIALIZATION_LEASE_V1 1:2\n");
        Write("base/ap_queue/recv-fedcba9876543210-map.cmd",
              mapHeaders + "ai_ScriptCmdEnt ap_fast_travel_unlock activate\n");
        queue.Import();
        const auto beforeMap = transport.commands.size();
        Sleep(260); // Nonreceipt maintenance retains its existing 250 ms spacing.
        Dispatch(queue, transport, {false, false});
        assert(transport.commands.size() == beforeMap);
        Dispatch(queue, transport, {false, true});
        assert(transport.commands.size() == beforeMap + 1);
        Write("base/ap_queue/recv-fedcba9876543210-stale-map.cmd",
              mapHeaders + "ai_ScriptCmdEnt ap_fast_travel_unlock activate\n");
        queue.Import();
        Write(kMaterializationLeasePath, "AP_MATERIALIZATION_LEASE_V1 1:3\n");
        queue.DiscardInvalidScopes(false);
        Dispatch(queue, transport);
        assert(transport.commands.size() == beforeMap + 1);
        assert(!std::filesystem::exists("base/ap_queue/recv-fedcba9876543210-stale-map.processing"));

        Write(kTransientScopePath, "effectscope-0123456789abcdef");
        Write("base/ap_queue/recv-fedcba9876543210-effect.cmd",
              "AP_EXECUTION_CLASS_V1 TRANSIENT_EFFECT\n"
              "AP_TRANSIENT_SCOPE_V1 effectscope-0123456789abcdef\ng_infiniteAmmo 1\n");
        queue.Import();
        Dispatch(queue, transport, {true, true}, false);
        assert(transport.commands.size() == beforeMap + 1);
        Sleep(260);
        Dispatch(queue, transport, {true, true}, true);
        assert(transport.commands.back() == "g_infiniteAmmo 1");
    }
    {
        NativeCommandQueue queue(log); queue.Initialize(); Scope("fedcba9876543210"); queue.Import();
        const std::string key = "base/ap_queue/recv-fedcba9876543210-unknown";
        Write(key + ".cmd", "give ammo\n"); queue.Import();
        transport.succeed = false; transport.failure = AP_RPC_AMBIGUOUS;
        const auto attempts = transport.commands.size();
        Dispatch(queue, transport); Sleep(260); Dispatch(queue, transport);
        assert(transport.commands.size() == attempts + 1);
        assert(std::filesystem::exists(key + ".failed"));
        Write(key + ".cmd", "give ammo\n"); queue.Import(); Dispatch(queue, transport);
        assert(transport.commands.size() == attempts + 1);
        const std::string crash = "base/ap_queue/recv-fedcba9876543210-crash";
        Write(crash + ".processing", "AP_NATIVE_ATTEMPT_V1\ngive ammo\n");
        queue.Import(); assert(std::filesystem::exists(crash + ".failed"));
        assert(transport.commands.size() == attempts + 1);
    }
    assert(std::any_of(logs.begin(), logs.end(), [](const std::string& line) {
        return line.find("ACK_REMOVE_RETRY command_id=") != std::string::npos;
    }));
    assert(std::none_of(logs.begin(), logs.end(), [](const std::string& line) {
        return line.find("result=verified") != std::string::npos;
    }));
    return 0;
}
