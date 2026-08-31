#pragma once

#include <rpc.h>
#include <windows.h>
#include <functional>
#include <string>

enum ApRpcResult { AP_RPC_NONE, AP_RPC_PIPE_MISSING, AP_RPC_PIPE_BUSY,
    AP_RPC_WAIT_TIMEOUT, AP_RPC_DELIVERED, AP_RPC_EXCEPTION, AP_RPC_UNKNOWN };

class ApRuntimeRpcClient {
public:
    using LogCallback = std::function<void(const std::string&)>;
    ApRuntimeRpcClient();
    ~ApRuntimeRpcClient();
    ApRuntimeRpcClient(const ApRuntimeRpcClient&) = delete;
    ApRuntimeRpcClient& operator=(const ApRuntimeRpcClient&) = delete;
    ApRuntimeRpcClient(ApRuntimeRpcClient&&) = delete;
    ApRuntimeRpcClient& operator=(ApRuntimeRpcClient&&) = delete;
    void SetLogCallback(LogCallback callback);
    bool Initialize();
    bool PollHealth();
    bool Ready() const;
    bool ExecuteConsoleCommand(const std::string& command);
    unsigned long long AttachmentEpoch() const;
    bool RequestEntityLoad(const std::string& path, bool begin, int size = 0);
    bool RetrieveEntities(unsigned char* data, size_t* capacity);
    bool Checkpoint(int* size, unsigned char* data, int capacity);
    bool Spawn(int* size, unsigned char* data, int capacity);
    void SetCurrentCommandId(const std::string& id);
    std::string CurrentCommandId() const;
    ApRpcResult LastResult() const;
    DWORD LastTransportStatus() const;

private:
    RPC_BINDING_HANDLE binding_ = nullptr;
    RPC_CSTR string_binding_ = nullptr;
    bool ready_ = false;
    DWORD next_health_tick_ = 0;
    DWORD status_ = RPC_S_OK;
    ApRpcResult result_ = AP_RPC_NONE;
    std::string command_id_ = "-";
    unsigned long long call_sequence_ = 0;
    unsigned long long attachment_epoch_ = 0;
    bool health_log_initialized_ = false;
    bool health_available_ = false;
    DWORD next_health_summary_tick_ = 0;
    unsigned long health_suppressed_failures_ = 0;
    LogCallback log_callback_;

    bool InitializeUnlocked();
    bool Prepare(const char* operation, DWORD* start_tick, unsigned long long* call_id);
    bool SetCallTimeout(ULONG milliseconds);
    void Record(const char* operation, DWORD start_tick, unsigned long long call_id,
        ApRpcResult result, DWORD status);
    void DropBinding();
    static ApRpcResult ClassifyWaitFailure(DWORD status);
    static bool TickReached(DWORD now, DWORD deadline);
    static bool RpcTraceEnabled();
};
