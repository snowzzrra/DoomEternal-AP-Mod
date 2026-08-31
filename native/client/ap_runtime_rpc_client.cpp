#include "ap_runtime_rpc_client.h"
#include "ap_runtime_rpc_seh.h"
#include <climits>
#include <cstring>
#include <cstdlib>
#include <sstream>
#include <utility>

namespace {
const char* kProtocol = "ncacn_np";
const char* kEndpoint = "\\pipe\\meathook_interface_rpc";
const char* kPipe = "\\\\.\\pipe\\meathook_interface_rpc";
const DWORD kHealthInterval = 1000;
const DWORD kHealthSummaryInterval = 12000;
const size_t kMaxCommand = 64 * 1024;
const size_t kMaxPath = 32 * 1024;
CRITICAL_SECTION g_rpc_binding_lock;
volatile LONG g_rpc_binding_lock_state = 0;

void EnsureRpcBindingLock()
{
    if (InterlockedCompareExchange(&g_rpc_binding_lock_state, 1, 0) == 0) {
        InitializeCriticalSection(&g_rpc_binding_lock);
        InterlockedExchange(&g_rpc_binding_lock_state, 2);
        return;
    }
    while (InterlockedCompareExchange(&g_rpc_binding_lock_state, 2, 2) != 2) {
        Sleep(0);
    }
}

class RpcBindingLock {
public:
    RpcBindingLock()
    {
        EnsureRpcBindingLock();
        EnterCriticalSection(&g_rpc_binding_lock);
    }

    ~RpcBindingLock()
    {
        LeaveCriticalSection(&g_rpc_binding_lock);
    }
};
}

ApRuntimeRpcClient::ApRuntimeRpcClient() = default;

ApRuntimeRpcClient::~ApRuntimeRpcClient()
{
    RpcBindingLock lock;
    DropBinding();
}

void ApRuntimeRpcClient::SetLogCallback(LogCallback callback)
{
    RpcBindingLock lock;
    log_callback_ = std::move(callback);
}

void ApRuntimeRpcClient::DropBinding()
{
    ready_ = false;
    ApRpcClearImplicitBinding();
    if (binding_ != nullptr) {
        RpcBindingFree(&binding_);
        binding_ = nullptr;
    }
    if (string_binding_ != nullptr) {
        RpcStringFreeA(&string_binding_);
        string_binding_ = nullptr;
    }
}

bool ApRuntimeRpcClient::Initialize()
{
    RpcBindingLock lock;
    return InitializeUnlocked();
}

bool ApRuntimeRpcClient::InitializeUnlocked()
{
    if (binding_ != nullptr) return true;
    RPC_STATUS status = RpcStringBindingComposeA(
        nullptr, (RPC_CSTR)kProtocol, nullptr, (RPC_CSTR)kEndpoint,
        nullptr, &string_binding_);
    if (status != RPC_S_OK) { status_ = status; return false; }
    status = RpcBindingFromStringBindingA(string_binding_, &binding_);
    if (status != RPC_S_OK) { status_ = status; DropBinding(); return false; }
    status = RpcStringFreeA(&string_binding_);
    if (status != RPC_S_OK) { status_ = status; }
    ++attachment_epoch_;
    return true;
}

bool ApRuntimeRpcClient::Ready() const
{
    RpcBindingLock lock;
    return ready_;
}

unsigned long long ApRuntimeRpcClient::AttachmentEpoch() const
{
    RpcBindingLock lock;
    return attachment_epoch_;
}

void ApRuntimeRpcClient::SetCurrentCommandId(const std::string& id)
{
    RpcBindingLock lock;
    command_id_ = id;
}

std::string ApRuntimeRpcClient::CurrentCommandId() const
{
    RpcBindingLock lock;
    return command_id_;
}

ApRpcResult ApRuntimeRpcClient::LastResult() const
{
    RpcBindingLock lock;
    return result_;
}

DWORD ApRuntimeRpcClient::LastTransportStatus() const
{
    RpcBindingLock lock;
    return status_;
}

bool ApRuntimeRpcClient::SetCallTimeout(ULONG milliseconds)
{
    const RPC_STATUS status = RpcBindingSetOption(binding_, RPC_C_OPT_CALL_TIMEOUT, milliseconds);
    if (status != RPC_S_OK) {
        status_ = status;
        DropBinding();
        return false;
    }
    return true;
}

ApRpcResult ApRuntimeRpcClient::ClassifyWaitFailure(DWORD status)
{
    if (status == ERROR_FILE_NOT_FOUND) return AP_RPC_PIPE_MISSING;
    if (status == ERROR_PIPE_BUSY) return AP_RPC_PIPE_BUSY;
    if (status == ERROR_SEM_TIMEOUT) return AP_RPC_WAIT_TIMEOUT;
    return AP_RPC_UNKNOWN;
}

bool ApRuntimeRpcClient::TickReached(DWORD now, DWORD deadline)
{
    return static_cast<LONG>(now - deadline) >= 0;
}

bool ApRuntimeRpcClient::RpcTraceEnabled()
{
    const char* trace = std::getenv("DOOM_AP_RPC_TRACE");
    return trace != nullptr && trace[0] != '\0' && trace[0] != '0';
}

bool ApRuntimeRpcClient::Prepare(const char* operation, DWORD* start_tick,
    unsigned long long* call_id)
{
    *start_tick = GetTickCount();
    *call_id = ++call_sequence_;
    if (log_callback_ && (strcmp(operation, "health") != 0 || RpcTraceEnabled())) {
        std::ostringstream message;
        message << "RPC_CALL_START operation=" << operation
                << " call_sequence=" << *call_id
                << " command_id=" << command_id_;
        log_callback_(message.str());
    }
    if (!InitializeUnlocked()) {
        Record(operation, *start_tick, *call_id, AP_RPC_UNKNOWN, status_);
        return false;
    }
    ULONG timeout = 15000;
    if (strcmp(operation, "health") == 0) timeout = 3000;
    if (strcmp(operation, "retrieve_entities") == 0) timeout = 60000;
    if (!SetCallTimeout(timeout)) {
        Record(operation, *start_tick, *call_id, AP_RPC_UNKNOWN, status_);
        return false;
    }
    if (!WaitNamedPipeA(kPipe, 100)) {
        status_ = GetLastError();
        ready_ = false;
        result_ = ClassifyWaitFailure(status_);
        Record(operation, *start_tick, *call_id, result_, status_);
        return false;
    }
    return true;
}

void ApRuntimeRpcClient::Record(
    const char* operation, DWORD start_tick, unsigned long long call_id,
    ApRpcResult result, DWORD status)
{
    result_ = result;
    status_ = status;
    std::ostringstream message;
    message << "RPC_CALL_END operation=" << operation
            << " call_sequence=" << call_id
            << " command_id=" << command_id_
            << " elapsed_ms=" << GetTickCount() - start_tick
            << " result=" << static_cast<int>(result)
            << " status=" << status;
    const bool health_operation = strcmp(operation, "health") == 0;
    const bool health_success = health_operation && result == AP_RPC_DELIVERED;
    if (health_operation && !RpcTraceEnabled()) {
        const DWORD now = GetTickCount();
        const bool transition = !health_log_initialized_ || health_available_ != health_success;
        if (transition) {
            const bool recovery = health_log_initialized_ && health_success;
            std::ostringstream health_message;
            health_message << "RPC_HEALTH_"
                << (recovery ? "RECOVERY" : "TRANSITION")
                << " state=" << (health_success ? "available" : "unavailable")
                << " result=" << static_cast<int>(result)
                << " status=" << status;
            if (health_suppressed_failures_ != 0) {
                health_message << " suppressed_failures=" << health_suppressed_failures_;
            }
            if (log_callback_) {
                log_callback_(health_message.str());
            }
            health_log_initialized_ = true;
            health_available_ = health_success;
            health_suppressed_failures_ = 0;
            next_health_summary_tick_ = now + kHealthSummaryInterval;
        } else if (!health_success) {
            ++health_suppressed_failures_;
            if (TickReached(now, next_health_summary_tick_)) {
                if (log_callback_) {
                    log_callback_(
                        "RPC_HEALTH_SUMMARY state=unavailable suppressed_failures="
                        + std::to_string(health_suppressed_failures_)
                        + " last_result=" + std::to_string(static_cast<int>(result))
                        + " last_status=" + std::to_string(status)
                    );
                }
                health_suppressed_failures_ = 0;
                next_health_summary_tick_ = now + kHealthSummaryInterval;
            }
        }
        return;
    }
    if (log_callback_ && (!health_success || RpcTraceEnabled())) {
        log_callback_(message.str());
    }
}

bool ApRuntimeRpcClient::PollHealth()
{
    RpcBindingLock lock;
    const std::string saved_command_id = command_id_;
    command_id_ = "-";
    const DWORD now = GetTickCount();
    if (!TickReached(now, next_health_tick_)) {
        command_id_ = saved_command_id;
        return ready_;
    }
    next_health_tick_ = now + kHealthInterval;
    DWORD start = 0; unsigned long long call = 0;
    if (!Prepare("health", &start, &call)) { ready_ = false; command_id_ = saved_command_id; return false; }
    int state = 0;
    const RPC_STATUS status = ApRpcHealth(binding_, &state);
    Record("health", start, call, status == RPC_S_OK ? AP_RPC_DELIVERED : AP_RPC_EXCEPTION, status);
    if (status != RPC_S_OK) { DropBinding(); command_id_ = saved_command_id; return ready_ = false; }
    command_id_ = saved_command_id;
    return ready_ = true;
}

bool ApRuntimeRpcClient::ExecuteConsoleCommand(const std::string& command)
{
    RpcBindingLock lock;
    if (command.empty() || command.size() > kMaxCommand || command.find('\0') != std::string::npos) {
        Record("execute", GetTickCount(), ++call_sequence_, AP_RPC_UNKNOWN, ERROR_INVALID_PARAMETER);
        return false;
    }
    DWORD start = 0; unsigned long long call = 0;
    if (!Prepare("execute", &start, &call)) return false;
    const RPC_STATUS status = ApRpcExecute(binding_, (unsigned char*)command.c_str());
    Record("execute", start, call, status == RPC_S_OK ? AP_RPC_DELIVERED : AP_RPC_EXCEPTION, status);
    if (status != RPC_S_OK) DropBinding();
    return status == RPC_S_OK;
}

bool ApRuntimeRpcClient::RequestEntityLoad(const std::string& path, bool begin, int size)
{
    RpcBindingLock lock;
    if (path.empty() || path.size() > kMaxPath || size < 0 || path.find('\0') != std::string::npos) {
        Record("request_entities", GetTickCount(), ++call_sequence_, AP_RPC_UNKNOWN, ERROR_INVALID_PARAMETER);
        return false;
    }
    DWORD start = 0; unsigned long long call = 0;
    if (!Prepare("request_entities", &start, &call)) return false;
    const RPC_STATUS status = ApRpcRequestEntities(binding_, (unsigned char*)path.c_str(), begin, size);
    Record("request_entities", start, call, status == RPC_S_OK ? AP_RPC_DELIVERED : AP_RPC_EXCEPTION, status);
    if (status != RPC_S_OK) DropBinding();
    return status == RPC_S_OK;
}

bool ApRuntimeRpcClient::RetrieveEntities(unsigned char* data, size_t* capacity)
{
    RpcBindingLock lock;
    if (!data || !capacity || *capacity > 128u * 1024u * 1024u || *capacity > INT_MAX) {
        Record("retrieve_entities", GetTickCount(), ++call_sequence_, AP_RPC_UNKNOWN, ERROR_INVALID_PARAMETER);
        return false;
    }
    int size = static_cast<int>(*capacity); DWORD start = 0; unsigned long long call = 0;
    if (!Prepare("retrieve_entities", &start, &call)) return false;
    const RPC_STATUS status = ApRpcRetrieveEntities(binding_, &size, data);
    const bool valid = status == RPC_S_OK && size >= 0 && static_cast<size_t>(size) <= *capacity;
    const DWORD diagnostic_status = valid ? RPC_S_OK : (status == RPC_S_OK ? ERROR_INVALID_DATA : status);
    Record("retrieve_entities", start, call, valid ? AP_RPC_DELIVERED : AP_RPC_EXCEPTION, diagnostic_status);
    if (!valid) { DropBinding(); return false; }
    *capacity = static_cast<size_t>(size);
    return true;
}

bool ApRuntimeRpcClient::Checkpoint(int* size, unsigned char* data, int capacity)
{
    RpcBindingLock lock;
    if (!size || !data || capacity < 0 || *size < 0 || *size > capacity) {
        Record("checkpoint", GetTickCount(), ++call_sequence_, AP_RPC_UNKNOWN, ERROR_INVALID_PARAMETER);
        return false;
    }
    DWORD start = 0; unsigned long long call = 0;
    if (!Prepare("checkpoint", &start, &call)) return false;
    const RPC_STATUS status = ApRpcCheckpoint(binding_, size, data);
    const bool valid = status == RPC_S_OK && *size >= 0 && *size <= capacity;
    Record("checkpoint", start, call, valid ? AP_RPC_DELIVERED : AP_RPC_EXCEPTION,
        valid ? RPC_S_OK : (status == RPC_S_OK ? ERROR_INVALID_DATA : status));
    if (!valid) DropBinding();
    return valid;
}

bool ApRuntimeRpcClient::Spawn(int* size, unsigned char* data, int capacity)
{
    RpcBindingLock lock;
    if (!size || !data || capacity < 0 || *size < 0 || *size > capacity) {
        Record("spawn", GetTickCount(), ++call_sequence_, AP_RPC_UNKNOWN, ERROR_INVALID_PARAMETER);
        return false;
    }
    DWORD start = 0; unsigned long long call = 0;
    if (!Prepare("spawn", &start, &call)) return false;
    const RPC_STATUS status = ApRpcSpawn(binding_, size, data);
    const bool valid = status == RPC_S_OK && *size >= 0 && *size <= capacity;
    Record("spawn", start, call, valid ? AP_RPC_DELIVERED : AP_RPC_EXCEPTION,
        valid ? RPC_S_OK : (status == RPC_S_OK ? ERROR_INVALID_DATA : status));
    if (!valid) DropBinding();
    return valid;
}
