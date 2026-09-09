#pragma once
#include <windows.h>
#include <string>

enum ApRpcResult { AP_RPC_NONE, AP_RPC_PIPE_MISSING, AP_RPC_PIPE_BUSY,
    AP_RPC_WAIT_TIMEOUT, AP_RPC_DELIVERED, AP_RPC_EXCEPTION, AP_RPC_UNKNOWN };

class CommandTransport {
public:
    virtual ~CommandTransport() = default;
    virtual bool Ready() const = 0;
    virtual bool ExecuteConsoleCommand(const std::string& command) = 0;
    virtual bool RequestEntityLoad(const std::string& path, bool begin, int size) = 0;
    virtual bool RetrieveEntities(unsigned char* data, size_t* capacity) = 0;
    virtual void SetCurrentCommandId(const std::string& id) = 0;
    virtual ApRpcResult LastResult() const = 0;
    virtual DWORD LastTransportStatus() const = 0;
};
