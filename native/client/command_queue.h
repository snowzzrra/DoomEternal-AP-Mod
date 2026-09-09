#pragma once
#include "command_queue_contracts.h"
#include "command_transport.h"
#include <deque>
#include <functional>
#include <unordered_set>
#include <vector>
#include <utility>

const char* RpcCallResultName(ApRpcResult result);
std::string DeliveryContextFields();
QueueSnapshot CountQueueFiles();

class NativeCommandQueue {
public:
    using LogCallback = std::function<void(const std::string&)>;
    explicit NativeCommandQueue(LogCallback logger) : logger_(std::move(logger)) {}
    ~NativeCommandQueue();
    NativeCommandQueue(const NativeCommandQueue&) = delete;
    NativeCommandQueue& operator=(const NativeCommandQueue&) = delete;
    void Initialize();
    std::optional<std::string> Import();
    bool ArmIfPending();
    void DiscardInvalidScopes(bool rpcEnabled);
    bool Dispatch(DWORD now, bool rpcArmed, bool rpcTransportReady,
                  const QueueSafetySnapshot& safety, bool transientBaselineReady,
                  const std::string& gateReason, CommandTransport* rpc);
    void Wait();
private:
    void LogDebug(const std::string& message) const { logger_(message); }
    void finishSilentBurst(const char* reason);
    LogCallback logger_;
    CommandSourceMap recoveredSources;
    std::unordered_set<std::string> heldReceiptLogs;
    bool startupNamespaceCleared = false;
    HANDLE queueChangeNotification = INVALID_HANDLE_VALUE;
    QueueDedupeLogState dedupeLogState;
    std::deque<CommandJob> queue;
    std::unordered_set<std::string> knownCommandIds;
    std::unordered_map<std::string, DeliveredSpool> deliveredSpools;
    DWORD lastExecution = 0;
    DWORD lastQueueStateLog = 0;
    size_t acknowledgedCommands = 0;
    bool queueWasActive = false;
    bool noSelectionDiagnosticActive = false;
    DWORD nextNoSelectionDiagnosticTick = 0;
    size_t suppressedNoSelectionDiagnostics = 0;
    bool silentBurstActive = false;
    DWORD silentBurstStarted = 0;
    DWORD silentBurstRpcMs = 0;
    size_t silentBurstOperations = 0;

    std::string TrimLine(std::string value);
    std::string CommandIdFromPath(const std::string& path);
    const char* ReceiptCommandKind(const std::string& commandId);
    void NoteQueueDedupe(const std::string& commandId);
    void RecoverQueueDedupeLog();
    bool ReadCommandFile(
    const std::string& path,
    std::string& command,
    std::optional<std::string>* materializationLease = nullptr,
    CommandExecutionClass* executionClass = nullptr,
    MapEntityOperation* mapEntityOperation = nullptr,
    bool* diagnosticCondump = nullptr,
    std::optional<std::string>* transientScope = nullptr
);
    bool WriteCommandFile(const std::string& path, const std::string& command);
    bool StartsWith(const std::string& value, const std::string& prefix);
    std::optional<std::string> MigratedDirectItemCommand(
    const std::string& filename,
    const std::string& command
);
    std::optional<std::string> ActiveQueueSessionNamespace();
    std::optional<std::string> ActiveMaterializationLease();
    std::optional<std::string> ActiveTransientScope();
    std::optional<std::string> ReceiptCommandNamespace(const std::string& filename);
    void EnsureQueueDirectory(
    CommandSourceMap& recoveredSources,
    std::unordered_set<std::string>& heldReceiptLogs,
    const std::optional<std::string>& activeNamespace,
    const std::unordered_set<std::string>& activeCommandIds
);
    std::vector<std::string> FindQueuedFiles();
    bool MapEntityOperationMatchesCommand(
    MapEntityOperation operation,
    const std::string& command
);
    void ImportSpoolFiles(
    std::deque<CommandJob>& queue,
    std::unordered_set<std::string>& knownCommandIds,
    CommandSourceMap& recoveredSources,
    std::unordered_set<std::string>& heldReceiptLogs,
    const std::optional<std::string>& activeNamespace
);
    bool IsTelemetryJob(const CommandJob& job);
    bool IsSilentMaintenanceJob(const CommandJob& job);
    bool IsFreshReceiptCommandId(const std::string& commandId);
    bool IsMapEntitySafeJob(const CommandJob& job);
    bool IsTransientEffectJob(const CommandJob& job);
    bool IsValidTransientEffectCommand(const std::string& command);
    bool MapEntitySafeLeaseMatchesCurrent(const CommandJob& job);
    bool CommandExecutionGateOpen(
    const CommandJob& job,
    bool rpcArmed,
    bool rpcTransportReady,
    const QueueSafetySnapshot& safety,
    bool transientBaselineReady
);
    const char* CommandExecutionClassName(CommandExecutionClass executionClass);
    std::string CommandExecutionGateReason(
    const CommandJob& job,
    bool rpcArmed,
    bool rpcTransportReady,
    const QueueSafetySnapshot& safety,
    bool transientBaselineReady
);
    void DiscardMapEntitySafeJobsWithMismatchedLease(
    std::deque<CommandJob>& queue,
    std::unordered_set<std::string>& knownCommandIds,
    const std::optional<std::string>& currentLease
);
    void DiscardTransientEffectJobsWithMismatchedScope(
    std::deque<CommandJob>& queue,
    std::unordered_set<std::string>& knownCommandIds,
    const std::optional<std::string>& currentScope
);
    void DiscardTelemetryJobs(std::deque<CommandJob>& queue);
    bool IsRpcExecutionEnabled();
    bool ArmRpcExecution();
    void QuarantineFailedJob(const CommandJob& job);
    bool SpoolRemoved(const std::string& path);
    void RetryDeliveredSpoolRemovals(
    std::unordered_map<std::string, DeliveredSpool>& deliveredSpools,
    std::unordered_set<std::string>& knownCommandIds
);
    bool ExecuteCommand(const CommandJob& job, CommandTransport* rpc);
};
