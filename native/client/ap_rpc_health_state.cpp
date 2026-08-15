#include "ap_rpc_health_state.h"

#include <windows.h>
#include <io.h>

#include <chrono>
#include <cstdio>
#include <string>
#include <utility>

namespace {

const char* kHealthStateFile = "ap_rpc_health.state";
const size_t kDiagnosticLimit = 64;

unsigned long long UnixTimestampMs() {
    return static_cast<unsigned long long>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
}

std::string Bounded(std::string value) {
    for (char& character : value) {
        if (character == '\r' || character == '\n' || character == '=') {
            character = '_';
        }
    }
    if (value.size() > kDiagnosticLimit) {
        value.resize(kDiagnosticLimit);
    }
    return value;
}

const char* ResultName(int result_code) {
    switch (result_code) {
    case 0: return "none";
    case 1: return "pipe_missing";
    case 2: return "pipe_busy";
    case 3: return "wait_timeout";
    case 4: return "delivered";
    case 5: return "exception";
    case 6: return "unknown";
    default: return "unsupported";
    }
}

std::string TransportName(unsigned long status) {
    switch (status) {
    case ERROR_SUCCESS: return "ok";
    case ERROR_FILE_NOT_FOUND: return "pipe_missing";
    case ERROR_PIPE_BUSY: return "pipe_busy";
    case ERROR_SEM_TIMEOUT: return "wait_timeout";
    default: return "status_" + std::to_string(status);
    }
}

bool WriteAtomically(const std::filesystem::path& path, const std::string& contents) {
    std::filesystem::path temporary = path;
    temporary += ".tmp";
    FILE* output = fopen(temporary.string().c_str(), "wb");
    if (!output) {
        return false;
    }
    const size_t written = fwrite(contents.data(), 1, contents.size(), output);
    const bool flushed = fflush(output) == 0;
    const int handle = _fileno(output);
    if (handle >= 0) {
        _commit(handle);
    }
    fclose(output);
    if (written != contents.size() || !flushed) {
        DeleteFileA(temporary.string().c_str());
        return false;
    }
    if (!MoveFileExA(
            temporary.string().c_str(),
            path.string().c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        DeleteFileA(temporary.string().c_str());
        return false;
    }
    return true;
}

}  // namespace

ApRpcHealthStatePublisher::ApRpcHealthStatePublisher(std::filesystem::path base_directory)
    : path_(std::move(base_directory) / kHealthStateFile) {}

ApRpcHealthStatePublisher::~ApRpcHealthStatePublisher() {
    PublishStopped();
}

void ApRpcHealthStatePublisher::PublishStarting() {
    Publish("starting", 0, ERROR_SUCCESS, true);
}

void ApRpcHealthStatePublisher::PublishHealth(
    bool available,
    int result_code,
    unsigned long transport_status
) {
    Publish(available ? "ready" : "unavailable", result_code, transport_status, false);
}

void ApRpcHealthStatePublisher::PublishStopped() {
    if (!stopped_) {
        Publish("stopped", 0, ERROR_SUCCESS, true);
        stopped_ = true;
    }
}

void ApRpcHealthStatePublisher::Publish(
    const char* state,
    int result_code,
    unsigned long transport_status,
    bool force
) {
    const unsigned long long now = UnixTimestampMs();
    if (!force && state_ != nullptr && std::string(state_) == state
            && now < last_timestamp_ms_ + kFreshnessMs / 3) {
        return;
    }

    const unsigned long long sequence = sequence_ + 1;
    const std::string contents =
        std::string("schema=1\nstate=") + state + "\n"
        "pid=" + std::to_string(GetCurrentProcessId()) + "\n"
        "timestamp_ms=" + std::to_string(now) + "\n"
        "freshness_ms=" + std::to_string(kFreshnessMs) + "\n"
        "sequence=" + std::to_string(sequence) + "\n"
        "result=" + Bounded(ResultName(result_code)) + "\n"
        "result_code=" + std::to_string(result_code) + "\n"
        "transport=" + Bounded(TransportName(transport_status)) + "\n"
        "transport_status=" + std::to_string(transport_status) + "\n";
    if (!WriteAtomically(path_, contents)) {
        return;
    }
    sequence_ = sequence;
    last_timestamp_ms_ = now;
    state_ = state;
}
