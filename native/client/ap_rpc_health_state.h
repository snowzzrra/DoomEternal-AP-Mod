#pragma once

#include <filesystem>

class ApRpcHealthStatePublisher {
public:
    static constexpr unsigned long kFreshnessMs = 3000;

    explicit ApRpcHealthStatePublisher(std::filesystem::path base_directory);
    ~ApRpcHealthStatePublisher();

    ApRpcHealthStatePublisher(const ApRpcHealthStatePublisher&) = delete;
    ApRpcHealthStatePublisher& operator=(const ApRpcHealthStatePublisher&) = delete;

    void PublishStarting();
    void PublishHealth(bool available, int result_code, unsigned long transport_status);
    void PublishStopped();
    void PublishEffectBaseline(bool ready, unsigned long long attachment_epoch);

private:
    std::filesystem::path path_;
    std::filesystem::path baseline_path_;
    unsigned long long sequence_ = 0;
    unsigned long long last_timestamp_ms_ = 0;
    const char* state_ = nullptr;
    bool stopped_ = false;
    bool baseline_ready_ = false;
    unsigned long long baseline_epoch_ = 0;
    unsigned long long baseline_timestamp_ms_ = 0;

    void Publish(const char* state, int result_code, unsigned long transport_status, bool force);
};
