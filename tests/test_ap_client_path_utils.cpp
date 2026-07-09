#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <cstdlib>

#include "../ap_client_path_utils.h"

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

}  // namespace

int main() {
    TestConfigPrefersClientDir();
    TestProtonClientLocalDllIsAccepted();
    return 0;
}
