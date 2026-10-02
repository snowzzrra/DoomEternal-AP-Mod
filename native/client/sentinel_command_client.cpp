#include "sentinel_command_client.h"
#include "commands.h"
#include "sentinel_version.h"
#include <bcrypt.h>
#include <cstring>

void SentinelCommandClient::SetTargetProcess(DWORD pid) {
    if (pid_ != pid) { pid_ = pid; ready_ = false; identity_ = {}; }
}
bool SentinelCommandClient::PollHealth() {
    if (!pid_) { ready_ = false; result_ = AP_RPC_PIPE_MISSING; status_ = ERROR_FILE_NOT_FOUND; return false; }
    const auto observation = sentinel::query_native(pid_, 250);
    ready_ = observation.result == sentinel::ProbeResult::ok &&
        sentinel::commands::compatible_product(observation.snapshot.core.version) &&
        observation.native.availability == SC_NATIVE_ENABLED;
    status_ = observation.win32_error;
    if (ready_) {
        if (identity_.process_created != observation.snapshot.process_created ||
            identity_.instance != observation.snapshot.instance) ++epoch_;
        identity_ = observation.snapshot; result_ = AP_RPC_DELIVERED;
    } else result_ = observation.result == sentinel::ProbeResult::endpoint_absent ? AP_RPC_PIPE_MISSING : AP_RPC_UNKNOWN;
    return ready_;
}
bool SentinelCommandClient::ExecuteConsoleCommand(const std::string& command) {
    sc_command_request request{};
    if (!sentinel::commands::parse(command, request)) {
        result_ = AP_RPC_REJECTED; status_ = ERROR_INVALID_DATA; return false;
    }
    const auto native = sentinel::query_native(pid_, 500);
    if (native.result != sentinel::ProbeResult::ok ||
        !sentinel::commands::compatible_product(native.snapshot.core.version)) {
        result_ = AP_RPC_REJECTED; status_ = ERROR_NOT_READY; return false;
    }
    request.execution.expected = native.native.scope;
    if (sentinel::commands::process_scoped(request)) {
        std::memcpy(request.namespace_id, SC_COMMAND_PROCESS_NAMESPACE, sizeof(request.namespace_id));
    } else {
        const auto admission = sentinel::query_save_admission(pid_, 500);
        if (admission.result != sentinel::ProbeResult::ok || admission.admission.state != SC_SAVE_SESSION_ADMITTED ||
            !(admission.admission.flags & SC_SAVE_SESSION_ACCEPTING) ||
            native.snapshot.process_created != admission.snapshot.process_created ||
            native.snapshot.instance != admission.snapshot.instance) {
            result_ = AP_RPC_REJECTED; status_ = ERROR_NOT_READY; return false;
        }
        std::memcpy(request.namespace_id, admission.admission.namespace_id, sizeof(request.namespace_id));
    }
    request.execution.deadline_ms = 2000;
    if (BCryptGenRandom(nullptr, request.execution.nonce, sizeof(request.execution.nonce), BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
        result_ = AP_RPC_REJECTED; status_ = ERROR_GEN_FAILURE; return false;
    }
    std::memcpy(&request.execution.request_id, request.execution.nonce, sizeof(request.execution.request_id));
    if (!request.execution.request_id || !sentinel::commands::valid(request)) {
        result_ = AP_RPC_REJECTED; status_ = ERROR_INVALID_DATA; return false;
    }
    const auto deadline = GetTickCount64() + 4000;
    auto reply = sentinel::query_command(pid_, 500, sentinel::command_submit_operation, request);
    for (;;) {
        if (reply.result == sentinel::ProbeResult::ok) {
            const auto& execution = reply.command.execution;
            if (execution.state >= SC_DIAGNOSTIC_EXECUTED)
                sentinel::query_command(pid_, 500, sentinel::command_release_operation, request);
            if (execution.state == SC_DIAGNOSTIC_EXECUTED) {
                status_ = reply.command.native_exception;
                result_ = reply.command.outcome == SC_COMMAND_DISPATCHED ? AP_RPC_DELIVERED :
                    reply.command.outcome == SC_COMMAND_NATIVE_FAILED ? AP_RPC_AMBIGUOUS : AP_RPC_REJECTED;
                if (log_) log_("CORE_COMMAND command_id=" + command_id_ +
                    " request_id=" + std::to_string(request.execution.request_id) +
                    " outcome=" + std::to_string(reply.command.outcome) + " gameplay_effect=unverified");
                return result_ == AP_RPC_DELIVERED;
            }
            if (execution.state >= SC_DIAGNOSTIC_REJECTED) {
                result_ = AP_RPC_REJECTED; status_ = execution.reason; return false;
            }
        }
        if (GetTickCount64() >= deadline) break;
        Sleep(10);
        // Query the same identity after an uncertain submit; never resubmit it.
        reply = sentinel::query_command(pid_, 500, sentinel::command_result_operation, request);
    }
    result_ = AP_RPC_AMBIGUOUS; status_ = ERROR_TIMEOUT;
    if (log_) log_("CORE_COMMAND command_id=" + command_id_ + " outcome=ambiguous automatic_replay=disabled");
    return false;
}
