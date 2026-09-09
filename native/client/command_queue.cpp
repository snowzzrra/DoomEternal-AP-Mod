#include "command_queue.h"
#include "rpc_queue_policy.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <io.h>
#include <regex>
#include <sstream>
#include <utility>

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


std::string DeliveryContextFields() {
    return " active_map=unavailable slot=unavailable bridge_protocol_version=unavailable"
        " rpc_entity_contract_revision=" + std::to_string(kRpcEntityContractRevision)
        + " rpc_entity_prefix=" + kRpcEntityPrefix
        + " helper_sha=unavailable injector_sha=unavailable";
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


std::string NativeCommandQueue::TrimLine(std::string value) {
    while (!value.empty() && (value.back() == '\n' || value.back() == '\r' || value.back() == '\0')) {
        value.pop_back();
    }
    return value;
}


std::string NativeCommandQueue::CommandIdFromPath(const std::string& path) {
    return std::filesystem::path(path).stem().string();
}


const char* NativeCommandQueue::ReceiptCommandKind(const std::string& commandId) {
    return commandId.find("-notify") != std::string::npos ? "notification" : "effect";
}


void NativeCommandQueue::NoteQueueDedupe(const std::string& commandId) {
    const DWORD now = GetTickCount();
    if (!dedupeLogState.active) {
        dedupeLogState.active = true;
        dedupeLogState.nextSummaryTick = now + kQueueNoiseSummaryMs;
        dedupeLogState.suppressed = 0;
        LogDebug(
            "QUEUE_DUPLICATE_REJECT command_id=" + commandId
            + " reason=known_command_id RPC_QUEUE_DEDUPE transition=active"
            + DeliveryContextFields()
        );
        return;
    }
    ++dedupeLogState.suppressed;
    if (static_cast<LONG>(now - dedupeLogState.nextSummaryTick) >= 0) {
        LogDebug(
            "RPC_QUEUE_DEDUPE summary suppressed="
            + std::to_string(dedupeLogState.suppressed)
        );
        dedupeLogState.suppressed = 0;
        dedupeLogState.nextSummaryTick = now + kQueueNoiseSummaryMs;
    }
}


void NativeCommandQueue::RecoverQueueDedupeLog() {
    if (!dedupeLogState.active) {
        return;
    }
    LogDebug(
        "RPC_QUEUE_DEDUPE recovery suppressed="
        + std::to_string(dedupeLogState.suppressed)
    );
    dedupeLogState = {};
}


bool NativeCommandQueue::ReadCommandFile(
    const std::string& path,
    std::string& command,
    std::optional<std::string>* materializationLease,
    CommandExecutionClass* executionClass,
    MapEntityOperation* mapEntityOperation,
    bool* diagnosticCondump,
    std::optional<std::string>* transientScope
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
    if (transientScope) transientScope->reset();

    bool sawExecutionClass = false;
    bool sawMaterializationLease = false;
    bool sawMapEntityOperation = false;
    bool sawDiagnosticCondump = false;
    bool sawTransientScope = false;
    CommandExecutionClass parsedExecutionClass = CommandExecutionClass::PlayerRuntime;
    MapEntityOperation parsedMapEntityOperation = MapEntityOperation::None;
    size_t payloadOffset = 0;
    const std::string leasePrefix = std::string(kMaterializationLeaseHeader) + " ";
    const std::string executionPrefix = std::string(kExecutionClassHeader) + " ";
    const std::string operationPrefix = std::string(kMapEntityOperationHeader) + " ";
    const std::string diagnosticPrefix = std::string(kDiagnosticCondumpHeader) + " ";
    const std::string transientPrefix = std::string(kTransientScopeHeader) + " ";
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
            } else if (value == "TRANSIENT_EFFECT") {
                parsedExecutionClass = CommandExecutionClass::TransientEffect;
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
        } else if (line.rfind(std::string(kTransientScopeHeader), 0) == 0) {
            if (sawTransientScope || line.rfind(transientPrefix, 0) != 0) return false;
            const std::string scope = line.substr(transientPrefix.size());
            static const std::regex validScope(R"(^effectscope-[0-9a-f]{16}$)");
            if (!std::regex_match(scope, validScope)) return false;
            if (transientScope) *transientScope = scope;
            sawTransientScope = true;
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
    if (parsedExecutionClass == CommandExecutionClass::TransientEffect
            && !sawTransientScope) return false;
    if (parsedExecutionClass != CommandExecutionClass::TransientEffect
            && sawTransientScope) return false;
    if (executionClass) *executionClass = parsedExecutionClass;
    if (mapEntityOperation) *mapEntityOperation = parsedMapEntityOperation;
    if (diagnosticCondump) *diagnosticCondump = sawDiagnosticCondump;
    return !command.empty();
}


bool NativeCommandQueue::WriteCommandFile(const std::string& path, const std::string& command) {
    FILE* file = fopen(path.c_str(), "wb");
    if (!file) return false;
    const std::string line = command + "\n";
    const size_t written = fwrite(line.data(), 1, line.size(), file);
    const bool ok = written == line.size() && fflush(file) == 0;
    fclose(file);
    return ok;
}


bool NativeCommandQueue::StartsWith(const std::string& value, const std::string& prefix) {
    return value.rfind(prefix, 0) == 0;
}


std::optional<std::string> NativeCommandQueue::MigratedDirectItemCommand(
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


std::optional<std::string> NativeCommandQueue::ActiveQueueSessionNamespace() {
    std::ifstream input(kQueueSessionNamespacePath);
    std::string value;
    if (!std::getline(input, value)) return std::nullopt;
    static const std::regex valid(R"(^[0-9a-f]{16}$)");
    if (!std::regex_match(value, valid)) return std::nullopt;
    return value;
}


std::optional<std::string> NativeCommandQueue::ActiveMaterializationLease() {
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


std::optional<std::string> NativeCommandQueue::ActiveTransientScope() {
    std::ifstream input(kTransientScopePath);
    std::string value;
    if (!std::getline(input, value)) return std::nullopt;
    static const std::regex valid(R"(^effectscope-[0-9a-f]{16}$)");
    if (!std::regex_match(value, valid)) return std::nullopt;
    return value;
}


std::optional<std::string> NativeCommandQueue::ReceiptCommandNamespace(const std::string& filename) {
    static const std::regex namespaced(R"(^recv-([0-9a-f]{16})-.*\.(cmd|processing)$)");
    std::smatch match;
    if (std::regex_match(filename, match, namespaced)) return match[1].str();
    if (StartsWith(filename, "recv-")) return std::string(); // unscoped receipt
    return std::nullopt; // generic command
}


void NativeCommandQueue::EnsureQueueDirectory(
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


std::vector<std::string> NativeCommandQueue::FindQueuedFiles() {
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


bool NativeCommandQueue::MapEntityOperationMatchesCommand(
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


void NativeCommandQueue::ImportSpoolFiles(
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
        std::optional<std::string> transientScope;
        if (ReadCommandFile(
                processingPath,
                command,
                &materializationLease,
                &executionClass,
                &mapEntityOperation,
                &diagnosticCondump,
                &transientScope
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
                transientScope,
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


bool NativeCommandQueue::IsTelemetryJob(const CommandJob& job) {
    const size_t separator = job.path.find_last_of("\\/");
    const std::string filename =
        separator == std::string::npos ? job.path : job.path.substr(separator + 1);
    return filename.rfind("telemetry.", 0) == 0;
}


bool NativeCommandQueue::IsSilentMaintenanceJob(const CommandJob& job) {
    if (job.executionClass == CommandExecutionClass::PlayerRuntime) return false;
    const std::string commandId = CommandIdFromPath(job.path);
    return StartsWith(commandId, "reconcile-")
        || commandId.find("-reconcile-") != std::string::npos
        || StartsWith(commandId, "automap-cleanup-")
        || commandId.find("-automap-cleanup-") != std::string::npos
        || job.mapEntityOperation == MapEntityOperation::CheckedVisualHide;
}


bool NativeCommandQueue::IsFreshReceiptCommandId(const std::string& commandId) {
    static const std::regex receipt(
        R"(^recv-[0-9a-f]{16}-[0-9]+-item-[0-9]+-(?:cmd-[0-9]+(?:-notify)?|effect-[0-9]+|notify)$)"
    );
    return std::regex_match(commandId, receipt);
}


bool NativeCommandQueue::IsMapEntitySafeJob(const CommandJob& job) {
    return job.executionClass == CommandExecutionClass::MapEntitySafe;
}


bool NativeCommandQueue::IsTransientEffectJob(const CommandJob& job) {
    return job.executionClass == CommandExecutionClass::TransientEffect;
}


bool NativeCommandQueue::IsValidTransientEffectCommand(const std::string& command) {
    static const std::regex valid(
        R"(^g_(damageScaleAllToAI|damageScaleAllToSlayer) (0|[0-9]+\.[0-9]{2})$|^g_infiniteAmmo [01]$)"
    );
    return std::regex_match(command, valid);
}


bool NativeCommandQueue::MapEntitySafeLeaseMatchesCurrent(const CommandJob& job) {
    if (!IsMapEntitySafeJob(job)) return true;
    if (!MapEntityOperationMatchesCommand(job.mapEntityOperation, job.command)) return false;
    const std::optional<std::string> currentLease = ActiveMaterializationLease();
    return job.materializationLease.has_value()
        && currentLease.has_value()
        && job.materializationLease.value() == currentLease.value();
}


bool NativeCommandQueue::CommandExecutionGateOpen(
    const CommandJob& job,
    bool rpcArmed,
    bool rpcTransportReady,
    const QueueSafetySnapshot& safety,
    bool transientBaselineReady
) {
    if (job.diagnosticCondump) {
        // Support condump bypasses gameplay/RPC safety gates, but still needs
        // live native transport, which is only healthy while game is open.
        return rpcTransportReady;
    }
    if (IsTransientEffectJob(job)) {
        const std::optional<std::string> activeScope = ActiveTransientScope();
        return rpcTransportReady
            && transientBaselineReady
            && safety.playerSafe
            && job.transientScope.has_value()
            && activeScope.has_value()
            && job.transientScope.value() == activeScope.value()
            && IsValidTransientEffectCommand(job.command);
    }
    if (!rpcArmed || !rpcTransportReady) return false;
    if (IsMapEntitySafeJob(job)) {
        return safety.mapEntitySafe
            && MapEntitySafeLeaseMatchesCurrent(job);
    }
    return safety.playerSafe;
}


const char* NativeCommandQueue::CommandExecutionClassName(CommandExecutionClass executionClass) {
    switch (executionClass) {
    case CommandExecutionClass::MapEntitySafe:
        return "MAP_ENTITY_SAFE";
    case CommandExecutionClass::TransientEffect:
        return "TRANSIENT_EFFECT";
    case CommandExecutionClass::PlayerRuntime:
    default:
        return "PLAYER_RUNTIME";
    }
}


std::string NativeCommandQueue::CommandExecutionGateReason(
    const CommandJob& job,
    bool rpcArmed,
    bool rpcTransportReady,
    const QueueSafetySnapshot& safety,
    bool transientBaselineReady
) {
    if (job.diagnosticCondump) {
        return rpcTransportReady ? "ready" : "rpc_unavailable";
    }
    if (IsTransientEffectJob(job)) {
        if (!rpcTransportReady) return "rpc_unavailable";
        if (!transientBaselineReady) return "transient_baseline_unready";
        if (!safety.playerSafe) return "player_unavailable";
        if (!job.transientScope.has_value()) return "transient_scope_missing";
        const std::optional<std::string> activeScope = ActiveTransientScope();
        if (!activeScope.has_value()) return "transient_scope_unavailable";
        if (job.transientScope.value() != activeScope.value()) {
            return "transient_scope_mismatch";
        }
        if (!IsValidTransientEffectCommand(job.command)) {
            return "transient_command_invalid";
        }
        return "ready";
    }
    if (!rpcArmed) return "rpc_disarmed";
    if (!rpcTransportReady) return "rpc_unavailable";
    if (IsMapEntitySafeJob(job)) {
        if (!safety.mapEntitySafe) return "map_entity_unsafe";
        if (!MapEntityOperationMatchesCommand(job.mapEntityOperation, job.command)) {
            return "map_entity_operation_invalid";
        }
        const std::optional<std::string> currentLease = ActiveMaterializationLease();
        if (!job.materializationLease.has_value()) return "materialization_lease_missing";
        if (!currentLease.has_value()) return "materialization_lease_unavailable";
        if (job.materializationLease.value() != currentLease.value()) {
            return "materialization_lease_mismatch";
        }
        return "ready";
    }
    if (!safety.playerSafe) return "player_unavailable";
    return "ready";
}


void NativeCommandQueue::DiscardMapEntitySafeJobsWithMismatchedLease(
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


void NativeCommandQueue::DiscardTransientEffectJobsWithMismatchedScope(
    std::deque<CommandJob>& queue,
    std::unordered_set<std::string>& knownCommandIds,
    const std::optional<std::string>& currentScope
) {
    auto job = queue.begin();
    while (job != queue.end()) {
        if (!IsTransientEffectJob(*job)
                || (job->transientScope.has_value()
                    && currentScope.has_value()
                    && job->transientScope.value() == currentScope.value())) {
            ++job;
            continue;
        }
        const std::string commandId = CommandIdFromPath(job->path);
        const std::string reason = !currentScope.has_value()
            ? "transient_scope_unavailable"
            : (!job->transientScope.has_value()
                ? "transient_scope_missing" : "transient_scope_mismatch");
        DeleteFileA(job->path.c_str());
        knownCommandIds.erase(commandId);
        LogDebug(
            "QUEUE_STALE_DROP command_id=" + commandId
            + " kind=transient_effect reason=" + reason
            + " effect=unconfirmed"
            + DeliveryContextFields()
        );
        job = queue.erase(job);
    }
}


void NativeCommandQueue::DiscardTelemetryJobs(std::deque<CommandJob>& queue) {
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


bool NativeCommandQueue::IsRpcExecutionEnabled() {
    return GetFileAttributesA(kRpcGatePath) != INVALID_FILE_ATTRIBUTES;
}


bool NativeCommandQueue::ArmRpcExecution() {
    FILE* file = fopen(kRpcGatePath, "w");
    if (!file) {
        return false;
    }
    fputs("enabled\n", file);
    fflush(file);
    fclose(file);
    return true;
}


void NativeCommandQueue::QuarantineFailedJob(const CommandJob& job) {
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


bool NativeCommandQueue::SpoolRemoved(const std::string& path) {
    if (DeleteFileA(path.c_str())) {
        return true;
    }
    const DWORD error = GetLastError();
    return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND;
}


void NativeCommandQueue::RetryDeliveredSpoolRemovals(
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


bool NativeCommandQueue::ExecuteCommand(const CommandJob& job, CommandTransport* rpc) {
    const std::string commandId = CommandIdFromPath(job.path);
    const std::string& command = job.command;
    LogDebug("RPC_EXECUTE command_id=" + commandId + DeliveryContextFields());
    if (rpc) rpc->SetCurrentCommandId(commandId);

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
        const bool success = rpc->RetrieveEntities(buffer, &actualSize);
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
        const bool success = rpc->RequestEntityLoad(path, true, 0);
        return success;
    }

    return rpc->ExecuteConsoleCommand(command);
}


NativeCommandQueue::~NativeCommandQueue() {
    if (queueChangeNotification != INVALID_HANDLE_VALUE) CloseHandle(queueChangeNotification);
    // Persisted .processing ownership survives shutdown; execution is not crash-safe exactly once.
}

void NativeCommandQueue::Initialize() {
    startupNamespaceCleared = DeleteFileA(kQueueSessionNamespacePath) != FALSE;
    if (!startupNamespaceCleared) {
        const DWORD error = GetLastError();
        startupNamespaceCleared = error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND;
        if (!startupNamespaceCleared) {
            LogDebug(
                "QUEUE_SESSION_MARKER_CLEAR_RETRY error=" + std::to_string(error)
            );
        }
    }
    const std::unordered_set<std::string> noActiveCommandIds;
    EnsureQueueDirectory(recoveredSources, heldReceiptLogs, std::nullopt, noActiveCommandIds);
    queueChangeNotification = FindFirstChangeNotificationA(
        kQueueDirectory,
        FALSE,
        FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE
    );
    DeleteFileA(kRpcGatePath);
}

std::optional<std::string> NativeCommandQueue::Import() {
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

    return activeNamespace;
}

bool NativeCommandQueue::ArmIfPending() {
        bool rpcArmed = IsRpcExecutionEnabled();
        const bool normalCommandPending = std::any_of(
            queue.begin(), queue.end(), [this](const CommandJob& job) {
                return !job.diagnosticCondump && !IsTransientEffectJob(job);
            }
        );
        if (normalCommandPending && !rpcArmed) {
            if (ArmRpcExecution()) {
                rpcArmed = true;
                LogDebug("RPC command execution auto-armed because commands are pending.");
            }
        }
    return rpcArmed;
}

void NativeCommandQueue::DiscardInvalidScopes(bool rpcEnabled) {
        if (!rpcEnabled) {
            DiscardTelemetryJobs(queue);
        }
        DiscardMapEntitySafeJobsWithMismatchedLease(
            queue, knownCommandIds, ActiveMaterializationLease()
        );
        DiscardTransientEffectJobsWithMismatchedScope(
            queue, knownCommandIds, ActiveTransientScope()
        );
}

void NativeCommandQueue::finishSilentBurst(const char* reason) {
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
}

bool NativeCommandQueue::Dispatch(DWORD now, bool rpcArmed, bool rpcTransportReady,
        const QueueSafetySnapshot& safety, bool transientBaselineReady,
        const std::string& gateReason, CommandTransport* rpc) {
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

        std::optional<size_t> dispatchIndex;
        for (size_t index = 0; index < queue.size(); ++index) {
            if (CommandExecutionGateOpen(
                    queue[index], rpcArmed, rpcTransportReady, safety,
                    transientBaselineReady)) {
                dispatchIndex = index;
                break;
            }
        }
        if (silentBurstActive && !dispatchIndex.has_value()) {
            finishSilentBurst(queue.empty() ? "queue_drained" : "safety_gate_closed");
        }
        const bool globallyReadyWithoutSelection =
            gateReason == "ready" && !queue.empty() && !dispatchIndex.has_value();
        if (globallyReadyWithoutSelection) {
            const CommandJob& front = queue.front();
            if (!noSelectionDiagnosticActive
                    || static_cast<LONG>(now - nextNoSelectionDiagnosticTick) >= 0) {
                LogDebug(
                    "QUEUE_NO_SELECTION global_gate=ready in_flight_id=none"
                    " pending_count=" + std::to_string(queue.size())
                    + " front_command_id=" + CommandIdFromPath(front.path)
                    + " front_class=" + CommandExecutionClassName(front.executionClass)
                    + " front_gate_reason=" + CommandExecutionGateReason(
                        front, rpcArmed, rpcTransportReady, safety,
                        transientBaselineReady
                    )
                    + " suppressed=" + std::to_string(suppressedNoSelectionDiagnostics)
                    + DeliveryContextFields()
                );
                noSelectionDiagnosticActive = true;
                nextNoSelectionDiagnosticTick = now + kRpcStallWarnMs;
                suppressedNoSelectionDiagnostics = 0;
            } else {
                ++suppressedNoSelectionDiagnostics;
            }
        } else if (noSelectionDiagnosticActive) {
            if (suppressedNoSelectionDiagnostics != 0) {
                LogDebug(
                    "QUEUE_NO_SELECTION_RECOVERY suppressed="
                    + std::to_string(suppressedNoSelectionDiagnostics)
                );
            }
            noSelectionDiagnosticActive = false;
            suppressedNoSelectionDiagnostics = 0;
        }

        bool dispatchNextImmediately = false;
        if (dispatchIndex.has_value()
                && rpcTransportReady
                && rpc->Ready()) {
            const size_t selectedIndex = dispatchIndex.value();
            CommandJob& job = queue[selectedIndex];
            if (!rpcArmed && !job.diagnosticCondump && !IsTransientEffectJob(job)) {
                Sleep(50);
                return true;
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
                return true;
            }
            if (GetFileAttributesA(job.path.c_str()) == INVALID_FILE_ATTRIBUTES) {
                LogDebug(
                    "QUEUE_CANCELLED command_id=" + commandId + DeliveryContextFields()
                );
                knownCommandIds.erase(commandId);
                queue.erase(queue.begin() + selectedIndex);
                return true;
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
                    queue.erase(queue.begin() + selectedIndex);
                    return true;
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
                queue.erase(queue.begin() + selectedIndex);
                return true;
            }
            if (!CommandExecutionGateOpen(
                    job, rpcArmed, rpcTransportReady, safety, transientBaselineReady)) {
                Sleep(50);
                return true;
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
            if (ExecuteCommand(job, rpc)) {
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
                queue.erase(queue.begin() + selectedIndex);
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
                        + RpcCallResultName(rpc->LastResult())
                        + "/" + std::to_string(rpc->LastTransportStatus())
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
                        queue.erase(queue.begin() + selectedIndex);
                    } else {
                        job.nextAttemptTick = GetTickCount() + ReceiptRetryDelayMs(job.retryAttempt);
                        LogDebug(
                            "RPC_DIAGNOSTIC_CONDUMP_RETRY command_id=" + commandId
                            + " attempt=" + std::to_string(job.retryAttempt)
                            + " reason=" + RpcCallResultName(rpc->LastResult())
                            + "/" + std::to_string(rpc->LastTransportStatus())
                        );
                    }
                } else {
                    LogDebug(
                        "RPC_RESULT command_id=" + commandId
                        + " kind=non_receipt result=retry"
                        + " transport=" + RpcCallResultName(rpc->LastResult())
                        + " wait_error=" + std::to_string(rpc->LastTransportStatus())
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
            return true;
        }

    return false;
}

void NativeCommandQueue::Wait() {
        if (queueChangeNotification == INVALID_HANDLE_VALUE) {
            Sleep(50);
            return;
        }
        const DWORD queueWait = WaitForSingleObject(queueChangeNotification, 50);
        if (queueWait == WAIT_OBJECT_0
                && !FindNextChangeNotification(queueChangeNotification)) {
            CloseHandle(queueChangeNotification);
            queueChangeNotification = INVALID_HANDLE_VALUE;
        }
}
