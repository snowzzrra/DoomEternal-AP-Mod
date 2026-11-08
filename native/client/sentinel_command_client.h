#pragma once
#include <windows.h>
#include "sentinel_inspection.h"
#include "protocol_v2.h"
#include <string>

enum ApRpcResult { AP_RPC_NONE, AP_RPC_PIPE_MISSING, AP_RPC_PIPE_BUSY,
    AP_RPC_WAIT_TIMEOUT, AP_RPC_DELIVERED, AP_RPC_EXCEPTION, AP_RPC_UNKNOWN,
    AP_RPC_AMBIGUOUS, AP_RPC_REJECTED };
const char* RpcCallResultName(ApRpcResult result);

class SentinelCommandClient {
public:
    void SetTargetProcess(DWORD pid);
    bool PollHealth();
    bool Ready() const { return ready_; }
    bool ExecuteConsoleCommand(const std::string& command);
    unsigned long long AttachmentEpoch() const { return epoch_; }
    ApRpcResult LastResult() const { return result_; }
    DWORD LastTransportStatus() const { return status_; }
    sentinel::Inspection SaveAdmission();
private:
    bool AwaitCommand(const sentinel::generated::Bytes& body, const sentinel::generated::Bytes& reference, const sentinel::generated::command_reference& ref);
    template<class T> bool Read(uint16_t type, const sentinel::generated::Bytes& payload, T& value);
    DWORD pid_ = 0, status_ = ERROR_FILE_NOT_FOUND;
    bool ready_ = false;
    unsigned long long epoch_ = 0;
    sentinel::Snapshot identity_{};
    sentinel::V2Connection connection_;
    sentinel::generated::scope scope_{};
    std::array<uint8_t, 16> binding_{};
    ApRpcResult result_ = AP_RPC_NONE;
};
