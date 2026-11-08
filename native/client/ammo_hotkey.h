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
        bool safeForRpc,
        ExecuteCommandFunction executeConsoleCommand,
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
    void DisableMissingConfig();
    void LogConfiguration();
    void UpdateLatch(DWORD now, bool isDown);
    void LogPress(bool foreground, bool session, bool rpcReady, bool safe);
    void ExecuteRefill(ExecuteCommandFunction executeConsoleCommand, int rpcLastTransportStatus);

    LogFunction log_;
    std::string stateFilePath_;
    std::string token_;
    int vk_;
    bool wasDown_;
    bool latched_ = false;
    DWORD releaseStartTick_ = 0;
    static constexpr DWORD kReleaseStabilizeMs = 120;
    DWORD lastConfigCheckTick_;
    FILETIME lastWriteTime_;
    bool fileExisted_;
};
