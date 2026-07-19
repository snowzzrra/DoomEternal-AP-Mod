#pragma once

#include <algorithm>
#include <cstdint>
#include <string>
#include <unordered_set>

inline bool IsNormalReceiptCommandId(const std::string& commandId) {
    return commandId.rfind("recv-", 0) == 0;
}

inline unsigned long ReceiptRetryDelayMs(unsigned int attempt) {
    constexpr unsigned long kBaseMs = 250;
    constexpr unsigned long kMaximumMs = 8000;
    if (attempt == 0) {
        return 0;
    }
    const unsigned int shift = std::min(attempt - 1, 5U);
    return std::min(kBaseMs << shift, kMaximumMs);
}

inline bool ReceiptDispatchReady(
    std::uint32_t now,
    std::uint32_t nextAttempt
) {
    return static_cast<std::int32_t>(now - nextAttempt) >= 0;
}

inline bool RememberRpcCommandId(
    std::unordered_set<std::string>& known,
    const std::string& commandId
) {
    return known.insert(commandId).second;
}
