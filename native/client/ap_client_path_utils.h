#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <vector>

struct RuntimeEnvSignals {
    std::string wineDllOverrides;
    std::string winePrefix;
    std::string steamCompatDataPath;
    std::string steamCompatClientInstallPath;
    std::string protonLog;
};

struct RuntimePathInfo {
    std::filesystem::path executablePath;
    std::filesystem::path clientDir;
    std::filesystem::path workingDir;
    std::filesystem::path gameRootDir;
    std::vector<std::filesystem::path> configCandidates;
    bool probableProton = false;
    std::vector<std::string> protonSignals;
};

RuntimePathInfo ResolveRuntimePathInfo(
    const std::filesystem::path& executablePath,
    const std::filesystem::path& workingDir,
    const RuntimeEnvSignals& envSignals
);

std::optional<std::filesystem::path> FindFirstExistingPath(
    const std::vector<std::filesystem::path>& candidates
);
