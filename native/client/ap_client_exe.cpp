#include <windows.h>
#include <bcrypt.h>
#include <io.h>
#include <tlhelp32.h>
#include <winver.h>
#include <cstdlib>
#include <cstring>
#include <stdio.h>
#include <algorithm>
#include <array>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <optional>
#include <regex>
#include <sstream>
#include <string>
#include <utility>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include "ap_client_path_utils.h"
#include "game_state_probe.h"
#include "mhclient.h"
#include "rpc_queue_policy.h"

MeathookInterface* g_MhInterface = nullptr;

static const char* kQueueDirectory = "base\\ap_queue";
static const char* kQueueSessionNamespacePath = "base\\ap_queue\\active_session_namespace";
static const char* kRpcGatePath = "base\\ap_rpc_enabled";
static const char* kTransitionEventPrefix = "base\\ap_transition_";
static const char* kGameplaySaveEvidencePath = "base\\ap_gameplay_save.state";
static const char* kReleaseVersion = "0.4.0-beta.4";
static const char* kRuntimeCapabilityPath = "base\\ap_runtime.capability";
static const char* kDeathLinkRequestPath = "base\\ap_runtime.deathlink.request";
static const char* kDeathLinkEventPath = "base\\ap_runtime.deathlink.event";
static const char* kNativeEventPath = "base\\ap_runtime.events";
static const char* kNativeEventAckPath = "base\\ap_runtime.events.ack";

static const char* RuntimeGameStateName(APGameState state)
{
    switch (static_cast<int>(state)) {
    case 1: return "MAIN_MENU";
    case 2: return "LOADING";
    case 3: return "GAMEPLAY";
    case 4: return "PAUSED";
    default: return "UNKNOWN";
    }
}
static const char* kRpcEntityPrefix = "ap_rpc_v3";
static const int kRpcEntityContractRevision = 3;
static const int kNativeCommandPolicyRevision = 7;
static const ULONGLONG kSteamId64Base = 76561197960265728ULL;
static const DWORD kCommandSpacingMs = 250;
static const DWORD kQueueStateLogMs = 5000;
static const DWORD kGoalMonitorPollMs = 1000;
static const DWORD kRpcStallWarnMs = 15000;
static const std::array<const char*, 0> kValidatedXinputSha256 = {};

std::string CanonicalMapName(std::string name) {
    std::replace(name.begin(), name.end(), '\\', '/');
    while (!name.empty() && (name.back() == '/' || name.back() == '\r'
            || name.back() == '\n' || name.back() == ' ' || name.back() == '\t')) {
        name.pop_back();
    }
    if (name == "game/hub/hub" || name == "game/sp/hub/hub") {
        return "game/hub/hub";
    }
    return name;
}

struct CommandJob {
    std::string path;
    std::string command;
    unsigned int retryAttempt = 0;
    DWORD nextAttemptTick = 0;
    std::string source = "cmd";
    DWORD importedTick = 0;
    std::optional<std::string> receiptNamespace;
};

using CommandSourceMap = std::unordered_map<std::string, std::string>;

struct RpcWatchdogContext {
    volatile LONG completed = 0;
    DWORD startTick = 0;
    std::string commandId;
    std::string operation;
};

struct SaveSnapshot {
    std::string slotDirectory;
    std::string path;
    std::string mapName;
    long long mtimeToken = 0;
};

struct QueueSnapshot {
    size_t pending = 0;
    size_t processing = 0;
    size_t failed = 0;
};

struct MeathookPreflightResult {
    bool xinputPresent = false;
    bool hashValidated = false;
    bool deliveryAllowed = false;
    bool multipleSuspiciousLoaders = false;
    bool probableProton = false;
    XinputDllMode dllMode = XinputDllMode::Missing;
    std::string xinputPath;
    std::string gameRootCandidate;
    std::string clientCandidate;
    std::string sha256;
    std::string fileVersion;
    std::string productVersion;
    unsigned long long sizeBytes = 0;
    std::string lastWriteLocal;
    std::vector<std::string> suspiciousLoaders;
    std::vector<std::string> protonSignals;
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

void RotateClientLog() {
    const char* current = "base\\ap_client.log";
    const char* previous = "base\\ap_client.previous.log";
    DeleteFileA(previous);
    MoveFileExA(current, previous, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    if (FILE* file = fopen(current, "wb")) {
        fclose(file);
    }
}

DWORD WINAPI RpcCallWatchdog(LPVOID data) {
    RpcWatchdogContext* context = static_cast<RpcWatchdogContext*>(data);
    Sleep(kRpcStallWarnMs);
    if (InterlockedCompareExchange(&context->completed, 0, 0) == 0) {
        LogDebug(
            "RPC_CALL_STALLED command_id=" + context->commandId
            + " operation=" + context->operation
            + " elapsed_ms=" + std::to_string(GetTickCount() - context->startTick)
        );
    }
    return 0;
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

std::string DigestToHex(const std::array<unsigned char, 32>& digest) {
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (unsigned char byte : digest) {
        output << std::setw(2) << static_cast<int>(byte);
    }
    return output.str();
}

std::string ReadBinaryFile(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        return {};
    }
    return std::string(
        (std::istreambuf_iterator<char>(input)),
        std::istreambuf_iterator<char>()
    );
}

std::string FormatLocalFileTime(const FILETIME& fileTime) {
    FILETIME localFileTime = {};
    SYSTEMTIME localSystemTime = {};
    if (!FileTimeToLocalFileTime(&fileTime, &localFileTime)
            || !FileTimeToSystemTime(&localFileTime, &localSystemTime)) {
        return "UNKNOWN";
    }
    char buffer[32] = {};
    snprintf(
        buffer,
        sizeof(buffer),
        "%04u-%02u-%02u %02u:%02u:%02u",
        localSystemTime.wYear,
        localSystemTime.wMonth,
        localSystemTime.wDay,
        localSystemTime.wHour,
        localSystemTime.wMinute,
        localSystemTime.wSecond
    );
    return buffer;
}

std::string FormatVersionNumber(DWORD ms, DWORD ls) {
    std::ostringstream output;
    output
        << HIWORD(ms) << '.'
        << LOWORD(ms) << '.'
        << HIWORD(ls) << '.'
        << LOWORD(ls);
    return output.str();
}

std::string GetFixedFileVersion(const std::filesystem::path& path, bool productVersion) {
    DWORD handle = 0;
    const DWORD infoSize = GetFileVersionInfoSizeA(path.string().c_str(), &handle);
    if (infoSize == 0) {
        return "UNKNOWN";
    }

    std::vector<char> info(infoSize);
    if (!GetFileVersionInfoA(path.string().c_str(), 0, infoSize, info.data())) {
        return "UNKNOWN";
    }

    VS_FIXEDFILEINFO* fixedInfo = nullptr;
    UINT fixedInfoSize = 0;
    if (!VerQueryValueA(info.data(), "\\", reinterpret_cast<LPVOID*>(&fixedInfo), &fixedInfoSize)
            || fixedInfo == nullptr
            || fixedInfoSize < sizeof(VS_FIXEDFILEINFO)) {
        return "UNKNOWN";
    }

    return productVersion
        ? FormatVersionNumber(fixedInfo->dwProductVersionMS, fixedInfo->dwProductVersionLS)
        : FormatVersionNumber(fixedInfo->dwFileVersionMS, fixedInfo->dwFileVersionLS);
}

QueueSnapshot CountQueueFiles() {
    QueueSnapshot snapshot;
    std::error_code error;
    const std::filesystem::path queueDir(kQueueDirectory);
    if (!std::filesystem::is_directory(queueDir, error)) {
        return snapshot;
    }

    for (const auto& entry : std::filesystem::directory_iterator(queueDir, error)) {
        if (error || !entry.is_regular_file(error)) {
            continue;
        }
        const std::string extension = entry.path().extension().string();
        if (extension == ".cmd") {
            ++snapshot.pending;
        } else if (extension == ".processing") {
            ++snapshot.processing;
        } else if (extension == ".failed") {
            ++snapshot.failed;
        }
    }
    return snapshot;
}

DWORD CountProcessesNamed(const char* executableName) {
    DWORD count = 0;
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) {
        return 0;
    }

    PROCESSENTRY32 entry = {};
    entry.dwSize = sizeof(entry);
    if (Process32First(snapshot, &entry)) {
        do {
            if (_stricmp(entry.szExeFile, executableName) == 0) {
                ++count;
            }
        } while (Process32Next(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return count;
}

std::string CurrentWorkingDirectory() {
    std::error_code error;
    const std::filesystem::path current = std::filesystem::current_path(error);
    return error ? "UNKNOWN" : current.string();
}

std::string CommandIdFromPath(const std::string& path) {
    return std::filesystem::path(path).stem().string();
}

const char* RpcCallResultName(RpcCallResult result) {
    switch (result) {
    case PIPE_NOT_FOUND:
        return "PIPE_NOT_FOUND";
    case PIPE_BUSY:
        return "PIPE_BUSY";
    case WAIT_NAMED_PIPE_TIMEOUT:
        return "WAIT_NAMED_PIPE_TIMEOUT";
    case RPC_CALL_DELIVERED:
        return "RPC_CALL_DELIVERED";
    case RPC_EXCEPTION:
        return "RPC_EXCEPTION";
    case UNKNOWN_TRANSPORT_ERROR:
        return "UNKNOWN_TRANSPORT_ERROR";
    case RPC_CALL_RESULT_NONE:
    default:
        return "RPC_CALL_RESULT_NONE";
    }
}

const char* ReceiptCommandKind(const std::string& commandId) {
    return commandId.find("-notify") != std::string::npos ? "notification" : "effect";
}

std::string DeliveryContextFields() {
    return " active_map=unavailable slot=unavailable bridge_protocol_version=unavailable"
        " rpc_entity_contract_revision=" + std::to_string(kRpcEntityContractRevision)
        + " rpc_entity_prefix=" + kRpcEntityPrefix
        + " helper_sha=unavailable injector_sha=unavailable";
}

std::string RpcGateReason(
    bool rpcArmed,
    bool rpcTransportReady,
    const GameStateProbe& gameStateProbe
) {
    if (!rpcArmed) return "rpc_disarmed";
    if (!rpcTransportReady) return "rpc_unavailable";
    if (gameStateProbe.IsLoading()) return "loading";
    if (!gameStateProbe.IsGameplayLoaded()) return "menu_or_no_active_map";
    if (!gameStateProbe.IsSafeForRpc()) return "player_unavailable";
    return "ready";
}

MeathookPreflightResult InspectMeathookInstallation(const RuntimePathInfo& runtimePaths) {
    MeathookPreflightResult result;
    result.probableProton = runtimePaths.probableProton;
    result.protonSignals = runtimePaths.protonSignals;
    result.gameRootCandidate = runtimePaths.gameRootDllCandidate.string();
    result.clientCandidate = runtimePaths.clientDllCandidate.string();

    const XinputDllSelection selectedDll = SelectXinputDllCandidate(runtimePaths);
    result.dllMode = selectedDll.mode;
    result.xinputPath = selectedDll.selectedPath.string();
    if (selectedDll.mode == XinputDllMode::Missing) {
        return result;
    }

    WIN32_FILE_ATTRIBUTE_DATA attributes = {};
    if (!GetFileAttributesExA(
            selectedDll.selectedPath.string().c_str(),
            GetFileExInfoStandard,
            &attributes
        )) {
        return result;
    }

    result.xinputPresent = true;
    result.sizeBytes =
        (static_cast<unsigned long long>(attributes.nFileSizeHigh) << 32)
        | attributes.nFileSizeLow;
    result.lastWriteLocal = FormatLocalFileTime(attributes.ftLastWriteTime);
    result.fileVersion = GetFixedFileVersion(selectedDll.selectedPath, false);
    result.productVersion = GetFixedFileVersion(selectedDll.selectedPath, true);

    const std::string contents = ReadBinaryFile(selectedDll.selectedPath);
    if (!contents.empty()) {
        std::array<unsigned char, 32> digest = {};
        if (ComputeSha256(contents, digest)) {
            result.sha256 = DigestToHex(digest);
        }
    }

    for (const char* candidate : { "xinput1_4.dll", "dinput8.dll", "dxgi.dll", "version.dll" }) {
        const std::filesystem::path candidatePath = runtimePaths.gameRootDir / candidate;
        if (std::filesystem::exists(candidatePath)) {
            result.suspiciousLoaders.push_back(candidatePath.string());
        }
    }
    result.multipleSuspiciousLoaders = result.suspiciousLoaders.size() > 1;

    if (!result.sha256.empty()) {
        for (const char* validatedHash : kValidatedXinputSha256) {
            if (result.sha256 == validatedHash) {
                result.hashValidated = true;
                break;
            }
        }
    }

    result.deliveryAllowed =
        result.xinputPresent && (result.hashValidated || kValidatedXinputSha256.empty());
    return result;
}

void LogStartupHeader(
    const std::string& executablePath,
    const std::string& workingDirectory,
    const std::string& doomExecutablePath,
    const QueueSnapshot& queueSnapshot,
    const MeathookPreflightResult& preflight,
    const RuntimePathInfo& runtimePaths
) {
    SYSTEMTIME utcNow = {};
    SYSTEMTIME localNow = {};
    GetSystemTime(&utcNow);
    GetLocalTime(&localNow);

    char utcTimestamp[40] = {};
    char localTimestamp[40] = {};
    snprintf(
        utcTimestamp,
        sizeof(utcTimestamp),
        "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ",
        utcNow.wYear,
        utcNow.wMonth,
        utcNow.wDay,
        utcNow.wHour,
        utcNow.wMinute,
        utcNow.wSecond,
        utcNow.wMilliseconds
    );
    snprintf(
        localTimestamp,
        sizeof(localTimestamp),
        "%04u-%02u-%02u %02u:%02u:%02u.%03u",
        localNow.wYear,
        localNow.wMonth,
        localNow.wDay,
        localNow.wHour,
        localNow.wMinute,
        localNow.wSecond,
        localNow.wMilliseconds
    );

    OSVERSIONINFOEXA versionInfo = {};
    versionInfo.dwOSVersionInfoSize = sizeof(versionInfo);
    GetVersionExA(reinterpret_cast<OSVERSIONINFOA*>(&versionInfo));

    const std::string architecture =
#if defined(_WIN64)
        "x86_64";
#else
        "x86";
#endif

    LogDebug("=== AP Client startup header ===");
    LogDebug(std::string("PTB version: ") + kReleaseVersion);
    LogDebug(std::string("Build ID: ") + __DATE__ + " " + __TIME__);
    LogDebug(std::string("UTC time: ") + utcTimestamp);
    LogDebug(std::string("Local time: ") + localTimestamp);
    LogDebug(std::string("Executable architecture: ") + architecture);
    LogDebug(
        "Windows version: "
        + std::to_string(versionInfo.dwMajorVersion) + "."
        + std::to_string(versionInfo.dwMinorVersion) + "."
        + std::to_string(versionInfo.dwBuildNumber)
    );
    LogDebug("PID: " + std::to_string(GetCurrentProcessId()));
    LogDebug("Working directory: " + workingDirectory);
    LogDebug("Executable path: " + executablePath);
    LogDebug("Client directory: " + runtimePaths.clientDir.string());
    LogDebug("DOOMEternalx64vk.exe path: " + doomExecutablePath);
    LogDebug("base path: " + std::filesystem::absolute(workingDirectory).string());
    LogDebug(
        "queue path: "
        + std::filesystem::absolute(std::filesystem::path(kQueueDirectory)).string()
    );
    LogDebug(
        "gate path: "
        + std::filesystem::absolute(std::filesystem::path(kRpcGatePath)).string()
    );
    LogDebug(
        "Queue snapshot: pending=" + std::to_string(queueSnapshot.pending)
        + " processing=" + std::to_string(queueSnapshot.processing)
        + " failed=" + std::to_string(queueSnapshot.failed)
    );
    LogDebug("Another ap_client.exe instance detected: no (single-instance mutex acquired).");
    LogDebug(
        "Other DOOM processes detected: "
        + std::to_string(CountProcessesNamed("DOOMEternalx64vk.exe"))
    );
    LogDebug("Offset profile: steam-6.66-rev-3.1");
    LogDebug(
        "RPC_ENTITY_CONTRACT_REVISION: "
        + std::to_string(kRpcEntityContractRevision)
    );
    LogDebug(std::string("RPC_ENTITY_PREFIX: ") + kRpcEntityPrefix);
    LogDebug(
        "NATIVE_COMMAND_POLICY_REVISION: "
        + std::to_string(kNativeCommandPolicyRevision)
    );
    LogDebug(
        std::string("Runtime mode: ")
        + (preflight.probableProton ? "Proton-compatible/client-local DLL allowed" : "Windows-native/game-root DLL required")
    );
    LogDebug("Game root DLL candidate: " + preflight.gameRootCandidate);
    LogDebug("Client DLL candidate: " + preflight.clientCandidate);
    if (!preflight.protonSignals.empty()) {
        for (const std::string& signal : preflight.protonSignals) {
            LogDebug("Proton signal: " + signal);
        }
    }
    for (const std::filesystem::path& configPath : runtimePaths.configCandidates) {
        LogDebug("Config candidate: " + configPath.string());
    }
    LogDebug(std::string("Meathook XINPUT1_3.dll path: ") + preflight.xinputPath);
    LogDebug(
        "Meathook XINPUT1_3.dll present: "
        + std::string(preflight.xinputPresent ? "yes" : "no")
    );
    if (preflight.xinputPresent) {
        LogDebug(
            std::string("Meathook XINPUT1_3.dll source: ")
            + (preflight.dllMode == XinputDllMode::GameRoot
                ? "game-root candidate"
                : "client-local Proton candidate")
        );
    }
    if (preflight.xinputPresent) {
        LogDebug("Meathook XINPUT1_3.dll size: " + std::to_string(preflight.sizeBytes));
        LogDebug("Meathook XINPUT1_3.dll last write: " + preflight.lastWriteLocal);
        LogDebug("Meathook XINPUT1_3.dll SHA-256: " + preflight.sha256);
        LogDebug("Meathook XINPUT1_3.dll FileVersion: " + preflight.fileVersion);
        LogDebug("Meathook XINPUT1_3.dll ProductVersion: " + preflight.productVersion);
    }
    LogDebug(
        "Meathook XINPUT1_3.dll hash validated: "
        + std::string(preflight.hashValidated ? "yes" : "no")
    );
    if (kValidatedXinputSha256.empty()) {
        LogDebug("Validated Meathook hash list: not configured in this build.");
    } else {
        LogDebug("Validated Meathook hash list: configured.");
    }
    if (!preflight.suspiciousLoaders.empty()) {
        for (const std::string& loaderPath : preflight.suspiciousLoaders) {
            LogDebug("Suspicious proxy DLL present: " + loaderPath);
        }
    }
    if (preflight.multipleSuspiciousLoaders) {
        LogDebug("WARNING: multiple proxy DLL candidates are present in the DOOM root.");
    }
    if (!preflight.probableProton && getenv("DOOM_AP_STARTED_BY_WINDOWS_BATCH") == nullptr) {
        LogDebug("WARNING: ap_client.exe was opened directly. Start the integrated DOOM Eternal Client from Archipelago Launcher.");
    }
    LogDebug("=== End startup header ===");
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

class MissionTransitionMonitor {
public:
    explicit MissionTransitionMonitor(const RuntimePathInfo& runtimePaths)
        : runtimePaths_(runtimePaths) {}

    void Poll(bool gameplayLoaded, bool loading) {
        const DWORD now = GetTickCount();
        const bool stateChanged = !gameplayStateInitialized_
            || gameplayLoaded != gameplayLoaded_
            || !loadingStateInitialized_
            || loading != loading_;
        if (!stateChanged && now < nextPollTick_) {
            return;
        }
        nextPollTick_ = now + kGoalMonitorPollMs;

        if (!EnsureConfigured()) {
            return;
        }

        if (!loadingStateInitialized_ || loading != loading_) {
            loadingStateInitialized_ = true;
            loading_ = loading;
            if (loading_) {
                // Menu/cloud/delete writes already present at this edge cannot
                // identify the slot being loaded into gameplay.
                CaptureMenuSlotTokens();
                sawLoadingForEpoch_ = true;
            }
        }

        if (!gameplayStateInitialized_ || gameplayLoaded != gameplayLoaded_) {
            const bool firstObservedState = !gameplayStateInitialized_;
            gameplayStateInitialized_ = true;
            gameplayLoaded_ = gameplayLoaded;
            ++gameplayEpoch_;
            if (!gameplayLoaded_) {
                CaptureMenuSlotTokens();
                WriteGameplayEvidence(std::nullopt);
                return;
            }

            // Mission Complete owns its own load-edge baseline. It must not
            // wait for the asynchronous Python durable-save observer to prove
            // a slot: a decrypted primary game.details at safe gameplay is the
            // transition source.
            const std::optional<SaveSnapshot> changed = ReadChangedSnapshot(menuSlotTokens_);
            const std::optional<SaveSnapshot> entered = changed.has_value()
                ? changed
                : (firstObservedState || sawLoadingForEpoch_)
                    ? ReadLatestSnapshot()
                    : std::nullopt;
            sawLoadingForEpoch_ = false;
            if (!entered.has_value()) {
                LogDebug("[Mission] TRANSITION_EVENT_SKIPPED reason=no_load_snapshot");
                WriteGameplayEvidence(std::nullopt);
                return;
            }
            const bool provisional = !changed.has_value();
            LogDebug(
                "[Mission] MISSION_SNAPSHOT provisional="
                + std::string(provisional ? "true" : "false")
                + " slot=" + entered->slotDirectory
                + " map=" + entered->mapName
            );
            if (!lastSnapshot_.path.empty()
                    && lastSnapshot_.mapName != entered->mapName) {
                WriteTransitionEvent(lastSnapshot_.mapName, entered->mapName, entered->path);
            }
            activeSlotDirectory_ = entered->slotDirectory;
            lastSnapshot_ = *entered;
            WriteGameplayEvidence(entered, provisional);
            return;
        }

        if (!gameplayLoaded_) {
            return;
        }

        const std::optional<SaveSnapshot> changed = ReadChangedSnapshot(menuSlotTokens_);
        const std::optional<SaveSnapshot> latest = changed.has_value()
            ? changed
            : ReadSlotSnapshot(activeSlotDirectory_);
        if (!latest.has_value()) {
            return;
        }
        if (latest->path == lastSnapshot_.path && latest->mtimeToken == lastSnapshot_.mtimeToken && !changed.has_value()) {
            return;
        }

        if (!lastSnapshot_.mapName.empty()
                && lastSnapshot_.mapName != latest->mapName) {
            WriteTransitionEvent(lastSnapshot_.mapName, latest->mapName, latest->path);
        }

        activeSlotDirectory_ = latest->slotDirectory;
        lastSnapshot_ = *latest;
        const bool provisional = !changed.has_value();
        WriteGameplayEvidence(latest, provisional);
    }

private:
    bool EnsureConfigured() {
        const DWORD now = GetTickCount();
        if (configured_) {
            return true;
        }
        if (now < nextConfigRetryTick_) {
            return false;
        }
        nextConfigRetryTick_ = now + 5000;

        const std::optional<std::filesystem::path> configPath =
            FindFirstExistingPath(runtimePaths_.configCandidates);
        if (!configPath.has_value()) {
            std::ostringstream message;
            message
                << "[Goal] Config not found yet. Run/setup the DOOM Eternal Client "
                << "once, then restart ap_client.exe if needed. Tried:";
            for (const std::filesystem::path& path : runtimePaths_.configCandidates) {
                message << " " << path.string();
            }
            if (!loggedConfigurationFailure_) {
                LogDebug(message.str());
                loggedConfigurationFailure_ = true;
            }
            return false;
        }

        const std::string configContents = ReadTextFile(*configPath);
        if (configContents.empty()) {
            if (!loggedConfigurationFailure_) {
                LogDebug(
                    "[Goal] Config file exists but could not be read: "
                    + configPath->string()
                );
                loggedConfigurationFailure_ = true;
            }
            return false;
        }
        loggedConfigurationFailure_ = false;

        steamRemoteDir_.clear();
        steamId3_ = 0;
        if (const auto configuredRemote = ExtractJsonString(configContents, "steam_remote_dir")) {
            steamRemoteDir_ = *configuredRemote;
        }
        if (const auto configuredId = ExtractJsonUnsigned(configContents, "steam_id3")) {
            steamId3_ = *configuredId;
        }

        if (steamRemoteDir_.empty()) {
            if (!loggedConfigurationFailure_) {
                LogDebug(
                    "[Goal] steam_remote_dir missing from " + configPath->string()
                    + ". Complete setup in the DOOM Eternal Client, then restart "
                    + "ap_client.exe if needed."
                );
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
                LogDebug(
                    "[Goal] steam_id3 missing and could not be inferred from "
                    + configPath->string() + "."
                );
                loggedConfigurationFailure_ = true;
            }
            return false;
        }

        configured_ = true;
        LogDebug(
            "[Mission] Monitoring encrypted game.details transitions via "
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
            if (!std::regex_match(directoryName, std::regex("GAME-AUTOSAVE[0-9]+"))) {
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
        snapshot.slotDirectory = latestPath.parent_path().filename().string();
        snapshot.path = latestPath.string();
        snapshot.mtimeToken = latestToken;
        snapshot.mapName = ExtractMapName(plaintext);
        if (snapshot.mapName.empty()) {
            return std::nullopt;
        }
        return snapshot;
    }

    std::vector<std::pair<std::string, long long>> ReadSlotTokens() const {
        std::vector<std::pair<std::string, long long>> tokens;
        std::error_code error;
        const std::filesystem::path remoteRoot(steamRemoteDir_);
        if (!std::filesystem::is_directory(remoteRoot, error)) {
            return tokens;
        }
        for (const auto& entry : std::filesystem::directory_iterator(remoteRoot, error)) {
            if (error) {
                break;
            }
            if (!entry.is_directory(error)) {
                continue;
            }
            const std::string slot = entry.path().filename().string();
            if (!std::regex_match(slot, std::regex("GAME-AUTOSAVE[0-9]+"))) {
                continue;
            }
            const std::filesystem::path detailsPath = entry.path() / "game.details";
            if (!std::filesystem::is_regular_file(detailsPath, error)) {
                continue;
            }
            const auto writeTime = std::filesystem::last_write_time(detailsPath, error);
            if (!error) {
                tokens.emplace_back(slot, writeTime.time_since_epoch().count());
            }
            error.clear();
        }
        return tokens;
    }

    void CaptureMenuSlotTokens() {
        menuSlotTokens_ = ReadSlotTokens();
    }

    std::optional<SaveSnapshot> ReadChangedSnapshot(
        const std::vector<std::pair<std::string, long long>>& baseline
    ) {
        std::optional<SaveSnapshot> newestChanged;
        for (const auto& [slot, token] : ReadSlotTokens()) {
            const auto previous = std::find_if(
                baseline.begin(),
                baseline.end(),
                [&slot](const auto& entry) { return entry.first == slot; }
            );
            if (previous != baseline.end() && previous->second == token) {
                continue;
            }
            const std::optional<SaveSnapshot> candidate = ReadSlotSnapshot(slot);
            if (candidate.has_value()
                    && (!newestChanged.has_value()
                        || candidate->mtimeToken > newestChanged->mtimeToken)) {
                newestChanged = candidate;
            }
        }
        return newestChanged;
    }

    std::optional<SaveSnapshot> ReadSlotSnapshot(const std::string& slotDirectory) {
        if (!std::regex_match(slotDirectory, std::regex("GAME-AUTOSAVE[0-9]+"))) {
            return std::nullopt;
        }
        std::error_code error;
        const std::filesystem::path detailsPath =
            std::filesystem::path(steamRemoteDir_) / slotDirectory / "game.details";
        if (!std::filesystem::is_regular_file(detailsPath, error)) {
            return std::nullopt;
        }
        const auto writeTime = std::filesystem::last_write_time(detailsPath, error);
        if (error) {
            return std::nullopt;
        }
        std::string plaintext;
        if (!DecryptGameDetails(detailsPath, plaintext)) {
            return std::nullopt;
        }
        SaveSnapshot snapshot;
        snapshot.slotDirectory = slotDirectory;
        snapshot.path = detailsPath.string();
        snapshot.mtimeToken = writeTime.time_since_epoch().count();
        snapshot.mapName = ExtractMapName(plaintext);
        return snapshot.mapName.empty() ? std::nullopt : std::optional<SaveSnapshot>(snapshot);
    }

    void WriteGameplayEvidence(
        const std::optional<SaveSnapshot>& snapshot,
        bool provisional = false
    ) {
        const std::string temporaryPath = std::string(kGameplaySaveEvidencePath) + ".tmp";
        FILE* output = fopen(temporaryPath.c_str(), "wb");
        if (!output) {
            return;
        }
        std::string contents =
            "state=" + std::string(
                !gameplayLoaded_ ? "menu" : snapshot.has_value() ? "gameplay" : "unproven"
            ) + "\n"
            + "epoch=" + std::to_string(gameplayEpoch_) + "\n";
        if (gameplayLoaded_ && snapshot.has_value()) {
            contents += "slot=" + snapshot->slotDirectory + "\n"
                + "map_name=" + snapshot->mapName + "\n"
                + "provisional=" + std::string(provisional ? "true" : "false") + "\n"
                + "source_file=" + snapshot->path + "\n";
        }
        fwrite(contents.data(), 1, contents.size(), output);
        fflush(output);
        const int handle = _fileno(output);
        if (handle >= 0) {
            _commit(handle);
        }
        fclose(output);
        if (!MoveFileExA(
                temporaryPath.c_str(),
                kGameplaySaveEvidencePath,
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            )) {
            DeleteFileA(temporaryPath.c_str());
        }
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
                return CanonicalMapName(line.substr(std::string("mapName=").size()));
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
        const std::string canonicalFrom = CanonicalMapName(fromMap);
        const std::string canonicalTo = CanonicalMapName(toMap);
        LogDebug(
            "[Mission] MISSION_TRANSITION_SOURCE slot=" + activeSlotDirectory_
            + " map=" + canonicalFrom
        );
        LogDebug(
            "[Mission] MISSION_TRANSITION_TARGET slot=" + activeSlotDirectory_
            + " map=" + canonicalTo
        );
        // Emit every observed load edge. Python publisher rules assign each
        // edge to its matching publishers.
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

        const std::string eventPath =
            std::string(kTransitionEventPrefix)
            + std::to_string(GetCurrentProcessId()) + "_"
            + std::to_string(sequence_) + ".evt";
        const std::string temporaryPath = eventPath + ".tmp";
        FILE* output = fopen(temporaryPath.c_str(), "wb");
        if (!output) {
            LogDebug("[Mission] Failed to create transition event file.");
            return;
        }

        const std::string contents =
            "sequence=" + std::to_string(sequence_) + "\n"
            + "timestamp=" + isoTimestamp + "\n"
            + "from_map=" + canonicalFrom + "\n"
            + "to_map=" + canonicalTo + "\n"
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
                eventPath.c_str(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            )) {
            DeleteFileA(temporaryPath.c_str());
            LogDebug("[Mission] Failed to publish transition event file.");
            return;
        }

        LogDebug(
            "[Mission] TRANSITION_EVENT_PUBLISHED "
            + canonicalFrom + " -> " + canonicalTo + "."
        );
    }

    RuntimePathInfo runtimePaths_;
    bool configured_ = false;
    bool loggedConfigurationFailure_ = false;
    bool loggedRemoteFailure_ = false;
    std::string steamRemoteDir_;
    unsigned long long steamId3_ = 0;
    SaveSnapshot lastSnapshot_;
    std::string activeSlotDirectory_;
    std::vector<std::pair<std::string, long long>> menuSlotTokens_;
    unsigned long long sequence_ = 0;
    unsigned long long gameplayEpoch_ = 0;
    bool gameplayStateInitialized_ = false;
    bool gameplayLoaded_ = false;
    bool loadingStateInitialized_ = false;
    bool loading_ = false;
    bool sawLoadingForEpoch_ = false;
    DWORD nextPollTick_ = 0;
    DWORD nextConfigRetryTick_ = 0;
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

bool WriteCommandFile(const std::string& path, const std::string& command) {
    FILE* file = fopen(path.c_str(), "wb");
    if (!file) return false;
    const std::string line = command + "\n";
    const size_t written = fwrite(line.data(), 1, line.size(), file);
    const bool ok = written == line.size() && fflush(file) == 0;
    fclose(file);
    return ok;
}

bool WriteRuntimeRecord(const char* path, const std::string& record) {
    const std::string temporary = std::string(path) + ".tmp";
    FILE* file = fopen(temporary.c_str(), "wb");
    if (!file) return false;
    const size_t written = fwrite(record.data(), 1, record.size(), file);
    const bool ok = written == record.size() && fflush(file) == 0;
    fclose(file);
    if (!ok) {
        DeleteFileA(temporary.c_str());
        return false;
    }
    return MoveFileExA(
        temporary.c_str(), path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    ) != 0;
}

void PublishRuntimeCapability() {
    RuntimeCapabilityRecord capability;
    RuntimeSnapshot snapshot;
    const bool snapshotReady = g_MhInterface && g_MhInterface->PollRuntimeSnapshot();
    if (g_MhInterface) {
        capability = g_MhInterface->RuntimeCapabilities();
        g_MhInterface->GetCachedRuntimeSnapshot(snapshot);
    }
    std::string record = "status="
        + std::string(capability.status == RUNTIME_TRANSPORT_READY ? "ready" :
            capability.status == RUNTIME_TRANSPORT_PENDING ? "pending" : "unavailable") + "\n"
        + "valid=" + (capability.valid ? "true" : "false") + "\n"
        + "build_supported=" + (capability.info.build_supported ? "true" : "false") + "\n"
        + "hooks_ready=" + (capability.info.hooks_ready ? "true" : "false") + "\n"
        + "capability_runtime_snapshot="
            + (capability.info.capability_runtime_snapshot ? "true" : "false") + "\n"
        + "capability_map="
            + (capability.info.capability_map ? "true" : "false") + "\n"
        + "capability_native_deathlink="
            + (capability.info.capability_native_deathlink ? "true" : "false") + "\n"
        + "capability_native_events="
            + (capability.info.capability_native_events ? "true" : "false") + "\n"
        + "capability_extra_life_telemetry="
            + (capability.info.capability_extra_life_telemetry ? "true" : "false") + "\n"
        + "protocol_version=" + std::to_string(capability.info.protocol_version) + "\n"
        + "runtime_generation="
            + std::to_string(g_MhInterface ? g_MhInterface->BindingGeneration() : 0)
            + "\n"
        + "process_available=" + (capability.valid ? "true" : "false") + "\n"
        + "rpc_reachable=" + (g_MhInterface && g_MhInterface->IsInitialized() ? "true" : "false") + "\n"
        + "protocol_compatible=" + (capability.protocolCompatible ? "true" : "false") + "\n";
    if (!capability.error.empty()) record += "error=" + capability.error + "\n";
    WriteRuntimeRecord(kRuntimeCapabilityPath, record);
    if (snapshotReady && snapshot.valid) {
        const std::string mapName(
            snapshot.data.map,
            strnlen(snapshot.data.map, sizeof(snapshot.data.map))
        );
        const std::string checkpointName(
            snapshot.data.checkpoint,
            strnlen(snapshot.data.checkpoint, sizeof(snapshot.data.checkpoint))
        );
        std::string snapshotRecord =
            "status=ready\nvalid=true\nfresh=true\n"
            "protocol_version=" + std::to_string(capability.info.protocol_version) + "\n"
            "sequence=" + std::to_string(snapshot.data.sequence) + "\n"
            "timestamp_ms=" + std::to_string(snapshot.data.timestamp) + "\n"
            "runtime_ready=" + (snapshot.data.runtime_ready ? "true" : "false") + "\n"
            "player_valid=" + (snapshot.data.player_valid ? "true" : "false") + "\n"
            "health_valid=" + (snapshot.data.health_valid ? "true" : "false") + "\n"
            "max_health_valid=" + (snapshot.data.max_health_valid ? "true" : "false") + "\n"
            "health=" + std::to_string(snapshot.data.health) + "\n"
            "max_health=" + std::to_string(snapshot.data.max_health) + "\n"
            "dead_valid=" + (snapshot.data.dead_valid ? "true" : "false") + "\n"
            "dead=" + (snapshot.data.dead ? "true" : "false") + "\n"
            "game_state_valid=" + (snapshot.data.game_state_valid ? "true" : "false") + "\n"
            "game_state=" + std::string(RuntimeGameStateName(snapshot.data.game_state)) + "\n"
            "map_valid=" + (snapshot.data.map_valid ? "true" : "false") + "\n"
            "map=" + mapName + "\n"
            "checkpoint_valid=" + (snapshot.data.checkpoint_valid ? "true" : "false") + "\n"
            "checkpoint=" + checkpointName + "\n";
        WriteRuntimeRecord("base\\ap_runtime.snapshot", snapshotRecord);
    } else {
        const char* status = g_MhInterface && g_MhInterface->IsInitialized()
            ? "pending" : "unavailable";
        WriteRuntimeRecord(
            "base\\ap_runtime.snapshot",
            std::string("status=") + status + "\nvalid=false\nfresh=false\n"
                + "protocol_version=0\nsequence=0\ntimestamp_ms=0\n"
                + "runtime_ready=false\nplayer_valid=false\nhealth_valid=false\n"
                + "max_health_valid=false\ndead_valid=false\ndead=false\n"
                + "game_state_valid=false\ngame_state=UNKNOWN\nmap_valid=false\n"
                + "map=\ncheckpoint_valid=false\ncheckpoint=\n"
                + "error=typed runtime snapshot unavailable\n"
        );
    }
}

static const char* DeathLinkStatusName(APDeathLinkStatus status)
{
    switch (status) {
    case AP_DEATHLINK_QUEUED: return "queued";
    case AP_DEATHLINK_INVOKED: return "invoked";
    case AP_DEATHLINK_UNAVAILABLE: return "unavailable";
    case AP_DEATHLINK_INVALID_PLAYER: return "invalid_player";
    case AP_DEATHLINK_UNSUPPORTED: return "unsupported";
    case AP_DEATHLINK_ERROR: return "error";
    case AP_DEATHLINK_INVALID: return "invalid";
    default: return "error";
    }
}

static std::string ReadSmallFile(const char* path)
{
    FILE* file = fopen(path, "rb");
    if (!file) return {};
    char buffer[512] = {};
    const size_t count = fread(buffer, 1, sizeof(buffer) - 1, file);
    fclose(file);
    return std::string(buffer, count);
}

static std::string RecordValue(const std::string& record, const char* key)
{
    const std::string prefix = std::string(key) + "=";
    const size_t start = record.find(prefix);
    if (start == std::string::npos) return {};
    const size_t valueStart = start + prefix.size();
    const size_t end = record.find('\n', valueStart);
    return record.substr(valueStart, end == std::string::npos ? end : end - valueStart);
}

static void PublishDeathLinkResult(
    const std::string& eventId,
    const std::string& requestId,
    const std::string& runtimeGeneration,
    const NativeDeathLinkResult& result
)
{
    WriteRuntimeRecord(
        kDeathLinkEventPath,
        "status=" + std::string(DeathLinkStatusName(result.status)) + "\n"
        + "event_id=" + eventId + "\nrequest_id=" + requestId + "\n"
        + "runtime_generation=" + runtimeGeneration + "\n"
        + "transport_succeeded=" + (result.transportSucceeded ? "true" : "false") + "\n"
    );
}

static void PublishNativeEvents(MeathookInterface* interfaceClient, long long& sequence)
{
    if (!interfaceClient) return;
    static std::vector<APEvent> eventRing;
    static std::string ringGeneration;
    const std::string generation = std::to_string(interfaceClient->BindingGeneration());
    if (ringGeneration != generation) {
        ringGeneration = generation;
        eventRing.clear();
    }
    const std::string acknowledgement = ReadSmallFile(kNativeEventAckPath);
    if (RecordValue(acknowledgement, "runtime_generation") == generation) {
        long long acknowledgedSequence = 0;
        try {
            acknowledgedSequence = std::stoll(RecordValue(acknowledgement, "sequence"));
        } catch (...) {
            acknowledgedSequence = 0;
        }
        if (acknowledgedSequence > 0) {
            eventRing.erase(
                std::remove_if(
                    eventRing.begin(), eventRing.end(),
                    [acknowledgedSequence](const APEvent& event) {
                        return static_cast<long long>(event.sequence) <= acknowledgedSequence;
                    }
                ),
                eventRing.end()
            );
        }
    }
    APEventBatch batch = {};
    if (!interfaceClient->GetAPEventsSinceTyped(sequence, batch)) return;
    if (batch.count == 0 && eventRing.empty()) {
        return;
    }
    for (ULONG index = 0; index < batch.count && index < 128; ++index) {
        eventRing.push_back(batch.events[index]);
    }
    if (eventRing.size() > 128) {
        eventRing.erase(eventRing.begin(), eventRing.end() - 128);
    }
    const long long latestSequence = batch.count
        ? static_cast<long long>(batch.latest_sequence)
        : static_cast<long long>(eventRing.back().sequence);
    const long long oldestSequence = eventRing.empty()
        ? static_cast<long long>(batch.oldest_sequence)
        : static_cast<long long>(eventRing.front().sequence);
    std::string record = "status=ready\nlatest_sequence="
        + std::to_string(latestSequence) + "\noldest_sequence="
        + std::to_string(oldestSequence) + "\ngap="
        + (batch.gap ? "true" : "false") + "\nruntime_generation="
        + generation + "\n";
    for (size_t index = 0; index < eventRing.size(); ++index) {
        record += "event_" + std::to_string(index) + "_sequence="
            + std::to_string(eventRing[index].sequence) + "\n"
            + "event_" + std::to_string(index) + "_type="
            + std::to_string(static_cast<int>(eventRing[index].type)) + "\n"
            + "event_" + std::to_string(index) + "_request_id="
            + std::string(eventRing[index].request_id) + "\n";
    }
    sequence = std::max(sequence, latestSequence);
    WriteRuntimeRecord(kNativeEventPath, record);
}

static void ProcessNativeDeathLink(MeathookInterface* interfaceClient)
{
    if (!interfaceClient) return;
    const std::string request = ReadSmallFile(kDeathLinkRequestPath);
    const std::string eventId = RecordValue(request, "event_id");
    const std::string requestId = RecordValue(request, "request_id");
    const std::string requestGeneration = RecordValue(request, "runtime_generation");
    if (eventId.empty() || requestId.empty()) return;
    const std::string currentGeneration = std::to_string(interfaceClient->BindingGeneration());
    if (!requestGeneration.empty() && requestGeneration != currentGeneration) {
        NativeDeathLinkResult result;
        result.status = AP_DEATHLINK_INVALID;
        PublishDeathLinkResult(eventId, requestId, currentGeneration, result);
        return;
    }
    const std::string previous = ReadSmallFile(kDeathLinkEventPath);
    if (RecordValue(previous, "request_id") == requestId) {
        const std::string previousStatus = RecordValue(previous, "status");
        if (previousStatus == "invoked" || previousStatus == "error"
            || previousStatus == "invalid" || previousStatus == "unavailable"
            || previousStatus == "invalid_player" || previousStatus == "unsupported") {
            return;
        }
    }
    const NativeDeathLinkResult result = interfaceClient->ApplyDeathLinkTyped(requestId.c_str());
    PublishDeathLinkResult(eventId, requestId, currentGeneration, result);
}

bool RuntimeDiagnosticRequested(int argc, char** argv) {
    return argc > 2 && std::string(argv[2]) == "--runtime-info";
}

bool InventoryDiagnosticRequested(int argc, char** argv) {
    return argc > 3 && std::string(argv[2]) == "--runtime-inventory";
}

static const char* InventoryStatusName(APInventoryStatus status)
{
    switch (status) {
    case AP_INVENTORY_QUEUED: return "queued";
    case AP_INVENTORY_OK: return "ok";
    case AP_INVENTORY_UNSUPPORTED: return "unsupported";
    case AP_INVENTORY_INVALID_ARGUMENT: return "invalid_argument";
    case AP_INVENTORY_INVALID_PLAYER: return "invalid_player";
    case AP_INVENTORY_ERROR: return "error";
    default: return "error";
    }
}

void PrintInventoryDiagnostic(const char* declName) {
    RuntimeCapabilityRecord capability = g_MhInterface
        ? g_MhInterface->RuntimeCapabilities()
        : RuntimeCapabilityRecord{};
    if (!g_MhInterface || !capability.valid || !capability.protocolCompatible
        || !capability.info.capability_inventory_read) {
        printf(
            "inventory status=unavailable valid=false count=0 error=inventory_capability_unavailable\n"
        );
        return;
    }
    const InventoryItemCountResult result =
        g_MhInterface->GetInventoryItemCountTyped(declName);
    printf(
        "inventory status=%s valid=%s count=%ld error=%s\n",
        InventoryStatusName(result.status),
        result.valid ? "true" : "false",
        result.value,
        result.diagnostic.empty() ? "" : result.diagnostic.c_str()
    );
}

ULONGLONG RuntimeSnapshotAgeMs(const APRuntimeSnapshot& snapshot)
{
    FILETIME fileTime = {};
    GetSystemTimeAsFileTime(&fileTime);
    ULARGE_INTEGER now = {};
    now.LowPart = fileTime.dwLowDateTime;
    now.HighPart = fileTime.dwHighDateTime;
    const ULONGLONG nowMs =
        (now.QuadPart - 116444736000000000ULL) / 10000ULL;
    const ULONGLONG timestamp = static_cast<ULONGLONG>(snapshot.timestamp);
    return timestamp <= nowMs ? nowMs - timestamp : 0;
}

void PrintRuntimeDiagnostic() {
    if (!g_MhInterface || !g_MhInterface->PollRuntimeSnapshot()) {
        printf("runtime_info unavailable\n");
        return;
    }
    RuntimeCapabilityRecord capability = g_MhInterface->RuntimeCapabilities();
    RuntimeSnapshot snapshot;
    g_MhInterface->GetCachedRuntimeSnapshot(snapshot);
    printf("runtime_info protocol_version=%lu build_supported=%d hooks_ready=%d snapshot=%d\n",
        capability.info.protocol_version, capability.info.build_supported,
        capability.info.hooks_ready, capability.info.capability_runtime_snapshot);
    printf("runtime_snapshot sequence=%lld timestamp=%lld age_ms=%llu fresh=%d runtime_ready=%d "
           "player_valid=%d health_valid=%d max_health_valid=%d health=%f max_health=%f "
           "dead_valid=%d dead=%d game_state_valid=%d game_state=%s map_valid=%d map=%s "
           "checkpoint_valid=%d checkpoint=%s\n",
        snapshot.data.sequence, snapshot.data.timestamp,
        RuntimeSnapshotAgeMs(snapshot.data), snapshot.valid,
        snapshot.data.runtime_ready, snapshot.data.player_valid,
        snapshot.data.health_valid, snapshot.data.max_health_valid, snapshot.data.health,
        snapshot.data.max_health, snapshot.data.dead_valid, snapshot.data.dead,
        snapshot.data.game_state_valid,
        RuntimeGameStateName(snapshot.data.game_state),
        snapshot.data.map_valid, snapshot.data.map,
        snapshot.data.checkpoint_valid, snapshot.data.checkpoint);
}

bool StartsWith(const std::string& value, const std::string& prefix) {
    return value.rfind(prefix, 0) == 0;
}

std::optional<std::string> MigratedDirectItemCommand(
    const std::string& filename,
    const std::string& command
) {
    static const std::regex validMapActivation(
        std::string(R"(^ai_ScriptCmdEnt )") + kRpcEntityPrefix
        + R"(_[0-9]+(?:_[0-9]+)? activate$)"
    );
    if (std::regex_match(command, validMapActivation)) {
        return std::nullopt;
    }

    const bool legacyRawEffect =
        StartsWith(command, "give ")
        || StartsWith(command, "chrispy ")
        || StartsWith(command, "g_giveExtraLives ")
        || StartsWith(command, "ai_ScriptCmdEnt player1 givePlayerPerk ");
    if (!legacyRawEffect) {
        return std::nullopt;
    }

    static const std::regex commandIdPattern(
        R"(recv-\d+-item-(\d+)-cmd-(\d+)\.processing)"
    );
    std::smatch match;
    if (!std::regex_match(filename, match, commandIdPattern)) {
        LogDebug(
            "Direct item command left untouched; cannot parse deterministic "
            "command id: " + filename
        );
        return std::nullopt;
    }

    const std::string itemId = match[1].str();
    const std::string commandIndex = match[2].str();
    if (commandIndex == "00") {
        return std::string("ai_ScriptCmdEnt ") + kRpcEntityPrefix + "_" + itemId
            + " activate";
    }
    return std::string("ai_ScriptCmdEnt ") + kRpcEntityPrefix + "_" + itemId
        + "_" + std::to_string(std::stoi(commandIndex)) + " activate";
}

std::optional<std::string> ActiveQueueSessionNamespace() {
    std::ifstream input(kQueueSessionNamespacePath);
    std::string value;
    if (!std::getline(input, value)) return std::nullopt;
    static const std::regex valid(R"(^[0-9a-f]{16}$)");
    if (!std::regex_match(value, valid)) return std::nullopt;
    return value;
}

std::optional<std::string> ReceiptCommandNamespace(const std::string& filename) {
    static const std::regex namespaced(R"(^recv-([0-9a-f]{16})-.*\.(cmd|processing)$)");
    std::smatch match;
    if (std::regex_match(filename, match, namespaced)) return match[1].str();
    if (StartsWith(filename, "recv-")) return std::string(); // unscoped receipt
    return std::nullopt; // generic command
}

void EnsureQueueDirectory(
    CommandSourceMap& recoveredSources,
    std::unordered_set<std::string>& heldReceiptLogs,
    const std::optional<std::string>& activeNamespace
) {
    CreateDirectoryA(kQueueDirectory, nullptr);
    // Telemetry polls are scoped to the active game session.
    DeleteFileA("base\\ap_queue\\telemetry.cmd");
    DeleteFileA("base\\ap_queue\\telemetry.processing");

    // Resume gameplay commands owned by the active queue.
    WIN32_FIND_DATAA data = {};
    HANDLE find = FindFirstFileA("base\\ap_queue\\*.processing", &data);
    if (find == INVALID_HANDLE_VALUE) return;
    do {
        if (!(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
            const std::string filename = data.cFileName;
            const std::string processingPath = std::string(kQueueDirectory) + "\\" + filename;
            const auto receiptNamespace = ReceiptCommandNamespace(filename);
            if (receiptNamespace.has_value()) {
                if (!activeNamespace.has_value() || receiptNamespace.value() != activeNamespace.value()) {
                    if (heldReceiptLogs.insert(filename).second) {
                        const std::string reason = receiptNamespace->empty()
                            ? "legacy_unnamespaced"
                            : (!activeNamespace.has_value() ? "session_unavailable" : "foreign_session");
                        LogDebug("QUEUE_SESSION_HOLD command_id=" + CommandIdFromPath(processingPath)
                            + " reason=" + reason);
                    }
                    continue;
                }
            }
            const std::string queuedPath =
                processingPath.substr(0, processingPath.size() - std::string(".processing").size()) + ".cmd";
            std::string command;
            if (ReadCommandFile(processingPath, command)) {
                const std::optional<std::string> migrated =
                    MigratedDirectItemCommand(data.cFileName, command);
                if (migrated.has_value()) {
                    if (WriteCommandFile(processingPath, migrated.value())) {
                        LogDebug(
                            "MIGRATED_DIRECT_ITEM_COMMAND_TO_MAP_ENTITY command_id="
                            + CommandIdFromPath(processingPath)
                            + " old=" + command
                            + " new=" + migrated.value()
                        );
                    } else {
                        LogDebug(
                            "Failed to rewrite unsafe direct command before "
                            "requeue: " + processingPath
                        );
                    }
                }
            }
            if (MoveFileExA(
                    processingPath.c_str(), queuedPath.c_str(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
                )) {
                const std::string commandId = CommandIdFromPath(processingPath);
                recoveredSources[commandId] = "recovered_processing";
                LogDebug(
                    "QUEUE_SESSION_RECOVER command_id=" + commandId
                    + " source=recovered_processing"
                    + DeliveryContextFields()
                );
            }
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

void ImportSpoolFiles(
    std::deque<CommandJob>& queue,
    std::unordered_set<std::string>& knownCommandIds,
    CommandSourceMap& recoveredSources,
    std::unordered_set<std::string>& heldReceiptLogs,
    const std::optional<std::string>& activeNamespace
) {
    size_t duplicateCount = 0;
    for (const std::string& queuedPath : FindQueuedFiles()) {
        const std::string filename = std::filesystem::path(queuedPath).filename().string();
        const auto receiptNamespace = ReceiptCommandNamespace(filename);
        if (receiptNamespace.has_value()
                && (!activeNamespace.has_value()
                    || receiptNamespace.value() != activeNamespace.value())) {
            if (heldReceiptLogs.insert(filename).second) {
                const std::string reason = receiptNamespace->empty()
                    ? "legacy_unnamespaced"
                    : (!activeNamespace.has_value() ? "session_unavailable" : "foreign_session");
                LogDebug("QUEUE_SESSION_HOLD command_id=" + CommandIdFromPath(queuedPath)
                    + " reason=" + reason);
            }
            continue;
        }
        const std::string processingPath = queuedPath.substr(0, queuedPath.size() - 4) + ".processing";
        if (!MoveFileExA(queuedPath.c_str(), processingPath.c_str(), MOVEFILE_WRITE_THROUGH)) {
            continue;
        }

        std::string command;
        if (ReadCommandFile(processingPath, command)) {
            const std::string commandId = CommandIdFromPath(processingPath);
            if (!RememberRpcCommandId(knownCommandIds, commandId)) {
                ++duplicateCount;
                LogDebug(
                    "QUEUE_DUPLICATE_REJECT command_id=" + commandId
                    + " reason=known_command_id"
                    + DeliveryContextFields()
                );
                continue;
            }
            const auto recovered = recoveredSources.find(commandId);
            const std::string source = recovered == recoveredSources.end()
                ? "cmd" : recovered->second;
            if (recovered != recoveredSources.end()) {
                recoveredSources.erase(recovered);
            }
            queue.push_back({
                processingPath,
                command,
                0,
                0,
                source,
                GetTickCount(),
                receiptNamespace,
            });
            LogDebug(
                "QUEUE_IMPORT command_id=" + commandId
                + " source=" + source
                + DeliveryContextFields()
            );
        } else {
            LogDebug("Discarding unreadable/empty queue file: " + processingPath);
            DeleteFileA(processingPath.c_str());
        }
    }
    if (duplicateCount > 0) {
        LogDebug("RPC_QUEUE_DEDUPE count=" + std::to_string(duplicateCount));
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

bool ExecuteCommand(const CommandJob& job) {
    const std::string commandId = CommandIdFromPath(job.path);
    const std::string& command = job.command;
    LogDebug("RPC_EXECUTE command_id=" + commandId + DeliveryContextFields());
    if (g_MhInterface) {
        g_MhInterface->SetCurrentCommandId(commandId);
    }

    RpcWatchdogContext* watchdog = new RpcWatchdogContext();
    watchdog->startTick = GetTickCount();
    watchdog->commandId = commandId;
    watchdog->operation = "ExecuteConsoleCommand";
    HANDLE watchdogThread = CreateThread(nullptr, 0, RpcCallWatchdog, watchdog, 0, nullptr);

    if (command.rfind("#DUMP_ENTITIES", 0) == 0) {
        const size_t bufferSize = 128 * 1024 * 1024;
        unsigned char* buffer = static_cast<unsigned char*>(malloc(bufferSize));
        if (!buffer) {
            InterlockedExchange(&watchdog->completed, 1);
            if (watchdogThread) CloseHandle(watchdogThread);
            return false;
        }
        size_t actualSize = bufferSize;
        const bool success = g_MhInterface->GetEntitiesFile(buffer, &actualSize);
        if (success) {
            FILE* output = fopen("base\\map.entities", "wb");
            if (output) {
                fwrite(buffer, 1, actualSize, output);
                fclose(output);
            } else {
                free(buffer);
                InterlockedExchange(&watchdog->completed, 1);
                if (watchdogThread) CloseHandle(watchdogThread);
                return false;
            }
        }
        free(buffer);
        InterlockedExchange(&watchdog->completed, 1);
        if (watchdogThread) CloseHandle(watchdogThread);
        return success;
    }

    if (command.rfind("#PUSH_ENTITIES ", 0) == 0) {
        std::string path = command.substr(15);
        const bool success = g_MhInterface->PushEntitiesFile(path.data(), nullptr, 0);
        InterlockedExchange(&watchdog->completed, 1);
        if (watchdogThread) CloseHandle(watchdogThread);
        return success;
    }

    const bool success = g_MhInterface->ExecuteConsoleCommand(
        reinterpret_cast<unsigned char*>(const_cast<char*>(command.c_str()))
    );
    InterlockedExchange(&watchdog->completed, 1);
    if (watchdogThread) CloseHandle(watchdogThread);
    return success;
}

int main(int argc, char** argv) {
    if (argc > 1 && !SetCurrentDirectoryA(argv[1])) {
        printf("Failed to set DOOM working directory: %s\n", argv[1]);
        return 1;
    }
    RotateClientLog();

    char executablePath[MAX_PATH] = {};
    if (GetModuleFileNameA(nullptr, executablePath, MAX_PATH) == 0) {
        printf("Failed to resolve ap_client.exe path.\n");
        return 1;
    }
    const std::string workingDirectory = CurrentWorkingDirectory();
    RuntimeEnvSignals envSignals;
    if (const char* value = getenv("WINEDLLOVERRIDES")) {
        envSignals.wineDllOverrides = value;
    }
    if (const char* value = getenv("WINEPREFIX")) {
        envSignals.winePrefix = value;
    }
    if (const char* value = getenv("STEAM_COMPAT_DATA_PATH")) {
        envSignals.steamCompatDataPath = value;
    }
    if (const char* value = getenv("STEAM_COMPAT_CLIENT_INSTALL_PATH")) {
        envSignals.steamCompatClientInstallPath = value;
    }
    if (const char* value = getenv("PROTON_LOG")) {
        envSignals.protonLog = value;
    }

    const RuntimePathInfo runtimePaths = ResolveRuntimePathInfo(
        executablePath,
        workingDirectory,
        envSignals
    );
    const std::string doomExecutablePath =
        (runtimePaths.gameRootDir / "DOOMEternalx64vk.exe").string();
    MissionTransitionMonitor missionTransitionMonitor(runtimePaths);

    HANDLE singleInstance = CreateMutexA(nullptr, TRUE, "DoomEternalArchipelagoClient");
    if (!singleInstance || GetLastError() == ERROR_ALREADY_EXISTS) {
        LogDebug("Another AP Client instance is already running; exiting.");
        if (singleInstance) CloseHandle(singleInstance);
        return 0;
    }

    CommandSourceMap recoveredSources;
    // A previous bridge process may have left its room marker behind. Do not
    // trust it for startup recovery; current bridge publishes only after
    // authoritative Connected identity.
    bool startupNamespaceCleared = DeleteFileA(kQueueSessionNamespacePath) != FALSE;
    if (!startupNamespaceCleared) {
        const DWORD error = GetLastError();
        startupNamespaceCleared = error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND;
        if (!startupNamespaceCleared) {
            LogDebug(
                "QUEUE_SESSION_MARKER_CLEAR_RETRY error=" + std::to_string(error)
            );
        }
    }
    std::unordered_set<std::string> heldReceiptLogs;
    EnsureQueueDirectory(recoveredSources, heldReceiptLogs, std::nullopt);
    DeleteFileA(kRpcGatePath);
    const QueueSnapshot startupQueueSnapshot = CountQueueFiles();
    const MeathookPreflightResult preflight = InspectMeathookInstallation(runtimePaths);
    LogStartupHeader(
        executablePath,
        workingDirectory,
        doomExecutablePath,
        startupQueueSnapshot,
        preflight,
        runtimePaths
    );
    LogDebug("RPC command execution is PAUSED. Use /doom_rpc_on inside a loaded level.");

    GameStateProbe gameStateProbe(LogDebug);
    const bool meathookPreflightPassed = preflight.deliveryAllowed;
    if (!meathookPreflightPassed) {
        LogDebug(
            "Meathook preflight failed. No valid XINPUT1_3.dll candidate was accepted. "
            "Queued commands will remain pending until the client is restarted "
            "with a valid install."
        );
    } else {
        g_MhInterface = new MeathookInterface();
        g_MhInterface->SetLogCallback(LogDebug);
        LogDebug(
            "Meathook RPC client binding initialized. Waiting for the in-game "
            "Meathook server..."
        );
        LogDebug(
            "Meathook RPC startup is bounded; runtime capability polling is active."
        );
    }
    PublishRuntimeCapability();
    if (RuntimeDiagnosticRequested(argc, argv) || InventoryDiagnosticRequested(argc, argv)) {
        if (RuntimeDiagnosticRequested(argc, argv)) {
            PrintRuntimeDiagnostic();
        } else {
            PrintInventoryDiagnostic(argv[3]);
        }
        delete g_MhInterface;
        g_MhInterface = nullptr;
        ReleaseMutex(singleInstance);
        CloseHandle(singleInstance);
        return 0;
    }

    std::deque<CommandJob> queue;
    std::unordered_set<std::string> knownCommandIds;
    DWORD lastExecution = 0;
    DWORD lastQueueStateLog = 0;
    size_t acknowledgedCommands = 0;
    long long nativeEventSequence = 0;
    std::string nativeEventGeneration;
    bool queueWasActive = false;
    bool lastRpcArmed = false;
    bool lastRpcEnabled = false;
    std::string lastGateReason;
    DWORD lastStallLog = 0;

    while (true) {
        gameStateProbe.Poll();
        PublishRuntimeCapability();
        const std::string bindingGeneration = std::to_string(
            g_MhInterface ? g_MhInterface->BindingGeneration() : 0
        );
        if (bindingGeneration != nativeEventGeneration) {
            nativeEventGeneration = bindingGeneration;
            nativeEventSequence = 0;
            DeleteFileA(kNativeEventPath);
        }
        ProcessNativeDeathLink(g_MhInterface);
        PublishNativeEvents(g_MhInterface, nativeEventSequence);
        missionTransitionMonitor.Poll(
            gameStateProbe.IsGameplayLoaded(),
            gameStateProbe.IsLoading()
        );
        std::optional<std::string> activeNamespace;
        if (startupNamespaceCleared) {
            activeNamespace = ActiveQueueSessionNamespace();
        } else if (DeleteFileA(kQueueSessionNamespacePath)) {
            startupNamespaceCleared = true;
        } else {
            const DWORD error = GetLastError();
            if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND) {
                startupNamespaceCleared = true;
            }
        }
        EnsureQueueDirectory(recoveredSources, heldReceiptLogs, activeNamespace);
        ImportSpoolFiles(
            queue,
            knownCommandIds,
            recoveredSources,
            heldReceiptLogs,
            activeNamespace
        );

        const DWORD now = GetTickCount();
        bool rpcArmed = IsRpcExecutionEnabled();
        if (!queue.empty() && !rpcArmed) {
            if (ArmRpcExecution()) {
                rpcArmed = true;
                LogDebug("RPC command execution auto-armed because commands are pending.");
            }
        }
        const bool rpcTransportReady =
            meathookPreflightPassed && g_MhInterface && g_MhInterface->IsInitialized();
        const bool rpcEnabled =
            rpcArmed && rpcTransportReady && gameStateProbe.IsSafeForRpc();
        const std::string gateReason = RpcGateReason(
            rpcArmed, rpcTransportReady, gameStateProbe
        );
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
        if (gateReason != lastGateReason) {
            LogDebug(
                "GATE_TRANSITION state=" + std::string(rpcEnabled ? "open" : "closed")
                + " reason=" + gateReason
                + DeliveryContextFields()
            );
            lastGateReason = gateReason;
        }
        if (!rpcEnabled) {
            DiscardTelemetryJobs(queue);
        }
        const bool queueActive = !queue.empty();
        if ((queueActive || queueWasActive)
                && now - lastQueueStateLog >= kQueueStateLogMs) {
            LogDebug(
                "RPC_QUEUE pending=" + std::to_string(queue.size())
                + " in_flight=0 acked=" + std::to_string(acknowledgedCommands)
            );
            lastQueueStateLog = now;
        }
        queueWasActive = queueActive;

        if (!queue.empty() && !rpcEnabled && now - lastStallLog >= kRpcStallWarnMs) {
            const DWORD oldestAge = now - queue.front().importedTick;
            const QueueSnapshot diskQueue = CountQueueFiles();
            LogDebug(
                "QUEUE_STALL oldest_age_ms=" + std::to_string(oldestAge)
                + " pending_count=" + std::to_string(queue.size())
                + " processing_count=" + std::to_string(diskQueue.processing)
                + " in_flight_id=none gate=" + gateReason
                + DeliveryContextFields()
            );
            lastStallLog = now;
        }

        bool dispatchNextImmediately = false;
        if (!queue.empty() && rpcEnabled && g_MhInterface->IsInitialized()) {
            CommandJob& job = queue.front();
            const std::string commandId = CommandIdFromPath(job.path);
            const bool normalReceipt = IsNormalReceiptCommandId(commandId);
            const bool dispatchReady = normalReceipt
                ? ReceiptDispatchReady(now, job.nextAttemptTick)
                : now - lastExecution >= kCommandSpacingMs;
            if (!dispatchReady) {
                Sleep(50);
                continue;
            }
            if (GetFileAttributesA(job.path.c_str()) == INVALID_FILE_ATTRIBUTES) {
                LogDebug(
                    "QUEUE_CANCELLED command_id=" + commandId + DeliveryContextFields()
                );
                knownCommandIds.erase(commandId);
                queue.pop_front();
                continue;
            }
            if (job.receiptNamespace.has_value()) {
                const std::optional<std::string> dispatchNamespace =
                    startupNamespaceCleared ? ActiveQueueSessionNamespace() : std::nullopt;
                if (!dispatchNamespace.has_value()
                        || dispatchNamespace.value() != job.receiptNamespace.value()) {
                    const std::string reason = !dispatchNamespace.has_value()
                        ? "session_unavailable" : "foreign_session";
                    LogDebug(
                        "QUEUE_SESSION_HOLD command_id=" + commandId
                        + " reason=" + reason
                    );
                    // Leave .processing in place. EnsureQueueDirectory owns recovery;
                    // bridge owns only .cmd and may quarantine foreign receipts.
                    knownCommandIds.erase(commandId);
                    queue.pop_front();
                    continue;
                }
            }
            const DWORD dispatchTick = GetTickCount();
            if (normalReceipt) {
                LogDebug(
                    "RPC_DISPATCH command_id=" + commandId
                    + " kind=" + ReceiptCommandKind(commandId)
                    + " source=" + job.source
                    + " age_ms=" + std::to_string(now - job.importedTick)
                    + DeliveryContextFields()
                );
            }
            if (ExecuteCommand(job)) {
                DeleteFileA(job.path.c_str());
                if (normalReceipt) {
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=" + ReceiptCommandKind(commandId)
                        + " result=ack_executed_persistence_unknown"
                        + " elapsed_ms="
                        + std::to_string(GetTickCount() - dispatchTick)
                        + DeliveryContextFields()
                    );
                    LogDebug(
                        "ACK_REMOVE command_id=" + commandId
                        + " path=" + std::filesystem::path(job.path).filename().string()
                        + DeliveryContextFields()
                    );
                    dispatchNextImmediately = true;
                } else {
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=non_receipt result=ack_executed_persistence_unknown"
                        + DeliveryContextFields()
                    );
                }
                ++acknowledgedCommands;
                knownCommandIds.erase(commandId);
                queue.pop_front();
            } else {
                if (normalReceipt) {
                    ++job.retryAttempt;
                    const DWORD delay = ReceiptRetryDelayMs(job.retryAttempt);
                    job.nextAttemptTick = GetTickCount() + delay;
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=" + ReceiptCommandKind(commandId)
                        + " result=retry"
                        + " attempt=" + std::to_string(job.retryAttempt)
                        + " delay_ms=" + std::to_string(delay)
                        + " reason="
                        + RpcCallResultName(g_MhInterface->m_LastRpcCallResult)
                        + "/" + std::to_string(g_MhInterface->m_LastTransportError)
                        + DeliveryContextFields()
                    );
                } else {
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=non_receipt result=retry"
                        + " transport=" + RpcCallResultName(g_MhInterface->m_LastRpcCallResult)
                        + " wait_error=" + std::to_string(g_MhInterface->m_LastTransportError)
                        + DeliveryContextFields()
                    );
                    DeleteFileA(kRpcGatePath);
                }
            }
            lastExecution = GetTickCount();
        }

        if (dispatchNextImmediately) {
            continue;
        }

        Sleep(50);
    }

    ReleaseMutex(singleInstance);
    CloseHandle(singleInstance);
}
