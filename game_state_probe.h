#pragma once

#include <windows.h>
#include <stdint.h>
#include <string>

class GameStateProbe {
public:
    using LogFunction = void (*)(const std::string&);

    explicit GameStateProbe(LogFunction logFunction);
    ~GameStateProbe();

    void Poll();
    bool IsSafeForRpc() const;

private:
    bool Attach(DWORD processId);
    void Detach();
    DWORD FindGameProcess() const;
    bool FindGameModule(uintptr_t& baseAddress, DWORD& imageSize) const;
    bool FindIdGameSystemLocal(uintptr_t& address) const;
    bool ReadState(std::string& state, bool& safeForRpc) const;
    void Report(const std::string& state);

    template <typename T>
    bool Read(uintptr_t address, T& value) const {
        SIZE_T bytesRead = 0;
        return process_ != nullptr
            && ReadProcessMemory(
                process_,
                reinterpret_cast<const void*>(address),
                &value,
                sizeof(value),
                &bytesRead
            )
            && bytesRead == sizeof(value);
    }

    LogFunction log_;
    HANDLE process_;
    DWORD processId_;
    uintptr_t moduleBase_;
    DWORD moduleSize_;
    uintptr_t idGameSystemLocal_;
    DWORD nextAttachAttempt_;
    bool safeForRpc_;
    std::string lastReport_;
};
