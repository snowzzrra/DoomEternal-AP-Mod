#pragma once
#include <windows.h>
#include <optional>
#include <string>
#include <unordered_map>

inline constexpr const char* kQueueDirectory = "base\\ap_queue";
inline constexpr const char* kQueueSessionNamespacePath = "base\\ap_queue\\active_session_namespace";
inline constexpr const char* kMaterializationLeasePath = "base\\ap_queue\\active_materialization_lease";
inline constexpr const char* kMaterializationLeaseHeader = "AP_MATERIALIZATION_LEASE_V1";
inline constexpr const char* kExecutionClassHeader = "AP_EXECUTION_CLASS_V1";
inline constexpr const char* kDiagnosticCondumpHeader = "AP_DIAGNOSTIC_CONDUMP_V1";
inline constexpr const char* kTransientScopeHeader = "AP_TRANSIENT_SCOPE_V1";
inline constexpr const char* kTransientScopePath = "base\\ap_queue\\active_transient_scope";
inline constexpr const char* kMapEntityOperationHeader = "AP_MAP_ENTITY_OPERATION_V1";
inline constexpr const char* kRpcGatePath = "base\\ap_rpc_enabled";
inline constexpr DWORD kCommandSpacingMs = 250;
inline constexpr DWORD kQueueStateLogMs = 5000;
inline constexpr DWORD kRpcStallWarnMs = 15000;
inline constexpr DWORD kQueueNoiseSummaryMs = 12000;
inline constexpr DWORD kDeliveredRemovalRetryMs = 1000;
inline constexpr unsigned int kDiagnosticRetryLimit = 3;
inline constexpr const char* kRpcEntityPrefix = "ap_rpc_v3";
inline constexpr int kRpcEntityContractRevision = 3;

enum class CommandExecutionClass {
    PlayerRuntime,
    MapEntitySafe,
    TransientEffect,
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
    std::optional<std::string> transientScope;
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




struct QueueSnapshot {
    size_t pending = 0;
    size_t processing = 0;
    size_t failed = 0;
};


struct QueueSafetySnapshot {
    bool playerSafe = false;
    bool mapEntitySafe = false;
};
