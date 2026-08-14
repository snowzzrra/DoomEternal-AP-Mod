#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <string>
#include "../client/ap_runtime_rpc_client.h"

static FILE* g_log = nullptr;

struct LogFileOwner {
    ~LogFileOwner() { if (g_log) fclose(g_log); }
};

static void WriteLog(const char* level, const std::string& message)
{
    SYSTEMTIME now = {};
    GetLocalTime(&now);
    char timestamp[32] = {};
    snprintf(timestamp, sizeof(timestamp), "%02u:%02u:%02u", now.wHour, now.wMinute, now.wSecond);
    printf("[%s][%s] %s\n", timestamp, level, message.c_str());
    if (g_log) { fprintf(g_log, "[%s][%s] %s\n", timestamp, level, message.c_str()); fflush(g_log); }
}

struct Snapshot { std::string checkpoint; std::string spawn; bool has_checkpoint = false; bool has_spawn = false; };

static Snapshot Collect(ApRuntimeRpcClient& rpc)
{
    Snapshot snapshot;
    char checkpoint[512] = {}; int checkpoint_size = sizeof(checkpoint);
    snapshot.has_checkpoint = rpc.Checkpoint(&checkpoint_size, (unsigned char*)checkpoint, sizeof(checkpoint));
    if (snapshot.has_checkpoint) {
        snapshot.checkpoint.assign(checkpoint, checkpoint_size);
        if (!snapshot.checkpoint.empty() && snapshot.checkpoint.back() == '\0') snapshot.checkpoint.pop_back();
    }
    char spawn[512] = {}; int spawn_size = sizeof(spawn);
    snapshot.has_spawn = rpc.Spawn(&spawn_size, (unsigned char*)spawn, sizeof(spawn));
    if (snapshot.has_spawn) {
        snapshot.spawn.assign(spawn, spawn_size);
        if (!snapshot.spawn.empty() && snapshot.spawn.back() == '\0') snapshot.spawn.pop_back();
    }
    return snapshot;
}

static bool Changed(const Snapshot& a, const Snapshot& b)
{
    return a.has_checkpoint != b.has_checkpoint || a.has_spawn != b.has_spawn
        || a.checkpoint != b.checkpoint || a.spawn != b.spawn;
}

int main()
{
    g_log = fopen("base\\ap_logger.log", "a");
    LogFileOwner log_owner;
    WriteLog("INFO ", "=== AP Logger starting ===");
    ApRuntimeRpcClient rpc;
    rpc.SetLogCallback([](const std::string& message) { WriteLog("INFO ", message); });
    const DWORD deadline = GetTickCount() + 120000;
    while (!rpc.PollHealth() && static_cast<LONG>(GetTickCount() - deadline) < 0) Sleep(1000);
    if (!rpc.Ready()) {
        WriteLog("ERROR", "Could not connect to AP runtime RPC.");
        return 1;
    }
    Snapshot previous; int tick = 0;
    bool was_healthy = true;
    for (;;) {
        Sleep(500);
        ++tick;
        const bool healthy = rpc.PollHealth();
        if (!healthy) {
            if (was_healthy) WriteLog("WARN ", "RPC health transitioned to unavailable.");
            was_healthy = false;
            continue;
        }
        if (!was_healthy) WriteLog("INFO ", "RPC health transitioned to ready.");
        was_healthy = true;
        Snapshot current = Collect(rpc);
        const bool changed = Changed(previous, current);
        if (changed || tick % 20 == 0) {
            WriteLog("INFO ", changed ? ">>> State change detected:" : "--- Periodic snapshot:");
            if (current.has_checkpoint) WriteLog("INFO ", "Checkpoint : " + current.checkpoint); else WriteLog("WARN ", "Checkpoint : <unavailable>");
            if (current.has_spawn) WriteLog("INFO ", "SpawnInfo  : " + current.spawn); else WriteLog("WARN ", "SpawnInfo  : <unavailable>");
            previous = current;
        }
    }
}
