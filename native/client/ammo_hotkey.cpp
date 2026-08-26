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
    if (upper.empty() || upper == "UNBOUND" || upper == "NONE") {
        return "UNBOUND";
    }
    if (upper == "PGUP" || upper == "PAGE_UP") {
        return "PAGEUP";
    }
    if (upper == "PGDN" || upper == "PAGE_DOWN") {
        return "PAGEDOWN";
    }
    return upper;
}

int AmmoHotkeyHandler::TokenToVirtualKey(const std::string& token) {
    const std::string canonical = CanonicalToken(token);
    if (canonical == "UNBOUND") {
        return 0;
    }

    // Function keys F1..F12
    if (canonical.size() >= 2 && canonical[0] == 'F') {
        const std::string numStr = canonical.substr(1);
        if (!numStr.empty() && std::all_of(numStr.begin(), numStr.end(), ::isdigit)) {
            int num = std::stoi(numStr);
            if (num >= 1 && num <= 12) {
                return VK_F1 + (num - 1);
            }
        }
    }

    // Letters A..Z
    if (canonical.size() == 1 && canonical[0] >= 'A' && canonical[0] <= 'Z') {
        return static_cast<int>(canonical[0]);
    }

    // Digits 0..9
    if (canonical.size() == 1 && canonical[0] >= '0' && canonical[0] <= '9') {
        return static_cast<int>(canonical[0]);
    }

    // Common named keys
    if (canonical == "SPACE") return VK_SPACE;
    if (canonical == "TAB") return VK_TAB;
    if (canonical == "BACKSPACE") return VK_BACK;
    if (canonical == "INSERT") return VK_INSERT;
    if (canonical == "DELETE") return VK_DELETE;
    if (canonical == "HOME") return VK_HOME;
    if (canonical == "END") return VK_END;
    if (canonical == "PAGEUP") return VK_PRIOR;
    if (canonical == "PAGEDOWN") return VK_NEXT;
    if (canonical == "UP") return VK_UP;
    if (canonical == "DOWN") return VK_DOWN;
    if (canonical == "LEFT") return VK_LEFT;
    if (canonical == "RIGHT") return VK_RIGHT;

    return 0;
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

void AmmoHotkeyHandler::CheckConfigFile() {
    WIN32_FILE_ATTRIBUTE_DATA attr = {};
    const BOOL exists = GetFileAttributesExA(
        stateFilePath_.c_str(),
        GetFileExInfoStandard,
        &attr
    );

    if (!exists) {
        if (fileExisted_) {
            fileExisted_ = false;
            token_ = "UNBOUND";
            vk_ = 0;
            wasDown_ = false;
            if (log_) {
                log_("AMMO_HOTKEY_CONFIG token=UNBOUND state=disabled");
            }
        }
        return;
    }

    const bool isNewOrModified = !fileExisted_
        || CompareFileTime(&attr.ftLastWriteTime, &lastWriteTime_) != 0;

    if (!isNewOrModified) {
        return;
    }

    fileExisted_ = true;
    lastWriteTime_ = attr.ftLastWriteTime;

    std::ifstream file(stateFilePath_);
    std::string rawLine;
    std::string newToken = "UNBOUND";
    if (file && std::getline(file, rawLine)) {
        std::istringstream stream(rawLine);
        std::string header;
        if (stream >> header) {
            if (header == "AP_AMMO_REFILL_HOTKEY_V1") {
                std::string key;
                if (stream >> key) {
                    newToken = CanonicalToken(key);
                }
            } else {
                newToken = CanonicalToken(header);
            }
        }
    }

    const int newVk = TokenToVirtualKey(newToken);
    if (newToken != token_ || newVk != vk_) {
        token_ = newToken;
        vk_ = newVk;
        wasDown_ = false;
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
}

void AmmoHotkeyHandler::Poll(
    DWORD doomPid,
    bool sessionActive,
    bool rpcTransportReady,
    bool rpcArmed,
    bool safeForRpc,
    ExecuteCommandFunction executeConsoleCommand,
    int rpcLastResult,
    int rpcLastTransportStatus
) {
    const DWORD now = GetTickCount();
    if (now - lastConfigCheckTick_ >= 250) {
        lastConfigCheckTick_ = now;
        CheckConfigFile();
    }

    if (vk_ == 0) {
        wasDown_ = false;
        return;
    }

    const SHORT keyState = GetAsyncKeyState(vk_);
    const bool isDown = (keyState & 0x8000) != 0;
    const bool risingEdge = isDown && !wasDown_;
    wasDown_ = isDown;

    if (!risingEdge) {
        return;
    }

    const bool foreground = IsDoomForeground(doomPid);
    const bool session = sessionActive;
    const bool rpcReady = rpcTransportReady;
    const bool armed = rpcArmed;
    const bool safe = safeForRpc;

    if (log_) {
        log_(
            "AMMO_HOTKEY_PRESS key=" + token_
            + " foreground=" + (foreground ? "true" : "false")
            + " session=" + (session ? "true" : "false")
            + " rpc_ready=" + (rpcReady ? "true" : "false")
            + " rpc_armed=" + (armed ? "true" : "false")
            + " safe=" + (safe ? "true" : "false")
        );
    }

    if (foreground && session && rpcReady && armed && safe) {
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
    } else {
        std::string reason;
        if (!foreground) {
            reason = "game_not_foreground";
        } else if (!session) {
            reason = "session_unavailable";
        } else if (!rpcReady) {
            reason = "rpc_unavailable";
        } else if (!armed) {
            reason = "rpc_disarmed";
        } else if (!safe) {
            reason = "unsafe_gameplay";
        } else {
            reason = "rejected";
        }
        if (log_) {
            log_("AMMO_HOTKEY_REFUSED key=" + token_ + " reason=" + reason);
        }
    }
}
