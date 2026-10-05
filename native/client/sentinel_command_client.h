#pragma once
#include "command_transport.h"
#include "sentinel_inspection.h"
#include <functional>

class SentinelCommandClient : public CommandTransport {
public:
    using LogCallback = std::function<void(const std::string&)>;
    void SetLogCallback(LogCallback callback) { log_ = std::move(callback); }
    void SetTargetProcess(DWORD pid);
    DWORD TargetProcess() const { return pid_; }
    bool Initialize() { return PollHealth(); }
    bool PollHealth();
    bool Ready() const override { return ready_; }
    bool ExecuteConsoleCommand(const std::string& command) override;
    unsigned long long AttachmentEpoch() const { return epoch_; }
    void SetCurrentCommandId(const std::string& id) override { command_id_ = id; result_ = AP_RPC_NONE; status_ = 0; }
    std::string CurrentCommandId() const { return command_id_; }
    ApRpcResult LastResult() const override { return result_; }
    DWORD LastTransportStatus() const override { return status_; }
private:
    DWORD pid_ = 0, status_ = ERROR_FILE_NOT_FOUND;
    bool ready_ = false;
    unsigned long long epoch_ = 0;
    sentinel::Snapshot identity_{};
    std::string command_id_ = "-";
    ApRpcResult result_ = AP_RPC_NONE;
    LogCallback log_;
};
