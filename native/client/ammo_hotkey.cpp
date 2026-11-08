#include "ammo_hotkey.h"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <sstream>
#include <iomanip>

namespace {

std::string ToUpperTrimmed(const std::string& input) {
    std::string result;
    result.reserve(input.size());
    for (char c : input) {
        if (!std::isspace(static_cast<unsigned char>(c))) {
            result.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
        }
    }
    return result;
}

std::string HexVk(int vk) {
    std::ostringstream ss;
    ss << "0x" << std::hex << std::setw(2) << std::setfill('0') << std::uppercase << (vk & 0xFF);
    return ss.str();
}

}  // namespace

AmmoHotkeyHandler::AmmoHotkeyHandler(LogFunction logFunction, std::string stateFilePath)
    : log_(logFunction),
      stateFilePath_(std::move(stateFilePath)),
      token_("UNBOUND"),
      vk_(0),
      wasDown_(false),
      lastConfigCheckTick_(0),
      lastWriteTime_({}),
      fileExisted_(false) {
    CheckConfigFile();
}

std::string AmmoHotkeyHandler::CanonicalToken(const std::string& input) {
    const std::string upper = ToUpperTrimmed(input);
    if (upper.empty() || upper == "UNBOUND") {
        return "UNBOUND";
    }
    if (upper == "PGUP") {
        return "PAGEUP";
    }
    if (upper == "PGDN") {
        return "PAGEDOWN";
    }
    return upper;
}


namespace {
int FunctionVirtualKey(const std::string& token) {
    int number = 0;
    for (size_t i = 1; i < token.size(); ++i) {
        if (token[i] < '0' || token[i] > '9' || number > 12) return 0;
        number = number * 10 + token[i] - '0';
    }
    return number >= 1 && number <= 12 ? VK_F1 + number - 1 : 0;
}

int NamedVirtualKey(const std::string& token) {
    static const std::pair<const char*, int> keys[] = {
        {"SPACE", VK_SPACE}, {"TAB", VK_TAB}, {"BACKSPACE", VK_BACK},
        {"INSERT", VK_INSERT}, {"DELETE", VK_DELETE}, {"HOME", VK_HOME},
        {"END", VK_END}, {"PAGEUP", VK_PRIOR}, {"PAGEDOWN", VK_NEXT},
        {"UP", VK_UP}, {"DOWN", VK_DOWN}, {"LEFT", VK_LEFT}, {"RIGHT", VK_RIGHT},
    };
    for (const auto& key : keys) if (token == key.first) return key.second;
    return 0;
}
}

int AmmoHotkeyHandler::TokenToVirtualKey(const std::string& token) {
    const std::string canonical = CanonicalToken(token);
    if (canonical == "UNBOUND") return 0;
    if (canonical.size() >= 2 && canonical.size() <= 15 && canonical[0] == 'F') {
        return FunctionVirtualKey(canonical);
    }
    if (canonical.size() == 1) {
        if (canonical[0] >= 'A' && canonical[0] <= 'Z') return static_cast<int>(canonical[0]);
        if (canonical[0] >= '0' && canonical[0] <= '9') return static_cast<int>(canonical[0]);
    }
    return NamedVirtualKey(canonical);
}

bool AmmoHotkeyHandler::IsDoomForeground(DWORD doomPid) {
    if (doomPid == 0) {
        return false;
    }
    HWND foreground = GetForegroundWindow();
    if (!foreground) {
        return false;
    }
    DWORD foregroundPid = 0;
    GetWindowThreadProcessId(foreground, &foregroundPid);
    return foregroundPid == doomPid;
}

static std::string ReadHotkeyToken(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    char bytes[128]{};
    std::string newToken = "UNBOUND";
    if (file && file.read(bytes, sizeof(bytes) - 1).eof() &&
        std::find(bytes, bytes + file.gcount(), '\0') == bytes + file.gcount()) {
        std::istringstream stream(std::string(bytes, static_cast<size_t>(file.gcount())));
        std::string first, second, extra;
        if (stream >> first) {
            if (!(stream >> second)) newToken = AmmoHotkeyHandler::CanonicalToken(first);
            else if (first == "AP_AMMO_REFILL_HOTKEY_V1" && !(stream >> extra))
                newToken = AmmoHotkeyHandler::CanonicalToken(second);
        }
    }

    return newToken;
}

void AmmoHotkeyHandler::CheckConfigFile() {
    WIN32_FILE_ATTRIBUTE_DATA attr = {};
    const BOOL exists = GetFileAttributesExA(
        stateFilePath_.c_str(),
        GetFileExInfoStandard,
        &attr
    );

    if (!exists) {
        DisableMissingConfig();
        return;
    }

    const bool isNewOrModified = !fileExisted_
        || CompareFileTime(&attr.ftLastWriteTime, &lastWriteTime_) != 0;

    if (!isNewOrModified) {
        return;
    }

    fileExisted_ = true;
    lastWriteTime_ = attr.ftLastWriteTime;

    const std::string newToken = ReadHotkeyToken(stateFilePath_);
    const int newVk = TokenToVirtualKey(newToken);
    if (newToken != token_ || newVk != vk_) {
        token_ = newToken;
        vk_ = newVk;
        wasDown_ = false;
        latched_ = true;
        releaseStartTick_ = 0;
        LogConfiguration();
    }
}


static std::string RefillRefusal(bool foreground, bool session, bool rpcReady, bool safe) {
    if (!foreground) return "game_not_foreground";
    if (!session) return "session_unavailable";
    if (!rpcReady) return "rpc_unavailable";
    if (!safe) return "unsafe_gameplay";
    return {};
}

void AmmoHotkeyHandler::Poll(
    DWORD doomPid,
    bool sessionActive,
    bool rpcTransportReady,
    bool safeForRpc,
    ExecuteCommandFunction executeConsoleCommand,
    int rpcLastTransportStatus
) {
    const DWORD now = GetTickCount();
    if (now - lastConfigCheckTick_ >= 250) {
        lastConfigCheckTick_ = now;
        CheckConfigFile();
    }

    if (vk_ == 0) {
        wasDown_ = false;
        latched_ = false;
        releaseStartTick_ = 0;
        return;
    }

    const SHORT keyState = GetAsyncKeyState(vk_);
    const bool isDown = (keyState & 0x8000) != 0;

    if (latched_) {
        UpdateLatch(now, isDown);
        return;
    }

    const bool risingEdge = isDown && !wasDown_;
    wasDown_ = isDown;

    if (!risingEdge) {
        return;
    }

    const bool foreground = IsDoomForeground(doomPid);
    LogPress(foreground, sessionActive, rpcTransportReady, safeForRpc);

    latched_ = true;
    releaseStartTick_ = 0;

    const std::string reason = RefillRefusal(foreground, sessionActive, rpcTransportReady, safeForRpc);
    if (reason.empty()) {
        ExecuteRefill(executeConsoleCommand, rpcLastTransportStatus);
    } else if (log_) {
        log_("AMMO_HOTKEY_REFUSED key=" + token_ + " reason=" + reason);
    }
}

void AmmoHotkeyHandler::LogConfiguration() {
        if (log_) {
            if (vk_ != 0) {
                log_(
                    "AMMO_HOTKEY_CONFIG path=" + stateFilePath_
                    + " token=" + token_
                    + " vk=" + HexVk(vk_)
                    + " state=loaded"
                );
            } else {
                log_("AMMO_HOTKEY_CONFIG token=" + (token_.empty() ? "UNBOUND" : token_) + " state=disabled");
            }
        }
}

void AmmoHotkeyHandler::DisableMissingConfig() {
        if (fileExisted_) {
            fileExisted_ = false;
            token_ = "UNBOUND";
            vk_ = 0;
            wasDown_ = false;
            latched_ = false;
            releaseStartTick_ = 0;
            if (log_) {
                log_("AMMO_HOTKEY_CONFIG token=UNBOUND state=disabled");
            }
        }
}

void AmmoHotkeyHandler::UpdateLatch(DWORD now, bool isDown) {
        wasDown_ = isDown;
        if (isDown) {
            releaseStartTick_ = 0;
            return;
        }
        if (releaseStartTick_ == 0) {
            releaseStartTick_ = now;
            return;
        }
        if (now - releaseStartTick_ < kReleaseStabilizeMs) {
            return;
        }
        latched_ = false;
        releaseStartTick_ = 0;
        wasDown_ = false;
        return;
}

void AmmoHotkeyHandler::ExecuteRefill(ExecuteCommandFunction executeConsoleCommand, int rpcLastTransportStatus) {
        const std::string command = "condump AP_REFILL_REQUEST.txt";
        const bool success = executeConsoleCommand(command);
        if (log_) {
            log_(
                "AMMO_HOTKEY_EXECUTE key=" + token_
                + " command=" + command
                + " result=" + (success ? "accepted" : "failed")
                + " transport_status=" + std::to_string(rpcLastTransportStatus)
            );
        }
}

void AmmoHotkeyHandler::LogPress(bool foreground, bool session, bool rpcReady, bool safe) {
    if (log_) {
        log_("AMMO_HOTKEY_PRESS key=" + token_
            + " foreground=" + (foreground ? "true" : "false")
            + " session=" + (session ? "true" : "false")
            + " rpc_ready=" + (rpcReady ? "true" : "false")
            + " safe=" + (safe ? "true" : "false"));
    }
}
