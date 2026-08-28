#include <windows.h>
#include <bcrypt.h>
#include <io.h>
#include <tlhelp32.h>
#include <winver.h>
#include <cstdlib>
#include <stdio.h>
#include <algorithm>
#include <array>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <memory>
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
#include "ap_runtime_rpc_client.h"
#include "ap_rpc_health_state.h"
#include "rpc_queue_policy.h"
#include "ammo_hotkey.h"

ApRuntimeRpcClient* g_ApRpc = nullptr;
std::unique_ptr<ApRuntimeRpcClient> g_ApRpcOwner;

static const char* kQueueDirectory = "base\\ap_queue";
static const char* kQueueSessionNamespacePath = "base\\ap_queue\\active_session_namespace";
static const char* kMaterializationLeasePath = "base\\ap_queue\\active_materialization_lease";
static const char* kMaterializationLeaseHeader = "AP_MATERIALIZATION_LEASE_V1";
static const char* kExecutionClassHeader = "AP_EXECUTION_CLASS_V1";
static const char* kDiagnosticCondumpHeader = "AP_DIAGNOSTIC_CONDUMP_V1";
static const char* kMapEntityOperationHeader = "AP_MAP_ENTITY_OPERATION_V1";
static const char* kRpcGatePath = "base\\ap_rpc_enabled";
static const char* kTransitionEventPrefix = "base\\ap_transition_";
static const char* kGameplaySaveEvidencePath = "base\\ap_gameplay_save.state";
static const char* kReleaseVersion = "0.5.0";
static const char* kRpcEntityPrefix = "ap_rpc_v3";
static const int kRpcEntityContractRevision = 3;
static const int kNativeCommandPolicyRevision = 10;
static const ULONGLONG kSteamId64Base = 76561197960265728ULL;
static const DWORD kCommandSpacingMs = 250;
static const DWORD kQueueStateLogMs = 5000;
static const DWORD kGoalMonitorPollMs = 1000;
static const DWORD kRpcStallWarnMs = 15000;
static const DWORD kQueueNoiseSummaryMs = 12000;
static const DWORD kDeliveredRemovalRetryMs = 1000;
static const unsigned int kDiagnosticRetryLimit = 3;
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

enum class CommandExecutionClass {
    PlayerRuntime,
    MapEntitySafe,
};

enum class MapEntityOperation {
    None,
    CheckedVisualHide,
    FastTravelUnlock,
};

struct CommandJob {
    std::string path;
    std::string command;
    unsigned int retryAttempt = 0;
    DWORD nextAttemptTick = 0;
    std::string source = "cmd";
    DWORD importedTick = 0;
    std::optional<std::string> receiptNamespace;
    std::optional<std::string> materializationLease;
    CommandExecutionClass executionClass = CommandExecutionClass::PlayerRuntime;
    MapEntityOperation mapEntityOperation = MapEntityOperation::None;
    bool diagnosticCondump = false;
};

using CommandSourceMap = std::unordered_map<std::string, std::string>;

struct DeliveredSpool {
    std::string path;
    DWORD nextRemovalAttemptTick = 0;
    unsigned int removalAttempt = 0;
};

struct QueueDedupeLogState {
    bool active = false;
    DWORD nextSummaryTick = 0;
    size_t suppressed = 0;
};

QueueDedupeLogState g_QueueDedupeLogState;

std::string DeliveryContextFields();


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

void NoteQueueDedupe(const std::string& commandId) {
    const DWORD now = GetTickCount();
    if (!g_QueueDedupeLogState.active) {
        g_QueueDedupeLogState.active = true;
        g_QueueDedupeLogState.nextSummaryTick = now + kQueueNoiseSummaryMs;
        g_QueueDedupeLogState.suppressed = 0;
        LogDebug(
            "QUEUE_DUPLICATE_REJECT command_id=" + commandId
            + " reason=known_command_id RPC_QUEUE_DEDUPE transition=active"
            + DeliveryContextFields()
        );
        return;
    }
    ++g_QueueDedupeLogState.suppressed;
    if (static_cast<LONG>(now - g_QueueDedupeLogState.nextSummaryTick) >= 0) {
        LogDebug(
            "RPC_QUEUE_DEDUPE summary suppressed="
            + std::to_string(g_QueueDedupeLogState.suppressed)
        );
        g_QueueDedupeLogState.suppressed = 0;
        g_QueueDedupeLogState.nextSummaryTick = now + kQueueNoiseSummaryMs;
    }
}

void RecoverQueueDedupeLog() {
    if (!g_QueueDedupeLogState.active) {
        return;
    }
    LogDebug(
        "RPC_QUEUE_DEDUPE recovery suppressed="
        + std::to_string(g_QueueDedupeLogState.suppressed)
    );
    g_QueueDedupeLogState = {};
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

const char* RpcCallResultName(ApRpcResult result) {
    switch (result) {
    case AP_RPC_PIPE_MISSING:
        return "PIPE_NOT_FOUND";
    case AP_RPC_PIPE_BUSY:
        return "PIPE_BUSY";
    case AP_RPC_WAIT_TIMEOUT:
        return "WAIT_NAMED_PIPE_TIMEOUT";
    case AP_RPC_DELIVERED:
        return "RPC_CALL_DELIVERED";
    case AP_RPC_EXCEPTION:
        return "RPC_EXCEPTION";
    case AP_RPC_UNKNOWN:
        return "UNKNOWN_TRANSPORT_ERROR";
    case AP_RPC_NONE:
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
    LogDebug("Offset profile: dynamic-build-profiles");
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

    void Poll(bool gameplayLoaded, bool loading, bool nativeSafe) {
        const DWORD now = GetTickCount();
        const bool stateChanged = !gameplayStateInitialized_
            || gameplayLoaded != gameplayLoaded_
            || !loadingStateInitialized_
            || loading != loading_
            || !nativeSafeInitialized_
            || nativeSafe != nativeSafe_;
        if (!stateChanged && now < nextPollTick_) {
            return;
        }
        nextPollTick_ = now + kGoalMonitorPollMs;
        const bool nativeSafeChanged = !nativeSafeInitialized_ || nativeSafe != nativeSafe_;
        nativeSafeInitialized_ = true;
        nativeSafe_ = nativeSafe;

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
            lastSnapshotProvisional_ = provisional;
            WriteGameplayEvidence(entered, provisional);
            return;
        }

        if (!gameplayLoaded_) {
            return;
        }

        if (nativeSafeChanged && !lastSnapshot_.path.empty()) {
            WriteGameplayEvidence(lastSnapshot_, lastSnapshotProvisional_);
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
        lastSnapshotProvisional_ = provisional;
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
                + "native_safe=" + std::string(nativeSafe_ ? "true" : "false") + "\n"
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
    bool lastSnapshotProvisional_ = false;
    std::string activeSlotDirectory_;
    std::vector<std::pair<std::string, long long>> menuSlotTokens_;
    unsigned long long sequence_ = 0;
    unsigned long long gameplayEpoch_ = 0;
    bool gameplayStateInitialized_ = false;
    bool gameplayLoaded_ = false;
    bool loadingStateInitialized_ = false;
    bool loading_ = false;
    bool nativeSafeInitialized_ = false;
    bool nativeSafe_ = false;
    bool sawLoadingForEpoch_ = false;
    DWORD nextPollTick_ = 0;
    DWORD nextConfigRetryTick_ = 0;
};

bool ReadCommandFile(
    const std::string& path,
    std::string& command,
    std::optional<std::string>* materializationLease = nullptr,
    CommandExecutionClass* executionClass = nullptr,
    MapEntityOperation* mapEntityOperation = nullptr,
    bool* diagnosticCondump = nullptr
) {
    FILE* file = fopen(path.c_str(), "rb");
    if (!file) return false;

    char buffer[4096] = {};
    const size_t read = fread(buffer, 1, sizeof(buffer) - 1, file);
    fclose(file);
    const std::string contents(buffer, read);
    if (materializationLease) materializationLease->reset();
    if (executionClass) *executionClass = CommandExecutionClass::PlayerRuntime;
    if (mapEntityOperation) *mapEntityOperation = MapEntityOperation::None;
    if (diagnosticCondump) *diagnosticCondump = false;

    bool sawExecutionClass = false;
    bool sawMaterializationLease = false;
    bool sawMapEntityOperation = false;
    bool sawDiagnosticCondump = false;
    CommandExecutionClass parsedExecutionClass = CommandExecutionClass::PlayerRuntime;
    MapEntityOperation parsedMapEntityOperation = MapEntityOperation::None;
    size_t payloadOffset = 0;
    const std::string leasePrefix = std::string(kMaterializationLeaseHeader) + " ";
    const std::string executionPrefix = std::string(kExecutionClassHeader) + " ";
    const std::string operationPrefix = std::string(kMapEntityOperationHeader) + " ";
    const std::string diagnosticPrefix = std::string(kDiagnosticCondumpHeader) + " ";
    while (payloadOffset < contents.size()) {
        const size_t lineEnd = contents.find('\n', payloadOffset);
        const std::string line = TrimLine(contents.substr(
            payloadOffset,
            lineEnd == std::string::npos ? contents.size() - payloadOffset : lineEnd - payloadOffset
        ));

        if (line.rfind(std::string(kExecutionClassHeader), 0) == 0) {
            if (sawExecutionClass || line.rfind(executionPrefix, 0) != 0) {
                return false;
            }
            const std::string value = line.substr(executionPrefix.size());
            if (value == "PLAYER_RUNTIME") {
                parsedExecutionClass = CommandExecutionClass::PlayerRuntime;
            } else if (value == "MAP_ENTITY_SAFE") {
                parsedExecutionClass = CommandExecutionClass::MapEntitySafe;
            } else {
                return false;
            }
            sawExecutionClass = true;
        } else if (line.rfind(std::string(kMapEntityOperationHeader), 0) == 0) {
            if (sawMapEntityOperation || line.rfind(operationPrefix, 0) != 0) {
                return false;
            }
            const std::string value = line.substr(operationPrefix.size());
            if (value == "CHECKED_VISUAL_HIDE") {
                parsedMapEntityOperation = MapEntityOperation::CheckedVisualHide;
            } else if (value == "FAST_TRAVEL_UNLOCK") {
                parsedMapEntityOperation = MapEntityOperation::FastTravelUnlock;
            } else {
                return false;
            }
            sawMapEntityOperation = true;
        } else if (line.rfind(std::string(kMaterializationLeaseHeader), 0) == 0) {
            if (sawMaterializationLease || line.rfind(leasePrefix, 0) != 0) {
                return false;
            }
            const std::string lease = line.substr(leasePrefix.size());
            static const std::regex validLease(R"(^[0-9]+:[0-9]+$)");
            if (!std::regex_match(lease, validLease)) {
                return false;
            }
            if (materializationLease) *materializationLease = lease;
            sawMaterializationLease = true;
        } else if (line.rfind(std::string(kDiagnosticCondumpHeader), 0) == 0) {
            if (sawDiagnosticCondump || line != diagnosticPrefix + "AP_SUPPORT_FILE.txt") {
                return false;
            }
            sawDiagnosticCondump = true;
        } else {
            if (line.rfind("AP_", 0) == 0) {
                return false;
            }
            break;
        }

        if (lineEnd == std::string::npos) {
            return false;
        }
        payloadOffset = lineEnd + 1;
    }

    command = TrimLine(contents.substr(payloadOffset));
    if (sawDiagnosticCondump && command != "condump AP_SUPPORT_FILE.txt") {
        return false;
    }
    if (sawDiagnosticCondump && (sawExecutionClass || sawMapEntityOperation || sawMaterializationLease)) {
        return false;
    }
    if (parsedExecutionClass == CommandExecutionClass::MapEntitySafe
            && parsedMapEntityOperation == MapEntityOperation::None) {
        return false;
    }
    if (parsedExecutionClass == CommandExecutionClass::PlayerRuntime
            && parsedMapEntityOperation != MapEntityOperation::None) {
        return false;
    }
    if (executionClass) *executionClass = parsedExecutionClass;
    if (mapEntityOperation) *mapEntityOperation = parsedMapEntityOperation;
    if (diagnosticCondump) *diagnosticCondump = sawDiagnosticCondump;
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

std::optional<std::string> ActiveMaterializationLease() {
    std::ifstream input(kMaterializationLeasePath);
    std::string value;
    if (!std::getline(input, value)) return std::nullopt;
    value = TrimLine(value);
    const std::string prefix = std::string(kMaterializationLeaseHeader) + " ";
    if (!StartsWith(value, prefix)) return std::nullopt;
    value = value.substr(prefix.size());
    static const std::regex validLease(R"(^[0-9]+:[0-9]+$)");
    if (!std::regex_match(value, validLease)) return std::nullopt;
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
    const std::optional<std::string>& activeNamespace,
    const std::unordered_set<std::string>& activeCommandIds
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
            const std::string commandId = CommandIdFromPath(processingPath);
            if (activeCommandIds.find(commandId) != activeCommandIds.end()) {
                continue;
            }
            const std::string queuedPath =
                processingPath.substr(0, processingPath.size() - std::string(".processing").size()) + ".cmd";
            std::string command;
            CommandExecutionClass executionClass = CommandExecutionClass::PlayerRuntime;
            if (ReadCommandFile(processingPath, command, nullptr, &executionClass)
                    && executionClass == CommandExecutionClass::PlayerRuntime) {
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

bool MapEntityOperationMatchesCommand(
    MapEntityOperation operation,
    const std::string& command
) {
    if (operation == MapEntityOperation::FastTravelUnlock) {
        return command == "ai_ScriptCmdEnt ap_fast_travel_unlock activate";
    }
    if (operation != MapEntityOperation::CheckedVisualHide) return false;
    static const std::regex cleanup(
        R"(^ai_ScriptCmdEnt ap_hide_location_visual_[0-9]+ activate$)"
    );
    return std::regex_match(command, cleanup);
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
        const std::string queuedCommandId = CommandIdFromPath(queuedPath);
        if (knownCommandIds.find(queuedCommandId) != knownCommandIds.end()) {
            ++duplicateCount;
            NoteQueueDedupe(queuedCommandId);
            DeleteFileA(queuedPath.c_str());
            continue;
        }
        const std::string processingPath = queuedPath.substr(0, queuedPath.size() - 4) + ".processing";
        if (!MoveFileExA(queuedPath.c_str(), processingPath.c_str(), MOVEFILE_WRITE_THROUGH)) {
            continue;
        }

        std::string command;
        std::optional<std::string> materializationLease;
        CommandExecutionClass executionClass = CommandExecutionClass::PlayerRuntime;
        MapEntityOperation mapEntityOperation = MapEntityOperation::None;
        bool diagnosticCondump = false;
        if (ReadCommandFile(
                processingPath,
                command,
                &materializationLease,
                &executionClass,
                &mapEntityOperation,
                &diagnosticCondump
        )) {
            if (executionClass == CommandExecutionClass::MapEntitySafe
                    && !MapEntityOperationMatchesCommand(mapEntityOperation, command)) {
                LogDebug(
                    "Discarding MAP_ENTITY_SAFE command with invalid operation payload: "
                    + processingPath
                );
                DeleteFileA(processingPath.c_str());
                continue;
            }
            const std::string commandId = CommandIdFromPath(processingPath);
            if (!RememberRpcCommandId(knownCommandIds, commandId)) {
                ++duplicateCount;
                NoteQueueDedupe(commandId);
                DeleteFileA(processingPath.c_str());
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
                materializationLease,
                executionClass,
                mapEntityOperation,
                diagnosticCondump,
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
    if (duplicateCount == 0) {
        RecoverQueueDedupeLog();
    }
}

bool IsTelemetryJob(const CommandJob& job) {
    const size_t separator = job.path.find_last_of("\\/");
    const std::string filename =
        separator == std::string::npos ? job.path : job.path.substr(separator + 1);
    return filename.rfind("telemetry.", 0) == 0;
}

bool IsSilentMaintenanceJob(const CommandJob& job) {
    const std::string commandId = CommandIdFromPath(job.path);
    return StartsWith(commandId, "reconcile-")
        || commandId.find("-reconcile-") != std::string::npos
        || StartsWith(commandId, "automap-cleanup-")
        || commandId.find("-automap-cleanup-") != std::string::npos
        || job.mapEntityOperation == MapEntityOperation::CheckedVisualHide;
}

bool IsFreshReceiptCommandId(const std::string& commandId) {
    static const std::regex receipt(
        R"(^recv-[0-9a-f]{16}-[0-9]+-item-[0-9]+-cmd-[0-9]+(?:-notify)?$)"
    );
    return std::regex_match(commandId, receipt);
}

bool IsMapEntitySafeJob(const CommandJob& job) {
    return job.executionClass == CommandExecutionClass::MapEntitySafe;
}

bool MapEntitySafeLeaseMatchesCurrent(const CommandJob& job) {
    if (!IsMapEntitySafeJob(job)) return true;
    if (!MapEntityOperationMatchesCommand(job.mapEntityOperation, job.command)) return false;
    const std::optional<std::string> currentLease = ActiveMaterializationLease();
    return job.materializationLease.has_value()
        && currentLease.has_value()
        && job.materializationLease.value() == currentLease.value();
}

bool CommandExecutionGateOpen(
    const CommandJob& job,
    bool rpcArmed,
    bool rpcTransportReady,
    const GameStateProbe& gameStateProbe
) {
    if (job.diagnosticCondump) {
        // Support condump bypasses gameplay/RPC safety gates, but still needs
        // live native transport, which is only healthy while game is open.
        return rpcTransportReady;
    }
    if (!rpcArmed || !rpcTransportReady) return false;
    if (IsMapEntitySafeJob(job)) {
        return gameStateProbe.IsMapEntitySafe()
            && MapEntitySafeLeaseMatchesCurrent(job);
    }
    return gameStateProbe.IsSafeForRpc();
}

void DiscardMapEntitySafeJobsWithMismatchedLease(
    std::deque<CommandJob>& queue,
    std::unordered_set<std::string>& knownCommandIds,
    const std::optional<std::string>& currentLease
) {
    auto job = queue.begin();
    while (job != queue.end()) {
        if (!IsMapEntitySafeJob(*job)
                || (job->materializationLease.has_value()
                    && currentLease.has_value()
                    && job->materializationLease.value() == currentLease.value())) {
            ++job;
            continue;
        }
        const std::string commandId = CommandIdFromPath(job->path);
        DeleteFileA(job->path.c_str());
        knownCommandIds.erase(commandId);
        LogDebug(
            "QUEUE_STALE_DROP command_id=" + commandId
            + " kind=map_entity_safe reason=materialization_lease_mismatch"
            + " effect=unconfirmed"
            + DeliveryContextFields()
        );
        job = queue.erase(job);
    }
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

bool SpoolRemoved(const std::string& path) {
    if (DeleteFileA(path.c_str())) {
        return true;
    }
    const DWORD error = GetLastError();
    return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND;
}

void RetryDeliveredSpoolRemovals(
    std::unordered_map<std::string, DeliveredSpool>& deliveredSpools,
    std::unordered_set<std::string>& knownCommandIds
) {
    const DWORD now = GetTickCount();
    for (auto entry = deliveredSpools.begin(); entry != deliveredSpools.end();) {
        DeliveredSpool& spool = entry->second;
        if (!ReceiptDispatchReady(now, spool.nextRemovalAttemptTick)) {
            ++entry;
            continue;
        }
        if (SpoolRemoved(spool.path)) {
            LogDebug(
                "ACK_REMOVE_RETRY command_id=" + entry->first
                + " path=" + std::filesystem::path(spool.path).filename().string()
            );
            knownCommandIds.erase(entry->first);
            entry = deliveredSpools.erase(entry);
            continue;
        }
        ++spool.removalAttempt;
        spool.nextRemovalAttemptTick = now + std::min(
            kDeliveredRemovalRetryMs << std::min(spool.removalAttempt - 1, 3U),
            8000UL
        );
        if (spool.removalAttempt == 1) {
            LogDebug(
                "ACK_REMOVE_RETRY_PENDING command_id=" + entry->first
                + " path=" + std::filesystem::path(spool.path).filename().string()
            );
        }
        ++entry;
    }
}

bool ExecuteCommand(const CommandJob& job) {
    const std::string commandId = CommandIdFromPath(job.path);
    const std::string& command = job.command;
    LogDebug("RPC_EXECUTE command_id=" + commandId + DeliveryContextFields());
    if (g_ApRpc) g_ApRpc->SetCurrentCommandId(commandId);

    if (job.diagnosticCondump) {
        if (command != "condump AP_SUPPORT_FILE.txt") {
            LogDebug("RPC_DIAGNOSTIC_CONDUMP_REJECT command_id=" + commandId + " reason=payload");
            return false;
        }
        LogDebug("RPC_DIAGNOSTIC_CONDUMP command_id=" + commandId + " file=AP_SUPPORT_FILE.txt");
    }

    if (command.rfind("#DUMP_ENTITIES", 0) == 0) {
        const size_t bufferSize = 128 * 1024 * 1024;
        unsigned char* buffer = static_cast<unsigned char*>(malloc(bufferSize));
        if (!buffer) {
            return false;
        }
        size_t actualSize = bufferSize;
        const bool success = g_ApRpc->RetrieveEntities(buffer, &actualSize);
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
        const bool success = g_ApRpc->RequestEntityLoad(path, true, 0);
        return success;
    }

    return g_ApRpc->ExecuteConsoleCommand(command);
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
    ApRpcHealthStatePublisher healthStatePublisher(runtimePaths.gameRootDir / "base");
    healthStatePublisher.PublishStarting();

    CommandSourceMap recoveredSources;
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
    const std::unordered_set<std::string> noActiveCommandIds;
    EnsureQueueDirectory(recoveredSources, heldReceiptLogs, std::nullopt, noActiveCommandIds);
    HANDLE queueChangeNotification = FindFirstChangeNotificationA(
        kQueueDirectory,
        FALSE,
        FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE
    );
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
        healthStatePublisher.PublishHealth(false, AP_RPC_UNKNOWN, ERROR_FILE_NOT_FOUND);
        LogDebug(
            "Meathook preflight failed. No valid XINPUT1_3.dll candidate was accepted. "
            "Queued commands will remain pending until the client is restarted "
            "with a valid install."
        );
    } else {
        g_ApRpcOwner = std::make_unique<ApRuntimeRpcClient>();
        g_ApRpc = g_ApRpcOwner.get();
        g_ApRpc->SetLogCallback(LogDebug);
        LogDebug(
            "Meathook RPC client binding initialized. Waiting for the in-game "
            "Meathook server..."
        );
        while (!g_ApRpc || !g_ApRpc->PollHealth()) {
            if (g_ApRpc) {
                healthStatePublisher.PublishHealth(
                    false, static_cast<int>(g_ApRpc->LastResult()), g_ApRpc->LastTransportStatus()
                );
            } else {
                healthStatePublisher.PublishHealth(false, AP_RPC_UNKNOWN, ERROR_FILE_NOT_FOUND);
            }
            gameStateProbe.Poll();
            missionTransitionMonitor.Poll(
                gameStateProbe.IsGameplayLoaded(),
                gameStateProbe.IsLoading(),
                gameStateProbe.IsSafeForRpc()
            );
            Sleep(100);
        }
        healthStatePublisher.PublishHealth(
            true, static_cast<int>(g_ApRpc->LastResult()), g_ApRpc->LastTransportStatus()
        );
        LogDebug("Meathook RPC server verified.");
    }

    std::deque<CommandJob> queue;
    std::unordered_set<std::string> knownCommandIds;
    std::unordered_map<std::string, DeliveredSpool> deliveredSpools;
    DWORD lastExecution = 0;
    DWORD lastQueueStateLog = 0;
    size_t acknowledgedCommands = 0;
    bool queueWasActive = false;
    bool lastRpcArmed = false;
    bool lastRpcEnabled = false;
    std::string lastGateReason;
    DWORD lastStallLog = 0;
    bool silentBurstActive = false;
    DWORD silentBurstStarted = 0;
    DWORD silentBurstRpcMs = 0;
    size_t silentBurstOperations = 0;
    auto finishSilentBurst = [&](const char* reason) {
        if (!silentBurstActive) return;
        const DWORD totalMs = GetTickCount() - silentBurstStarted;
        const DWORD schedulerMs = totalMs > silentBurstRpcMs
            ? totalMs - silentBurstRpcMs : 0;
        const DWORD averageRpcMs = silentBurstOperations
            ? silentBurstRpcMs / static_cast<DWORD>(silentBurstOperations) : 0;
        LogDebug(
            "SILENT_MAINTENANCE_BURST operations=" + std::to_string(silentBurstOperations)
            + " total_ms=" + std::to_string(totalMs)
            + " average_rpc_ms=" + std::to_string(averageRpcMs)
            + " scheduler_overhead_ms=" + std::to_string(schedulerMs)
            + " reason=" + reason
        );
        silentBurstActive = false;
        silentBurstStarted = 0;
        silentBurstRpcMs = 0;
        silentBurstOperations = 0;
    };
    AmmoHotkeyHandler ammoHotkey(LogDebug);

    while (true) {
        gameStateProbe.Poll();
        missionTransitionMonitor.Poll(
            gameStateProbe.IsGameplayLoaded(),
            gameStateProbe.IsLoading(),
            gameStateProbe.IsSafeForRpc()
        );
        RetryDeliveredSpoolRemovals(deliveredSpools, knownCommandIds);
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
        EnsureQueueDirectory(
            recoveredSources, heldReceiptLogs, activeNamespace, knownCommandIds
        );
        ImportSpoolFiles(
            queue,
            knownCommandIds,
            recoveredSources,
            heldReceiptLogs,
            activeNamespace
        );

        const DWORD now = GetTickCount();
        bool rpcArmed = IsRpcExecutionEnabled();
        const bool normalCommandPending = std::any_of(
            queue.begin(), queue.end(), [](const CommandJob& job) { return !job.diagnosticCondump; }
        );
        if (normalCommandPending && !rpcArmed) {
            if (ArmRpcExecution()) {
                rpcArmed = true;
                LogDebug("RPC command execution auto-armed because commands are pending.");
            }
        }
        const bool rpcTransportReady =
            meathookPreflightPassed && g_ApRpc && g_ApRpc->PollHealth();
        healthStatePublisher.PublishHealth(
            rpcTransportReady,
            g_ApRpc ? static_cast<int>(g_ApRpc->LastResult()) : AP_RPC_UNKNOWN,
            g_ApRpc ? g_ApRpc->LastTransportStatus() : ERROR_FILE_NOT_FOUND
        );
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
        DiscardMapEntitySafeJobsWithMismatchedLease(
            queue, knownCommandIds, ActiveMaterializationLease()
        );
        ammoHotkey.Poll(
            gameStateProbe.GetProcessId(),
            activeNamespace.has_value(),
            rpcTransportReady,
            rpcArmed,
            gameStateProbe.IsSafeForRpc(),
            [](const std::string& command) {
                if (!g_ApRpc) return false;
                return g_ApRpc->ExecuteConsoleCommand(command);
            },
            g_ApRpc ? static_cast<int>(g_ApRpc->LastResult()) : 0,
            g_ApRpc ? g_ApRpc->LastTransportStatus() : 0
        );
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

        const bool frontGateOpen = !queue.empty()
            && CommandExecutionGateOpen(queue.front(), rpcArmed, rpcTransportReady, gameStateProbe);
        if (silentBurstActive && (queue.empty() || !frontGateOpen)) {
            finishSilentBurst(queue.empty() ? "queue_drained" : "safety_gate_closed");
        }
        if (!queue.empty() && !frontGateOpen && now - lastStallLog >= kRpcStallWarnMs) {
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
        if (!queue.empty()
                && rpcTransportReady
                && g_ApRpc->Ready()) {
            size_t dispatchIndex = 0;
            if (!frontGateOpen) {
                for (size_t index = 1; index < queue.size(); ++index) {
                    if (queue[index].diagnosticCondump) {
                        dispatchIndex = index;
                        break;
                    }
                }
            }
            CommandJob& job = queue[dispatchIndex];
            if (!rpcArmed && !job.diagnosticCondump) {
                Sleep(50);
                continue;
            }
            const std::string commandId = CommandIdFromPath(job.path);
            const bool normalReceipt = IsFreshReceiptCommandId(commandId);
            const bool silentMaintenance = IsSilentMaintenanceJob(job);
            if (silentBurstActive && !silentMaintenance) {
                finishSilentBurst("next_job_not_silent");
            }
            const bool dispatchReady = normalReceipt || job.diagnosticCondump || silentMaintenance
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
                queue.erase(queue.begin() + dispatchIndex);
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
                    queue.erase(queue.begin() + dispatchIndex);
                    continue;
                }
            }
            if (!MapEntitySafeLeaseMatchesCurrent(job)) {
                LogDebug(
                    "QUEUE_STALE_DROP command_id=" + commandId
                    + " kind=map_entity_safe reason=materialization_lease_mismatch"
                    + " effect=unconfirmed"
                    + DeliveryContextFields()
                );
                DeleteFileA(job.path.c_str());
                knownCommandIds.erase(commandId);
                queue.erase(queue.begin() + dispatchIndex);
                continue;
            }
            if (!CommandExecutionGateOpen(job, rpcArmed, rpcTransportReady, gameStateProbe)) {
                Sleep(50);
                continue;
            }
            const bool diagnosticJob = job.diagnosticCondump;
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
                const DWORD rpcElapsedMs = GetTickCount() - dispatchTick;
                const bool spoolRemoved = SpoolRemoved(job.path);
                if (normalReceipt) {
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=" + ReceiptCommandKind(commandId)
                        + " result=command_consumed_unverified"
                        + " spool_removal=" + (spoolRemoved ? "complete" : "pending")
                        + " elapsed_ms="
                        + std::to_string(rpcElapsedMs)
                        + DeliveryContextFields()
                    );
                    if (spoolRemoved) {
                        LogDebug(
                            "ACK_REMOVE command_id=" + commandId
                            + " path=" + std::filesystem::path(job.path).filename().string()
                            + DeliveryContextFields()
                        );
                    }
                    dispatchNextImmediately = true;
                } else if (diagnosticJob) {
                    LogDebug(
                        "RPC_DIAGNOSTIC_CONDUMP_RESULT command_id=" + commandId
                        + " result=command_consumed_unverified"
                        + " spool_removal=" + (spoolRemoved ? "complete" : "pending")
                    );
                } else {
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=non_receipt result=command_consumed_unverified"
                        + " spool_removal=" + (spoolRemoved ? "complete" : "pending")
                        + DeliveryContextFields()
                    );
                }
                if (silentMaintenance) {
                    if (!silentBurstActive) {
                        silentBurstActive = true;
                        silentBurstStarted = dispatchTick;
                    }
                    ++silentBurstOperations;
                    silentBurstRpcMs += rpcElapsedMs;
                    dispatchNextImmediately = true;
                }
                ++acknowledgedCommands;
                if (!spoolRemoved) {
                    deliveredSpools[commandId] = {
                        job.path,
                        GetTickCount() + kDeliveredRemovalRetryMs,
                        0,
                    };
                } else {
                    knownCommandIds.erase(commandId);
                }
                queue.erase(queue.begin() + dispatchIndex);
            } else {
                if (silentMaintenance) {
                    finishSilentBurst("rpc_failure");
                }
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
                        + RpcCallResultName(g_ApRpc->LastResult())
                        + "/" + std::to_string(g_ApRpc->LastTransportStatus())
                        + DeliveryContextFields()
                    );
                } else if (diagnosticJob) {
                    ++job.retryAttempt;
                    if (job.retryAttempt >= kDiagnosticRetryLimit) {
                        QuarantineFailedJob(job);
                        LogDebug(
                            "RPC_DIAGNOSTIC_CONDUMP_QUARANTINED command_id=" + commandId
                            + " attempts=" + std::to_string(job.retryAttempt)
                        );
                        knownCommandIds.erase(commandId);
                        queue.erase(queue.begin() + dispatchIndex);
                    } else {
                        job.nextAttemptTick = GetTickCount() + ReceiptRetryDelayMs(job.retryAttempt);
                        LogDebug(
                            "RPC_DIAGNOSTIC_CONDUMP_RETRY command_id=" + commandId
                            + " attempt=" + std::to_string(job.retryAttempt)
                            + " reason=" + RpcCallResultName(g_ApRpc->LastResult())
                            + "/" + std::to_string(g_ApRpc->LastTransportStatus())
                        );
                    }
                } else {
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=non_receipt result=retry"
                        + " transport=" + RpcCallResultName(g_ApRpc->LastResult())
                        + " wait_error=" + std::to_string(g_ApRpc->LastTransportStatus())
                        + DeliveryContextFields()
                    );
                    DeleteFileA(kRpcGatePath);
                }
            }
            if (!diagnosticJob) {
                lastExecution = GetTickCount();
            }
        }

        if (dispatchNextImmediately) {
            continue;
        }

        if (queueChangeNotification == INVALID_HANDLE_VALUE) {
            Sleep(50);
            continue;
        }
        const DWORD queueWait = WaitForSingleObject(queueChangeNotification, 50);
        if (queueWait == WAIT_OBJECT_0
                && !FindNextChangeNotification(queueChangeNotification)) {
            CloseHandle(queueChangeNotification);
            queueChangeNotification = INVALID_HANDLE_VALUE;
        }
    }

    ReleaseMutex(singleInstance);
    CloseHandle(singleInstance);
}
