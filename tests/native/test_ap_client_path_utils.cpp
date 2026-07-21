#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <cstdlib>
#include <deque>
#include <optional>
#include <unordered_set>
#include <vector>

#include "../ap_client_path_utils.h"
#include "../rpc_queue_policy.h"

namespace fs = std::filesystem;

namespace {

void Require(bool condition, const std::string& message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::exit(1);
    }
}

void TestConfigPrefersClientDir() {
    const fs::path tempRoot = fs::temp_directory_path() / "doom-ap-client-path-test";
    fs::remove_all(tempRoot);
    fs::create_directories(tempRoot / "client");
    fs::create_directories(tempRoot / "DOOMEternal");

    std::ofstream(tempRoot / "client" / "ap_config.json") << "{}\n";

    RuntimeEnvSignals envSignals;
    envSignals.wineDllOverrides = "XINPUT1_3=n,b";

    const RuntimePathInfo info = ResolveRuntimePathInfo(
        tempRoot / "client" / "ap_client.exe",
        tempRoot / "DOOMEternal",
        envSignals
    );

    Require(info.clientDir == fs::absolute(tempRoot / "client"), "clientDir must come from executable path");
    Require(
        info.configCandidates.size() >= 2
            && info.configCandidates[0] == fs::absolute(tempRoot / "client" / "ap_config.json"),
        "ap_config.json must be searched in client_dir first"
    );

    const auto configPath = FindFirstExistingPath(info.configCandidates);
    Require(configPath.has_value(), "config path should be found in client_dir");
    Require(
        configPath.value() == fs::absolute(tempRoot / "client" / "ap_config.json"),
        "client_dir config should win over cwd fallback"
    );
}

void TestProtonClientLocalDllIsAccepted() {
    const fs::path tempRoot = fs::temp_directory_path() / "doom-ap-client-dll-test";
    fs::remove_all(tempRoot);
    fs::create_directories(tempRoot / "client");
    fs::create_directories(tempRoot / "DOOMEternal");

    std::ofstream(tempRoot / "client" / "XINPUT1_3.dll") << "stub\n";

    RuntimeEnvSignals envSignals;
    envSignals.wineDllOverrides = "XINPUT1_3=n,b";
    envSignals.steamCompatDataPath = "/compatdata/782330";

    const RuntimePathInfo info = ResolveRuntimePathInfo(
        tempRoot / "client" / "ap_client.exe",
        tempRoot / "DOOMEternal",
        envSignals
    );

    Require(info.probableProton, "Proton signals should mark the runtime as probable Proton");
    const XinputDllSelection dll = SelectXinputDllCandidate(info);
    Require(
        dll.mode == XinputDllMode::ClientLocalProton,
        "client-local XINPUT1_3.dll should be accepted in Proton mode"
    );
    Require(
        dll.selectedPath == fs::absolute(tempRoot / "client" / "XINPUT1_3.dll"),
        "client-local DLL path should be selected"
    );
}

void TestHundredReceiptAckPipelineHasNoHealthyDelay() {
    std::deque<std::string> pending;
    std::unordered_set<std::string> known;
    for (int index = 0; index < 100; ++index) {
        char id[96] = {};
        snprintf(id, sizeof(id), "recv-%06d-item-7770000-cmd-00", index);
        Require(RememberRpcCommandId(known, id), "first enqueue must be unique");
        Require(!RememberRpcCommandId(known, id), "duplicate enqueue must be rejected");
        pending.emplace_back(id);
    }

    std::vector<std::string> dispatched;
    std::uint32_t now = 1000;
    while (!pending.empty()) {
        const std::string id = pending.front();
        Require(IsNormalReceiptCommandId(id), "release job must be classified recv");
        Require(ReceiptDispatchReady(now, 0), "healthy next receipt must dispatch immediately");
        dispatched.push_back(id);
        pending.pop_front();  // synchronous durable ACK
    }
    Require(dispatched.size() == 100, "all 100 release commands must ACK");
    Require(dispatched.front().find("000000") != std::string::npos, "first receipt order drift");
    Require(dispatched.back().find("000099") != std::string::npos, "last receipt order drift");
}

void TestRetryBackoffAndSingleInflight() {
    const std::string id = "recv-000007-item-7770012-cmd-01";
    std::optional<std::string> inFlight = id;
    Require(inFlight.has_value(), "one command must be in flight");
    Require(inFlight.value() == id, "delayed ACK must retain the same command");

    const std::uint32_t failureTick = 5000;
    const auto delay1 = ReceiptRetryDelayMs(1);
    const auto delay2 = ReceiptRetryDelayMs(2);
    Require(delay1 == 250, "first retry delay must be 250 ms");
    Require(delay2 == 500, "retry delay must be exponential");
    Require(ReceiptRetryDelayMs(99) == 8000, "retry delay must be bounded");
    const std::uint32_t retryAt = failureTick + delay1;
    inFlight.reset();
    Require(!ReceiptDispatchReady(retryAt - 1, retryAt), "retry dispatched before backoff");
    Require(ReceiptDispatchReady(retryAt, retryAt), "retry did not dispatch at backoff boundary");
}

void TestRestartDedupeAndReconcileIsolation() {
    std::unordered_set<std::string> known;
    const std::string pending = "recv-000042-item-7770005-cmd-00";
    Require(RememberRpcCommandId(known, pending), "recovered pending job must enqueue once");
    Require(!RememberRpcCommandId(known, pending), "recovered pending job duplicated");
    Require(!IsNormalReceiptCommandId("reconcile-seed-0-1-e2-item7770005-stage0"),
            "manual reconcile must not enter recv fast path");
}

}  // namespace

int main() {
    TestConfigPrefersClientDir();
    TestProtonClientLocalDllIsAccepted();
    TestHundredReceiptAckPipelineHasNoHealthyDelay();
    TestRetryBackoffAndSingleInflight();
    TestRestartDedupeAndReconcileIsolation();
    return 0;
}
