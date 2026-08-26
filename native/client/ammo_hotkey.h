#pragma once

#include <windows.h>
#include <string>
#include <functional>

class AmmoHotkeyHandler {
public:
    using LogFunction = void (*)(const std::string&);
    using ExecuteCommandFunction = std::function<bool(const std::string&)>;

    explicit AmmoHotkeyHandler(LogFunction logFunction, std::string stateFilePath = "base\\ap_queue\\ammo_refill_hotkey.state");

    void Poll(
        DWORD doomPid,
        bool sessionActive,
        bool rpcTransportReady,
        bool rpcArmed,
        bool safeForRpc,
        ExecuteCommandFunction executeConsoleCommand,
        int rpcLastResult = 0,
        int rpcLastTransportStatus = 0
    );

    static int TokenToVirtualKey(const std::string& token);
    static std::string CanonicalToken(const std::string& token);
    static bool IsDoomForeground(DWORD doomPid);

    const std::string& GetConfiguredToken() const { return token_; }
    int GetVirtualKey() const { return vk_; }
    bool WasDown() const { return wasDown_; }

private:
    void CheckConfigFile();

    LogFunction log_;
    std::string stateFilePath_;
    std::string token_;
    int vk_;
    bool wasDown_;
    DWORD lastConfigCheckTick_;
    FILETIME lastWriteTime_;
    bool fileExisted_;
};
