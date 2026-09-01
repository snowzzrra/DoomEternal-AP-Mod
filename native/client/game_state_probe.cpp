#include "game_state_probe.h"

#include <tlhelp32.h>
#include <algorithm>
#include <cstring>
#include <sstream>
#include <vector>

namespace {

constexpr const char* kGameExecutable = "DOOMEternalx64vk.exe";

constexpr GameBuildProfile kSupportedBuildProfiles[] = {
    {
        /* id */ "Steam pre-2026-08-18",
        /* sizeOfImage */ 0x742A000,
        /* peTimestamp */ 0,
        /* entryPoint */ 0,
        /* isLoadingRva */ 0x5471E18,
        /* isInGameRva */ 0x6B8DA98,
        /* cutsceneIdRva */ 0x540D4C8,
    },
    {
        /* id */ "Steam 2026-08-18",
        /* sizeOfImage */ 0x7431000,
        /* peTimestamp */ 0x6A7B9B8C,
        /* entryPoint */ 0x286CAA8,
        /* isLoadingRva */ 0x5477298,
        /* isInGameRva */ 0x6B92F18,
        /* cutsceneIdRva */ 0x5412948,
    },
};

constexpr uintptr_t kMapInstanceOffset = 0x50;
constexpr uintptr_t kPlayerOffset = 0x1AF8;
constexpr uintptr_t kGameWasPausedOffset = 0x476BC;
constexpr uintptr_t kHideReticleOffset = 0x84A5;
constexpr uintptr_t kHideHudForCinematicOffset = 0x84A6;
constexpr DWORD kAttachRetryMs = 1000;
constexpr SIZE_T kScanChunkSize = 1024 * 1024;

const unsigned char kGameSystemSignature[] = {
    0x48, 0x8D, 0x0D, 0, 0, 0, 0, 0xE8,
    0, 0, 0, 0, 0x84, 0xC0, 0x48, 0x8D,
    0x0D, 0, 0, 0, 0, 0x49, 0x8B, 0xD4,
};

const char kGameSystemMask[] = "xxx????x????xxxxx????xxx";

bool IsReadableProtection(DWORD protection) {
    if ((protection & PAGE_GUARD) != 0 || protection == PAGE_NOACCESS) {
        return false;
    }
    const DWORD baseProtection = protection & 0xff;
    return baseProtection == PAGE_READONLY
        || baseProtection == PAGE_READWRITE
        || baseProtection == PAGE_WRITECOPY
        || baseProtection == PAGE_EXECUTE
        || baseProtection == PAGE_EXECUTE_READ
        || baseProtection == PAGE_EXECUTE_READWRITE
        || baseProtection == PAGE_EXECUTE_WRITECOPY;
}

bool MatchesSignature(const unsigned char* data) {
    for (SIZE_T index = 0; index < sizeof(kGameSystemSignature); ++index) {
        if (kGameSystemMask[index] == 'x' && data[index] != kGameSystemSignature[index]) {
            return false;
        }
    }
    return true;
}

bool IsPlausiblePointer(uintptr_t address) {
    return address >= 0x10000 && address <= 0x00007FFFFFFFFFFFULL;
}

std::string Hex(uintptr_t value) {
    std::ostringstream output;
    output << "0x" << std::hex << std::uppercase << value;
    return output.str();
}

const char* BoolText(unsigned char value) {
    return value ? "1" : "0";
}

}  // namespace

GameStateProbe::GameStateProbe(LogFunction logFunction)
    : log_(logFunction),
      process_(nullptr),
      processId_(0),
      moduleBase_(0),
      moduleSize_(0),
      activeProfile_(nullptr),
      idGameSystemLocal_(0),
      nextAttachAttempt_(0),
      safeForRpc_(false),
      mapEntitySafe_(false),
      gameplayLoaded_(false),
      loading_(false),
      consecutiveReadFailures_(0),
      recoveringFromReadFailure_(false) {}

GameStateProbe::~GameStateProbe() {
    Detach();
}

DWORD GameStateProbe::FindGameProcess() const {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) {
        return 0;
    }

    PROCESSENTRY32 entry = {};
    entry.dwSize = sizeof(entry);
    DWORD processId = 0;
    if (Process32First(snapshot, &entry)) {
        do {
            if (_stricmp(entry.szExeFile, kGameExecutable) == 0) {
                processId = entry.th32ProcessID;
                break;
            }
        } while (Process32Next(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return processId;
}

bool GameStateProbe::FindGameModule(uintptr_t& baseAddress, DWORD& imageSize) const {
    HANDLE snapshot = CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
        processId_
    );
    if (snapshot == INVALID_HANDLE_VALUE) {
        return false;
    }

    MODULEENTRY32 entry = {};
    entry.dwSize = sizeof(entry);
    bool found = false;
    if (Module32First(snapshot, &entry)) {
        do {
            if (_stricmp(entry.szModule, kGameExecutable) == 0) {
                baseAddress = reinterpret_cast<uintptr_t>(entry.modBaseAddr);
                imageSize = entry.modBaseSize;
                found = true;
                break;
            }
        } while (Module32Next(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return found;
}

bool GameStateProbe::ReadPeHeaders(
    DWORD& sizeOfImage,
    DWORD& peTimestamp,
    DWORD& entryPoint
) const {
    sizeOfImage = 0;
    peTimestamp = 0;
    entryPoint = 0;
    if (!process_ || !moduleBase_) {
        return false;
    }

    IMAGE_DOS_HEADER dos = {};
    if (!Read(moduleBase_, dos) || dos.e_magic != IMAGE_DOS_SIGNATURE) {
        return false;
    }
    if (dos.e_lfanew <= 0 || dos.e_lfanew > 0x1000) {
        return false;
    }

    const uintptr_t ntHeaderAddress = moduleBase_ + static_cast<uintptr_t>(dos.e_lfanew);
    DWORD ntSignature = 0;
    if (!Read(ntHeaderAddress, ntSignature) || ntSignature != IMAGE_NT_SIGNATURE) {
        return false;
    }

    IMAGE_FILE_HEADER fileHeader = {};
    if (!Read(ntHeaderAddress + sizeof(DWORD), fileHeader)) {
        return false;
    }
    if (fileHeader.Machine != IMAGE_FILE_MACHINE_AMD64) {
        return false;
    }

    IMAGE_OPTIONAL_HEADER64 optHeader = {};
    if (!Read(ntHeaderAddress + sizeof(DWORD) + sizeof(IMAGE_FILE_HEADER), optHeader)) {
        return false;
    }
    if (optHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) {
        return false;
    }

    sizeOfImage = optHeader.SizeOfImage;
    peTimestamp = fileHeader.TimeDateStamp;
    entryPoint = optHeader.AddressOfEntryPoint;
    return true;
}

const GameBuildProfile* GameStateProbe::DetectBuildProfile(
    DWORD sizeOfImage,
    DWORD peTimestamp,
    DWORD entryPoint
) const {
    for (const auto& candidate : kSupportedBuildProfiles) {
        if (candidate.sizeOfImage != sizeOfImage) {
            continue;
        }
        if (candidate.peTimestamp != 0 && candidate.peTimestamp != peTimestamp) {
            continue;
        }
        if (candidate.entryPoint != 0 && candidate.entryPoint != entryPoint) {
            continue;
        }
        return &candidate;
    }
    return nullptr;
}

bool GameStateProbe::FindIdGameSystemLocal(uintptr_t& address) const {
    const uintptr_t imageEnd = moduleBase_ + moduleSize_;
    uintptr_t cursor = moduleBase_;
    uintptr_t matchAddress = 0;
    size_t matchCount = 0;

    while (cursor < imageEnd) {
        MEMORY_BASIC_INFORMATION memory = {};
        if (VirtualQueryEx(
                process_,
                reinterpret_cast<const void*>(cursor),
                &memory,
                sizeof(memory)
            ) != sizeof(memory)) {
            return false;
        }

        const uintptr_t regionStart = std::max(
            cursor,
            reinterpret_cast<uintptr_t>(memory.BaseAddress)
        );
        const uintptr_t regionEnd = std::min(
            imageEnd,
            reinterpret_cast<uintptr_t>(memory.BaseAddress) + memory.RegionSize
        );

        if (memory.State == MEM_COMMIT && IsReadableProtection(memory.Protect)) {
            uintptr_t chunkStart = regionStart;
            while (chunkStart < regionEnd) {
                const SIZE_T remaining = regionEnd - chunkStart;
                const SIZE_T chunkSize = std::min(kScanChunkSize, remaining);
                std::vector<unsigned char> buffer(chunkSize);
                SIZE_T bytesRead = 0;
                if (ReadProcessMemory(
                        process_,
                        reinterpret_cast<const void*>(chunkStart),
                        buffer.data(),
                        chunkSize,
                        &bytesRead
                    ) && bytesRead >= sizeof(kGameSystemSignature)) {
                    for (SIZE_T index = 0;
                         index + sizeof(kGameSystemSignature) <= bytesRead;
                         ++index) {
                        if (MatchesSignature(buffer.data() + index)) {
                            matchAddress = chunkStart + index;
                            ++matchCount;
                            if (matchCount > 1) {
                                return false;
                            }
                        }
                    }
                }

                if (chunkSize == remaining) {
                    break;
                }
                chunkStart += chunkSize - (sizeof(kGameSystemSignature) - 1);
            }
        }

        if (regionEnd <= cursor) {
            return false;
        }
        cursor = regionEnd;
    }

    if (matchCount != 1) {
        return false;
    }

    int32_t displacement = 0;
    if (!Read(matchAddress + 3, displacement)) {
        return false;
    }
    const uintptr_t resolved = matchAddress + 7 + static_cast<intptr_t>(displacement);
    if (resolved < moduleBase_ || resolved >= imageEnd) {
        return false;
    }
    address = resolved;
    return true;
}

bool GameStateProbe::Attach(DWORD processId) {
    Detach();
    processId_ = processId;
    process_ = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, processId_);
    if (!process_) {
        Report("Memory probe unavailable: OpenProcess failed (error "
            + std::to_string(GetLastError()) + ").");
        processId_ = 0;
        return false;
    }

    if (!FindGameModule(moduleBase_, moduleSize_)) {
        Report("Memory probe unavailable: game module not found.");
        Detach();
        return false;
    }

    DWORD peSizeOfImage = 0;
    DWORD peTimestamp = 0;
    DWORD entryPoint = 0;
    if (!ReadPeHeaders(peSizeOfImage, peTimestamp, entryPoint)) {
        Report("Memory probe unavailable: unable to read PE headers from game module.");
        Detach();
        return false;
    }

    const DWORD effectiveImageSize = peSizeOfImage ? peSizeOfImage : moduleSize_;
    activeProfile_ = DetectBuildProfile(effectiveImageSize, peTimestamp, entryPoint);
    if (!activeProfile_) {
        Report(
            "Memory probe unavailable: unsupported DOOM Eternal build (SizeOfImage="
            + Hex(effectiveImageSize) + " TimeDateStamp=" + Hex(peTimestamp) + ")."
        );
        Detach();
        return false;
    }

    if (!FindIdGameSystemLocal(idGameSystemLocal_)) {
        Report("Memory probe unavailable: idGameSystemLocal signature was not unique/readable.");
        Detach();
        return false;
    }

    Report(
        "Memory probe attached read-only: pid=" + std::to_string(processId_)
        + " build=" + std::string(activeProfile_->id)
        + " base=" + Hex(moduleBase_)
        + " SizeOfImage=" + Hex(effectiveImageSize)
        + " idGameSystemLocal=" + Hex(idGameSystemLocal_) + "."
    );
    return true;
}

void GameStateProbe::Detach() {
    if (process_) {
        CloseHandle(process_);
    }
    process_ = nullptr;
    processId_ = 0;
    moduleBase_ = 0;
    moduleSize_ = 0;
    activeProfile_ = nullptr;
    idGameSystemLocal_ = 0;
    safeForRpc_ = false;
    mapEntitySafe_ = false;
    gameplayLoaded_ = false;
    loading_ = false;
    consecutiveReadFailures_ = 0;
}

bool GameStateProbe::ReadState(
    std::string& state,
    bool& safeForRpc,
    bool& mapEntitySafe
) {
    safeForRpc = false;
    mapEntitySafe = false;
    if (!activeProfile_) {
        state = "Memory state unavailable: no active build profile.";
        return false;
    }
    unsigned char isLoading = 0;
    unsigned char isInGame = 0;
    int32_t cutsceneId = 0;
    DWORD winErr = 0;
    if (!Read(moduleBase_ + activeProfile_->isLoadingRva, isLoading, &winErr)) {
        state = "Memory state unavailable: global read failed field=isLoading winerr="
            + std::to_string(winErr) + ".";
        return false;
    }
    if (!Read(moduleBase_ + activeProfile_->isInGameRva, isInGame, &winErr)) {
        state = "Memory state unavailable: global read failed field=isInGame winerr="
            + std::to_string(winErr) + ".";
        return false;
    }
    if (!Read(moduleBase_ + activeProfile_->cutsceneIdRva, cutsceneId, &winErr)) {
        state = "Memory state unavailable: global read failed field=cutsceneID winerr="
            + std::to_string(winErr) + ".";
        return false;
    }
    if (isLoading > 1 || isInGame > 1
            || cutsceneId < 0 || cutsceneId > 100000) {
        state = "Memory state unavailable: unexpected global value"
            " isLoading=" + std::to_string(isLoading)
            + " isInGame=" + std::to_string(isInGame)
            + " cutsceneID=" + std::to_string(cutsceneId) + ".";
        return false;
    }


    uintptr_t mapInstance = 0;
    uintptr_t player = 0;
    unsigned char gameWasPaused = 0;
    unsigned char hideReticle = 0;
    unsigned char hideHudForCinematic = 0;
    std::string playerState;
    bool playerAvailable = false;

    if (!Read(idGameSystemLocal_ + kMapInstanceOffset, mapInstance)) {
        playerState = "map_read_failed";
    } else if (!IsPlausiblePointer(mapInstance)) {
        playerState = mapInstance == 0 ? "map_null" : "map_invalid";
    } else {
        if (!Read(mapInstance + kPlayerOffset, player)) {
            playerState = "player_read_failed";
        } else if (!IsPlausiblePointer(player)) {
            playerState = player == 0 ? "player_null" : "player_invalid";
        } else if (!Read(player + kGameWasPausedOffset, gameWasPaused)
                || !Read(player + kHideReticleOffset, hideReticle)
                || !Read(player + kHideHudForCinematicOffset, hideHudForCinematic)) {
            playerState = "flags_read_failed";
        } else if (gameWasPaused > 1 || hideReticle > 1 || hideHudForCinematic > 1) {
            playerState = "flags_unexpected";
        } else {
            playerAvailable = true;
            playerState = "available";
        }
    }

    const bool cutsceneActive = cutsceneId > 1;
    const bool gameplayLoaded = playerAvailable
        && !isLoading
        && isInGame;
    loading_ = isLoading != 0;
    const bool safeCandidate = playerAvailable
        && !isLoading
        && isInGame
        && !cutsceneActive
        && !gameWasPaused
        && !hideReticle
        && !hideHudForCinematic;
    safeForRpc = safeCandidate;
    mapEntitySafe = gameplayLoaded && !cutsceneActive;
    gameplayLoaded_ = gameplayLoaded;

    state = std::string("Memory state: gameplay_loaded=")
        + (gameplayLoaded ? "YES" : "NO")
        + " safe_candidate="
        + (safeCandidate ? "YES" : "NO")
        + " map_entity_safe="
        + (mapEntitySafe ? "YES" : "NO")
        + " isLoading=" + BoolText(isLoading)
        + " isInGame=" + BoolText(isInGame)
        + " cutsceneID=" + std::to_string(cutsceneId)
        + " cutscene_active=" + (cutsceneActive ? "1" : "0")
        + " player=" + playerState;
    if (playerAvailable) {
        state += " gameWasPaused=" + std::string(BoolText(gameWasPaused))
            + " hideReticle=" + BoolText(hideReticle)
            + " hideHudForCinematic=" + BoolText(hideHudForCinematic);
    }
    state += ".";
    return true;
}

bool GameStateProbe::IsSafeForRpc() const {
    return safeForRpc_;
}

bool GameStateProbe::IsMapEntitySafe() const {
    return mapEntitySafe_;
}

bool GameStateProbe::IsGameplayLoaded() const {
    return gameplayLoaded_;
}

bool GameStateProbe::IsLoading() const {
    return loading_;
}

void GameStateProbe::Report(const std::string& state) {
    if (state == lastReport_) {
        return;
    }
    lastReport_ = state;
    if (log_) {
        log_(state);
    }
}

void GameStateProbe::Poll() {
    if (!process_) {
        const DWORD now = GetTickCount();
        if (now < nextAttachAttempt_) {
            return;
        }
        nextAttachAttempt_ = now + kAttachRetryMs;
        const DWORD processId = FindGameProcess();
        if (!processId) {
            Report("Memory probe unavailable: waiting for DOOMEternalx64vk.exe.");
            return;
        }
        if (!Attach(processId)) {
            return;
        }
    }

    if (WaitForSingleObject(process_, 0) == WAIT_OBJECT_0) {
        Detach();
        recoveringFromReadFailure_ = false;
        Report("Memory probe unavailable: game process exited.");
        return;
    }

    std::string state;
    bool safeForRpc = false;
    bool mapEntitySafe = false;
    if (!ReadState(state, safeForRpc, mapEntitySafe)) {
        ++consecutiveReadFailures_;
        recoveringFromReadFailure_ = true;
        gameplayLoaded_ = false;
        mapEntitySafe_ = false;
        safeForRpc_ = false;
        loading_ = false;
        Report(state);
        if (consecutiveReadFailures_ >= 3) {
            Report("Memory probe read failure threshold reached; reattaching.");
            Detach();
            nextAttachAttempt_ = GetTickCount() + kAttachRetryMs;
        }
        return;
    }

    if (recoveringFromReadFailure_) {
        Report("Memory probe recovered.");
        recoveringFromReadFailure_ = false;
        consecutiveReadFailures_ = 0;
    }

    safeForRpc_ = safeForRpc;
    mapEntitySafe_ = mapEntitySafe;
    Report(state);
}
