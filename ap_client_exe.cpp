#include <windows.h>
#include <bcrypt.h>
#include <io.h>
#include <stdio.h>
#include <algorithm>
#include <array>
#include <deque>
#include <filesystem>
#include <fstream>
#include <optional>
#include <regex>
#include <string>
#include <vector>
#include "game_state_probe.h"
#include "mhclient.h"

MeathookInterface* g_MhInterface = nullptr;

static const char* kQueueDirectory = "base\\ap_queue";
static const char* kRpcGatePath = "base\\ap_rpc_enabled";
static const char* kGoalEventPath = "base\\ap_transition_e1m3_cult_to_e1m4_boss.evt";
static const char* kCultistBaseMap = "game/sp/e1m3_cult/e1m3_cult";
static const char* kDoomHunterBaseMap = "game/sp/e1m4_boss/e1m4_boss";
static const ULONGLONG kSteamId64Base = 76561197960265728ULL;
static const DWORD kCommandSpacingMs = 250;
static const DWORD kGoalMonitorPollMs = 1000;

struct CommandJob {
    std::string path;
    std::string command;
};

struct SaveSnapshot {
    std::string path;
    std::string mapName;
    long long mtimeToken = 0;
};

void LogDebug(const std::string& message) {
    SYSTEMTIME now = {};
    GetLocalTime(&now);
    char timestamp[32] = {};
    snprintf(
        timestamp,
        sizeof(timestamp),
        "%04u-%02u-%02u %02u:%02u:%02u.%03u",
        now.wYear, now.wMonth, now.wDay,
        now.wHour, now.wMinute, now.wSecond, now.wMilliseconds
    );
    printf("[%s] %s\n", timestamp, message.c_str());
    FILE* file = fopen("base\\ap_client.log", "a");
    if (file) {
        fprintf(file, "[%s] %s\n", timestamp, message.c_str());
        fclose(file);
    }
}

std::string TrimLine(std::string value) {
    while (!value.empty() && (value.back() == '\n' || value.back() == '\r' || value.back() == '\0')) {
        value.pop_back();
    }
    return value;
}

std::string ReadTextFile(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        return {};
    }
    return std::string(
        (std::istreambuf_iterator<char>(input)),
        std::istreambuf_iterator<char>()
    );
}

std::string JsonUnescape(const std::string& value) {
    std::string result;
    result.reserve(value.size());
    bool escaped = false;
    for (char character : value) {
        if (!escaped) {
            if (character == '\\') {
                escaped = true;
            } else {
                result.push_back(character);
            }
            continue;
        }

        switch (character) {
            case '\\':
            case '"':
            case '/':
                result.push_back(character);
                break;
            case 'b':
                result.push_back('\b');
                break;
            case 'f':
                result.push_back('\f');
                break;
            case 'n':
                result.push_back('\n');
                break;
            case 'r':
                result.push_back('\r');
                break;
            case 't':
                result.push_back('\t');
                break;
            default:
                result.push_back(character);
                break;
        }
        escaped = false;
    }
    if (escaped) {
        result.push_back('\\');
    }
    return result;
}

std::optional<std::string> ExtractJsonString(const std::string& json, const std::string& key) {
    const std::regex pattern(
        "\"" + key + "\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\""
    );
    std::smatch match;
    if (!std::regex_search(json, match, pattern)) {
        return std::nullopt;
    }
    return JsonUnescape(match[1].str());
}

std::optional<unsigned long long> ExtractJsonUnsigned(
    const std::string& json,
    const std::string& key
) {
    const std::regex pattern("\"" + key + "\"\\s*:\\s*(\\d+)");
    std::smatch match;
    if (!std::regex_search(json, match, pattern)) {
        return std::nullopt;
    }
    try {
        return std::stoull(match[1].str());
    } catch (...) {
        return std::nullopt;
    }
}

bool CryptoSucceeded(NTSTATUS status) {
    return status >= 0;
}

bool ComputeSha256(
    const std::string& input,
    std::array<unsigned char, 32>& digest
) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD objectLength = 0;
    DWORD bytesCopied = 0;
    NTSTATUS status = BCryptOpenAlgorithmProvider(
        &algorithm,
        BCRYPT_SHA256_ALGORITHM,
        nullptr,
        0
    );
    if (!CryptoSucceeded(status)) {
        return false;
    }

    std::vector<unsigned char> hashObject;
    status = BCryptGetProperty(
        algorithm,
        BCRYPT_OBJECT_LENGTH,
        reinterpret_cast<PUCHAR>(&objectLength),
        sizeof(objectLength),
        &bytesCopied,
        0
    );
    if (!CryptoSucceeded(status) || objectLength == 0) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
        return false;
    }

    hashObject.resize(objectLength);
    status = BCryptCreateHash(
        algorithm,
        &hash,
        hashObject.data(),
        static_cast<ULONG>(hashObject.size()),
        nullptr,
        0,
        0
    );
    if (!CryptoSucceeded(status)) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
        return false;
    }

    status = BCryptHashData(
        hash,
        reinterpret_cast<PUCHAR>(const_cast<char*>(input.data())),
        static_cast<ULONG>(input.size()),
        0
    );
    if (CryptoSucceeded(status)) {
        status = BCryptFinishHash(
            hash,
            digest.data(),
            static_cast<ULONG>(digest.size()),
            0
        );
    }

    BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    return CryptoSucceeded(status);
}

bool Aes128GcmDecrypt(
    const std::array<unsigned char, 16>& key,
    const std::vector<unsigned char>& nonce,
    const std::vector<unsigned char>& ciphertext,
    const std::vector<unsigned char>& tag,
    const std::string& aad,
    std::vector<unsigned char>& plaintext
) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_KEY_HANDLE keyHandle = nullptr;
    DWORD objectLength = 0;
    DWORD bytesCopied = 0;

    NTSTATUS status = BCryptOpenAlgorithmProvider(
        &algorithm,
        BCRYPT_AES_ALGORITHM,
        nullptr,
        0
    );
    if (!CryptoSucceeded(status)) {
        return false;
    }

    status = BCryptSetProperty(
        algorithm,
        BCRYPT_CHAINING_MODE,
        reinterpret_cast<PUCHAR>(const_cast<wchar_t*>(BCRYPT_CHAIN_MODE_GCM)),
        static_cast<ULONG>((wcslen(BCRYPT_CHAIN_MODE_GCM) + 1) * sizeof(wchar_t)),
        0
    );
    if (!CryptoSucceeded(status)) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
        return false;
    }

    status = BCryptGetProperty(
        algorithm,
        BCRYPT_OBJECT_LENGTH,
        reinterpret_cast<PUCHAR>(&objectLength),
        sizeof(objectLength),
        &bytesCopied,
        0
    );
    if (!CryptoSucceeded(status) || objectLength == 0) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
        return false;
    }

    std::vector<unsigned char> keyObject(objectLength);
    status = BCryptGenerateSymmetricKey(
        algorithm,
        &keyHandle,
        keyObject.data(),
        static_cast<ULONG>(keyObject.size()),
        const_cast<PUCHAR>(key.data()),
        static_cast<ULONG>(key.size()),
        0
    );
    if (!CryptoSucceeded(status)) {
        BCryptCloseAlgorithmProvider(algorithm, 0);
        return false;
    }

    BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO authInfo;
    BCRYPT_INIT_AUTH_MODE_INFO(authInfo);
    authInfo.pbNonce = const_cast<PUCHAR>(nonce.data());
    authInfo.cbNonce = static_cast<ULONG>(nonce.size());
    authInfo.pbAuthData = reinterpret_cast<PUCHAR>(const_cast<char*>(aad.data()));
    authInfo.cbAuthData = static_cast<ULONG>(aad.size());
    authInfo.pbTag = const_cast<PUCHAR>(tag.data());
    authInfo.cbTag = static_cast<ULONG>(tag.size());

    std::vector<unsigned char> ciphertextCopy = ciphertext;
    ULONG plaintextSize = 0;
    status = BCryptDecrypt(
        keyHandle,
        ciphertextCopy.data(),
        static_cast<ULONG>(ciphertextCopy.size()),
        &authInfo,
        nullptr,
        0,
        nullptr,
        0,
        &plaintextSize,
        0
    );
    if (!CryptoSucceeded(status)) {
        BCryptDestroyKey(keyHandle);
        BCryptCloseAlgorithmProvider(algorithm, 0);
        return false;
    }

    plaintext.resize(plaintextSize);
    status = BCryptDecrypt(
        keyHandle,
        ciphertextCopy.data(),
        static_cast<ULONG>(ciphertextCopy.size()),
        &authInfo,
        nullptr,
        0,
        plaintext.data(),
        plaintextSize,
        &plaintextSize,
        0
    );

    BCryptDestroyKey(keyHandle);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (!CryptoSucceeded(status)) {
        plaintext.clear();
        return false;
    }
    plaintext.resize(plaintextSize);
    return true;
}

class GoalTransitionMonitor {
public:
    explicit GoalTransitionMonitor(const std::filesystem::path& executableDirectory)
        : executableDirectory_(executableDirectory) {}

    void Poll() {
        const DWORD now = GetTickCount();
        if (now < nextPollTick_) {
            return;
        }
        nextPollTick_ = now + kGoalMonitorPollMs;

        if (!EnsureConfigured()) {
            return;
        }

        const std::optional<SaveSnapshot> latest = ReadLatestSnapshot();
        if (!latest.has_value()) {
            return;
        }
        if (latest->path == lastSnapshot_.path && latest->mtimeToken == lastSnapshot_.mtimeToken) {
            return;
        }

        if (lastSnapshot_.mapName == kCultistBaseMap
                && latest->mapName == kDoomHunterBaseMap) {
            WriteTransitionEvent(lastSnapshot_.mapName, latest->mapName, latest->path);
        }

        lastSnapshot_ = *latest;
    }

private:
    bool EnsureConfigured() {
        if (configured_) {
            return !steamRemoteDir_.empty() && steamId3_ != 0;
        }

        configured_ = true;
        const std::filesystem::path configPath = executableDirectory_ / "ap_config.json";
        const std::string configContents = ReadTextFile(configPath);
        if (configContents.empty()) {
            if (!loggedConfigurationFailure_) {
                LogDebug("[Goal] Goal transition monitor disabled: ap_config.json not found next to ap_client.exe.");
                loggedConfigurationFailure_ = true;
            }
            return false;
        }

        if (const auto configuredRemote = ExtractJsonString(configContents, "steam_remote_dir")) {
            steamRemoteDir_ = *configuredRemote;
        }
        if (const auto configuredId = ExtractJsonUnsigned(configContents, "steam_id3")) {
            steamId3_ = *configuredId;
        }

        if (steamRemoteDir_.empty()) {
            if (!loggedConfigurationFailure_) {
                LogDebug("[Goal] Goal transition monitor disabled: steam_remote_dir missing from ap_config.json.");
                loggedConfigurationFailure_ = true;
            }
            return false;
        }

        if (steamId3_ == 0) {
            try {
                const auto remotePath = std::filesystem::path(steamRemoteDir_);
                steamId3_ = std::stoull(remotePath.parent_path().parent_path().filename().string());
            } catch (...) {
                steamId3_ = 0;
            }
        }

        if (steamId3_ == 0) {
            if (!loggedConfigurationFailure_) {
                LogDebug("[Goal] Goal transition monitor disabled: steam_id3 missing and could not be inferred.");
                loggedConfigurationFailure_ = true;
            }
            return false;
        }

        LogDebug(
            "[Goal] Monitoring encrypted game.details for Cultist Base transition via "
            + steamRemoteDir_ + "."
        );
        return true;
    }

    std::optional<SaveSnapshot> ReadLatestSnapshot() {
        std::error_code error;
        const std::filesystem::path remoteRoot(steamRemoteDir_);
        if (!std::filesystem::is_directory(remoteRoot, error)) {
            if (!loggedRemoteFailure_) {
                LogDebug("[Goal] Goal transition monitor disabled: steam_remote_dir is not readable.");
                loggedRemoteFailure_ = true;
            }
            return std::nullopt;
        }

        std::filesystem::path latestPath;
        long long latestToken = 0;
        bool found = false;
        for (const auto& entry : std::filesystem::directory_iterator(remoteRoot, error)) {
            if (error) {
                return std::nullopt;
            }
            if (!entry.is_directory(error)) {
                continue;
            }
            const std::string directoryName = entry.path().filename().string();
            if (directoryName.rfind("GAME-AUTOSAVE", 0) != 0) {
                continue;
            }

            const std::filesystem::path detailsPath = entry.path() / "game.details";
            if (!std::filesystem::is_regular_file(detailsPath, error)) {
                continue;
            }

            const auto writeTime = std::filesystem::last_write_time(detailsPath, error);
            if (error) {
                continue;
            }
            const long long token = writeTime.time_since_epoch().count();
            if (!found || token > latestToken) {
                latestToken = token;
                latestPath = detailsPath;
                found = true;
            }
        }

        if (!found) {
            return std::nullopt;
        }

        std::string plaintext;
        if (!DecryptGameDetails(latestPath, plaintext)) {
            return std::nullopt;
        }

        SaveSnapshot snapshot;
        snapshot.path = latestPath.string();
        snapshot.mtimeToken = latestToken;
        snapshot.mapName = ExtractMapName(plaintext);
        if (snapshot.mapName.empty()) {
            return std::nullopt;
        }
        return snapshot;
    }

    bool DecryptGameDetails(
        const std::filesystem::path& path,
        std::string& plaintext
    ) const {
        std::ifstream input(path, std::ios::binary);
        if (!input) {
            return false;
        }
        std::vector<unsigned char> encrypted(
            (std::istreambuf_iterator<char>(input)),
            std::istreambuf_iterator<char>()
        );
        if (encrypted.size() < 28) {
            return false;
        }

        const unsigned long long steamId64 = kSteamId64Base + steamId3_;
        const std::string aad =
            std::to_string(steamId64) + "MANCUBUS" + path.filename().string();

        std::array<unsigned char, 32> digest = {};
        if (!ComputeSha256(aad, digest)) {
            return false;
        }

        std::array<unsigned char, 16> key = {};
        std::copy(digest.begin(), digest.begin() + key.size(), key.begin());
        const std::vector<unsigned char> nonce(encrypted.begin(), encrypted.begin() + 12);
        const std::vector<unsigned char> ciphertext(encrypted.begin() + 12, encrypted.end() - 16);
        const std::vector<unsigned char> tag(encrypted.end() - 16, encrypted.end());

        std::vector<unsigned char> decrypted;
        if (!Aes128GcmDecrypt(key, nonce, ciphertext, tag, aad, decrypted)) {
            return false;
        }

        plaintext.assign(decrypted.begin(), decrypted.end());
        return true;
    }

    std::string ExtractMapName(const std::string& plaintext) const {
        size_t lineStart = 0;
        while (lineStart < plaintext.size()) {
            size_t lineEnd = plaintext.find('\n', lineStart);
            if (lineEnd == std::string::npos) {
                lineEnd = plaintext.size();
            }
            const std::string line = TrimLine(plaintext.substr(lineStart, lineEnd - lineStart));
            if (line.rfind("mapName=", 0) == 0) {
                return line.substr(std::string("mapName=").size());
            }
            lineStart = lineEnd + 1;
        }
        return {};
    }

    void WriteTransitionEvent(
        const std::string& fromMap,
        const std::string& toMap,
        const std::string& sourcePath
    ) {
        if (GetFileAttributesA(kGoalEventPath) != INVALID_FILE_ATTRIBUTES) {
            LogDebug("[Goal] Transition detected but goal event file is still pending bridge consumption.");
            return;
        }

        ++sequence_;
        SYSTEMTIME now = {};
        GetSystemTime(&now);
        char isoTimestamp[40] = {};
        snprintf(
            isoTimestamp,
            sizeof(isoTimestamp),
            "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ",
            now.wYear,
            now.wMonth,
            now.wDay,
            now.wHour,
            now.wMinute,
            now.wSecond,
            now.wMilliseconds
        );

        const std::string temporaryPath =
            std::string(kGoalEventPath) + "." + std::to_string(GetCurrentProcessId()) + ".tmp";
        FILE* output = fopen(temporaryPath.c_str(), "wb");
        if (!output) {
            LogDebug("[Goal] Failed to create goal transition event file.");
            return;
        }

        const std::string contents =
            "sequence=" + std::to_string(sequence_) + "\n"
            + "timestamp=" + isoTimestamp + "\n"
            + "from_map=" + fromMap + "\n"
            + "to_map=" + toMap + "\n"
            + "source_file=" + sourcePath + "\n";
        fwrite(contents.data(), 1, contents.size(), output);
        fflush(output);
        const int handle = _fileno(output);
        if (handle >= 0) {
            _commit(handle);
        }
        fclose(output);

        if (!MoveFileExA(
                temporaryPath.c_str(),
                kGoalEventPath,
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            )) {
            DeleteFileA(temporaryPath.c_str());
            LogDebug("[Goal] Failed to publish goal transition event file.");
            return;
        }

        LogDebug(
            "[Goal] Published PTB goal transition event: "
            + fromMap + " -> " + toMap + "."
        );
    }

    std::filesystem::path executableDirectory_;
    bool configured_ = false;
    bool loggedConfigurationFailure_ = false;
    bool loggedRemoteFailure_ = false;
    std::string steamRemoteDir_;
    unsigned long long steamId3_ = 0;
    SaveSnapshot lastSnapshot_;
    unsigned long long sequence_ = 0;
    DWORD nextPollTick_ = 0;
};

bool ReadCommandFile(const std::string& path, std::string& command) {
    FILE* file = fopen(path.c_str(), "rb");
    if (!file) return false;

    char buffer[4096] = {};
    const size_t read = fread(buffer, 1, sizeof(buffer) - 1, file);
    fclose(file);
    command = TrimLine(std::string(buffer, read));
    return !command.empty();
}

void EnsureQueueDirectory() {
    CreateDirectoryA(kQueueDirectory, nullptr);
    // Telemetry is a disposable poll, not a gameplay command. Never recover a
    // stale condump across a pause, loading screen, crash, or new game session.
    DeleteFileA("base\\ap_queue\\telemetry.cmd");
    DeleteFileA("base\\ap_queue\\telemetry.processing");

    // Recover commands left in-flight if the injector or game was terminated.
    WIN32_FIND_DATAA data = {};
    HANDLE find = FindFirstFileA("base\\ap_queue\\*.processing", &data);
    if (find == INVALID_HANDLE_VALUE) return;
    do {
        if (!(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
            const std::string processingPath = std::string(kQueueDirectory) + "\\" + data.cFileName;
            const std::string queuedPath =
                processingPath.substr(0, processingPath.size() - std::string(".processing").size()) + ".cmd";
            MoveFileExA(processingPath.c_str(), queuedPath.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
        }
    } while (FindNextFileA(find, &data));
    FindClose(find);
}

std::vector<std::string> FindQueuedFiles() {
    std::vector<std::string> paths;
    WIN32_FIND_DATAA data = {};
    HANDLE find = FindFirstFileA("base\\ap_queue\\*.cmd", &data);
    if (find == INVALID_HANDLE_VALUE) return paths;

    do {
        if (!(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
            paths.push_back(std::string(kQueueDirectory) + "\\" + data.cFileName);
        }
    } while (FindNextFileA(find, &data));

    FindClose(find);
    std::sort(paths.begin(), paths.end());
    return paths;
}

void ImportSpoolFiles(std::deque<CommandJob>& queue) {
    for (const std::string& queuedPath : FindQueuedFiles()) {
        const std::string processingPath = queuedPath.substr(0, queuedPath.size() - 4) + ".processing";
        if (!MoveFileExA(queuedPath.c_str(), processingPath.c_str(), MOVEFILE_WRITE_THROUGH)) {
            continue;
        }

        std::string command;
        if (ReadCommandFile(processingPath, command)) {
            queue.push_back({processingPath, command});
            LogDebug("Queued command: " + command);
        } else {
            LogDebug("Discarding unreadable/empty queue file: " + processingPath);
            DeleteFileA(processingPath.c_str());
        }
    }
}

bool IsTelemetryJob(const CommandJob& job) {
    const size_t separator = job.path.find_last_of("\\/");
    const std::string filename =
        separator == std::string::npos ? job.path : job.path.substr(separator + 1);
    return filename.rfind("telemetry.", 0) == 0;
}

void DiscardTelemetryJobs(std::deque<CommandJob>& queue) {
    auto job = queue.begin();
    while (job != queue.end()) {
        if (!IsTelemetryJob(*job)) {
            ++job;
            continue;
        }
        DeleteFileA(job->path.c_str());
        LogDebug("Discarded stale telemetry command while RPC is paused.");
        job = queue.erase(job);
    }
    DeleteFileA("base\\ap_queue\\telemetry.cmd");
    DeleteFileA("base\\ap_queue\\telemetry.processing");
}

bool IsRpcExecutionEnabled() {
    return GetFileAttributesA(kRpcGatePath) != INVALID_FILE_ATTRIBUTES;
}

bool ArmRpcExecution() {
    FILE* file = fopen(kRpcGatePath, "w");
    if (!file) {
        return false;
    }
    fputs("enabled\n", file);
    fflush(file);
    fclose(file);
    return true;
}

void QuarantineFailedJob(const CommandJob& job) {
    const std::string suffix = ".processing";
    std::string failedPath = job.path;
    if (failedPath.size() >= suffix.size()
            && failedPath.compare(failedPath.size() - suffix.size(), suffix.size(), suffix) == 0) {
        failedPath.replace(failedPath.size() - suffix.size(), suffix.size(), ".failed");
    } else {
        failedPath += ".failed";
    }
    MoveFileExA(
        job.path.c_str(),
        failedPath.c_str(),
        MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    );
}

bool ExecuteCommand(const std::string& command) {
    LogDebug("Executing queued command: " + command);

    if (command.rfind("#DUMP_ENTITIES", 0) == 0) {
        const size_t bufferSize = 128 * 1024 * 1024;
        unsigned char* buffer = static_cast<unsigned char*>(malloc(bufferSize));
        if (!buffer) return false;
        size_t actualSize = bufferSize;
        const bool success = g_MhInterface->GetEntitiesFile(buffer, &actualSize);
        if (success) {
            FILE* output = fopen("base\\map.entities", "wb");
            if (output) {
                fwrite(buffer, 1, actualSize, output);
                fclose(output);
            } else {
                free(buffer);
                return false;
            }
        }
        free(buffer);
        return success;
    }

    if (command.rfind("#PUSH_ENTITIES ", 0) == 0) {
        std::string path = command.substr(15);
        return g_MhInterface->PushEntitiesFile(path.data(), nullptr, 0);
    }

    return g_MhInterface->ExecuteConsoleCommand(
        reinterpret_cast<unsigned char*>(const_cast<char*>(command.c_str()))
    );
}

int main(int argc, char** argv) {
    if (argc > 1 && !SetCurrentDirectoryA(argv[1])) {
        printf("Failed to set DOOM working directory: %s\n", argv[1]);
        return 1;
    }

    char executablePath[MAX_PATH] = {};
    if (GetModuleFileNameA(nullptr, executablePath, MAX_PATH) == 0) {
        printf("Failed to resolve ap_client.exe path.\n");
        return 1;
    }
    GoalTransitionMonitor goalTransitionMonitor(
        std::filesystem::path(executablePath).parent_path()
    );

    HANDLE singleInstance = CreateMutexA(nullptr, TRUE, "DoomEternalArchipelagoClient");
    if (!singleInstance || GetLastError() == ERROR_ALREADY_EXISTS) {
        LogDebug("Another AP Client instance is already running; exiting.");
        if (singleInstance) CloseHandle(singleInstance);
        return 0;
    }

    LogDebug("Starting AP Client EXE with atomic command spool...");
    EnsureQueueDirectory();
    DeleteFileA(kRpcGatePath);
    LogDebug("RPC command execution is PAUSED. Use /doom_rpc_on inside a loaded level.");

    GameStateProbe gameStateProbe(LogDebug);
    g_MhInterface = new MeathookInterface();
    LogDebug("Waiting for Meathook RPC to initialize...");
    while (!g_MhInterface || !g_MhInterface->m_Initialized) {
        gameStateProbe.Poll();
        Sleep(100);
    }
    LogDebug("Connected to Meathook RPC.");

    std::deque<CommandJob> queue;
    DWORD lastExecution = 0;
    bool lastRpcArmed = false;
    bool lastRpcEnabled = false;

    while (true) {
        gameStateProbe.Poll();
        goalTransitionMonitor.Poll();
        ImportSpoolFiles(queue);

        const DWORD now = GetTickCount();
        bool rpcArmed = IsRpcExecutionEnabled();
        if (!queue.empty() && !rpcArmed) {
            if (ArmRpcExecution()) {
                rpcArmed = true;
                LogDebug("RPC command execution auto-armed because commands are pending.");
            }
        }
        const bool rpcEnabled = rpcArmed && gameStateProbe.IsSafeForRpc();
        if (rpcArmed != lastRpcArmed) {
            LogDebug(rpcArmed
                ? "RPC command execution ARMED; waiting for safe gameplay."
                : "RPC command execution DISARMED.");
            lastRpcArmed = rpcArmed;
        }
        if (rpcEnabled != lastRpcEnabled) {
            LogDebug(rpcEnabled
                ? "RPC memory gate OPEN; command execution ENABLED."
                : "RPC memory gate CLOSED; queued commands are preserved.");
            lastRpcEnabled = rpcEnabled;
        }
        if (!rpcEnabled) {
            DiscardTelemetryJobs(queue);
        }
        if (!queue.empty()
                && rpcEnabled
                && g_MhInterface->m_Initialized
                && now - lastExecution >= kCommandSpacingMs) {
            CommandJob& job = queue.front();
            if (GetFileAttributesA(job.path.c_str()) == INVALID_FILE_ATTRIBUTES) {
                LogDebug("Discarded externally cancelled command: " + job.command);
                queue.pop_front();
                continue;
            }
            if (ExecuteCommand(job.command)) {
                DeleteFileA(job.path.c_str());
                LogDebug("Command completed and acknowledged: " + job.command);
                queue.pop_front();
            } else {
                QuarantineFailedJob(job);
                LogDebug("Command failed and was quarantined without retry: " + job.command);
                queue.pop_front();
                DeleteFileA(kRpcGatePath);
            }
            lastExecution = now;
        }

        Sleep(50);
    }

    ReleaseMutex(singleInstance);
    CloseHandle(singleInstance);
}
