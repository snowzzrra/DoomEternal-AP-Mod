#include "ap_client_path_utils.h"

#include <algorithm>
#include <cctype>

namespace {

std::filesystem::path AbsoluteOrOriginal(const std::filesystem::path& path) {
    std::error_code error;
    const std::filesystem::path absolute = std::filesystem::absolute(path, error);
    return error ? path : absolute;
}

std::string Lowercase(std::string value) {
    std::transform(
        value.begin(),
        value.end(),
        value.begin(),
        [](unsigned char character) { return static_cast<char>(std::tolower(character)); }
    );
    return value;
}

bool ContainsCaseInsensitive(const std::string& haystack, const std::string& needle) {
    return Lowercase(haystack).find(Lowercase(needle)) != std::string::npos;
}

bool IsLikelyWineDrivePath(const std::filesystem::path& path) {
    const std::string value = path.string();
    return value.size() >= 3
        && std::isalpha(static_cast<unsigned char>(value[0])) != 0
        && value[1] == ':'
        && (value[2] == '\\' || value[2] == '/');
}

bool PathsEqual(const std::filesystem::path& left, const std::filesystem::path& right) {
    return AbsoluteOrOriginal(left).lexically_normal().string()
        == AbsoluteOrOriginal(right).lexically_normal().string();
}

void AddUniquePath(
    std::vector<std::filesystem::path>& paths,
    const std::filesystem::path& candidate
) {
    for (const std::filesystem::path& existing : paths) {
        if (PathsEqual(existing, candidate)) {
            return;
        }
    }
    paths.push_back(candidate);
}

std::filesystem::path InferGameRoot(const std::filesystem::path& workingDir) {
    const std::string leaf = Lowercase(workingDir.filename().string());
    if (leaf == "base" && workingDir.has_parent_path()) {
        return workingDir.parent_path();
    }
    return workingDir;
}

}  // namespace

RuntimePathInfo ResolveRuntimePathInfo(
    const std::filesystem::path& executablePath,
    const std::filesystem::path& workingDir,
    const RuntimeEnvSignals& envSignals
) {
    RuntimePathInfo info;
    info.executablePath = AbsoluteOrOriginal(executablePath);
    info.clientDir = info.executablePath.parent_path();
    info.workingDir = AbsoluteOrOriginal(workingDir);
    info.gameRootDir = AbsoluteOrOriginal(InferGameRoot(info.workingDir));
    info.gameRootDllCandidate = info.gameRootDir / "XINPUT1_3.dll";
    info.clientDllCandidate = info.clientDir / "XINPUT1_3.dll";

    AddUniquePath(info.configCandidates, info.clientDir / "ap_config.json");
    AddUniquePath(info.configCandidates, info.workingDir / "ap_config.json");

    if (ContainsCaseInsensitive(envSignals.wineDllOverrides, "xinput1_3")) {
        info.probableProton = true;
        info.protonSignals.push_back("WINEDLLOVERRIDES contains XINPUT1_3");
    }
    if (!envSignals.winePrefix.empty()) {
        info.probableProton = true;
        info.protonSignals.push_back("WINEPREFIX is set");
    }
    if (!envSignals.steamCompatDataPath.empty()) {
        info.probableProton = true;
        info.protonSignals.push_back("STEAM_COMPAT_DATA_PATH is set");
    }
    if (!envSignals.steamCompatClientInstallPath.empty()) {
        info.probableProton = true;
        info.protonSignals.push_back("STEAM_COMPAT_CLIENT_INSTALL_PATH is set");
    }
    if (!envSignals.protonLog.empty()) {
        info.probableProton = true;
        info.protonSignals.push_back("PROTON_LOG is set");
    }
    if (!PathsEqual(info.clientDir, info.gameRootDir)) {
        info.probableProton = true;
        info.protonSignals.push_back("client_dir is separate from the game root");
    }
    if (IsLikelyWineDrivePath(info.executablePath) || IsLikelyWineDrivePath(info.workingDir)) {
        info.probableProton = true;
        info.protonSignals.push_back("Wine-style drive path detected");
    }

    return info;
}

std::optional<std::filesystem::path> FindFirstExistingPath(
    const std::vector<std::filesystem::path>& candidates
) {
    for (const std::filesystem::path& candidate : candidates) {
        std::error_code error;
        if (std::filesystem::is_regular_file(candidate, error)) {
            return candidate;
        }
    }
    return std::nullopt;
}

XinputDllSelection SelectXinputDllCandidate(const RuntimePathInfo& runtimePaths) {
    std::error_code error;
    if (std::filesystem::is_regular_file(runtimePaths.gameRootDllCandidate, error)) {
        return {XinputDllMode::GameRoot, runtimePaths.gameRootDllCandidate};
    }
    error.clear();
    if (
        runtimePaths.probableProton
        && std::filesystem::is_regular_file(runtimePaths.clientDllCandidate, error)
    ) {
        return {XinputDllMode::ClientLocalProton, runtimePaths.clientDllCandidate};
    }
    return {};
}
