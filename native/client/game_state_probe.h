#pragma once

#include <windows.h>
#include <stdint.h>
#include <string>

struct GameBuildProfile {
    const char* id;
    DWORD sizeOfImage;
    DWORD peTimestamp;
    DWORD entryPoint;
    uintptr_t isLoadingRva;
    uintptr_t isInGameRva;
    uintptr_t cutsceneIdRva;
};

class GameStateProbe {
public:
    using LogFunction = void (*)(const std::string&);

    explicit GameStateProbe(LogFunction logFunction);
    ~GameStateProbe();

    void Poll();
    bool IsSafeForRpc() const;
    bool IsMapEntitySafe() const;
    bool IsGameplayLoaded() const;
    bool IsLoading() const;
    DWORD GetProcessId() const { return processId_; }

private:
    bool Attach(DWORD processId);
    void Detach();
    DWORD FindGameProcess() const;
    bool FindGameModule(uintptr_t& baseAddress, DWORD& imageSize) const;
    bool ReadPeHeaders(DWORD& sizeOfImage, DWORD& peTimestamp, DWORD& entryPoint) const;
    const GameBuildProfile* DetectBuildProfile(DWORD sizeOfImage, DWORD peTimestamp, DWORD entryPoint) const;
    bool FindIdGameSystemLocal(uintptr_t& address) const;
    bool ReadState(std::string& state, bool& safeForRpc, bool& mapEntitySafe);
    void Report(const std::string& state);

    template <typename T>
    bool Read(uintptr_t address, T& value, DWORD* outWinErr = nullptr) const {
        SIZE_T bytesRead = 0;
        if (!process_) {
            if (outWinErr) {
                *outWinErr = ERROR_INVALID_HANDLE;
            }
            return false;
        }
        const BOOL success = ReadProcessMemory(
            process_,
            reinterpret_cast<const void*>(address),
            &value,
            sizeof(value),
            &bytesRead
        );
        if (!success || bytesRead != sizeof(value)) {
            if (outWinErr) {
                *outWinErr = GetLastError();
            }
            return false;
        }
        return true;
    }

    LogFunction log_;
    HANDLE process_;
    DWORD processId_;
    uintptr_t moduleBase_;
    DWORD moduleSize_;
    const GameBuildProfile* activeProfile_;
    uintptr_t idGameSystemLocal_;
    DWORD nextAttachAttempt_;
    bool safeForRpc_;
    bool mapEntitySafe_;
    bool gameplayLoaded_;
    bool loading_;
    unsigned int consecutiveReadFailures_;
    bool recoveringFromReadFailure_;
    std::string lastReport_;
};
