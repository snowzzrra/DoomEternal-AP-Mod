#include <windows.h>
#include <stdio.h>
#include <string>
#include <ctime>
#include "mhclient.h"

// -----------------------------------------------------------------------
// Logging helpers
// -----------------------------------------------------------------------

static FILE* g_LogFile = nullptr;

void OpenLog() {
    g_LogFile = fopen("base\\ap_logger.log", "a");
}

void Log(const char* level, const char* msg) {
    time_t now = time(nullptr);
    char ts[32];
    strftime(ts, sizeof(ts), "%H:%M:%S", localtime(&now));
    printf("[%s][%s] %s\n", ts, level, msg);
    if (g_LogFile) {
        fprintf(g_LogFile, "[%s][%s] %s\n", ts, level, msg);
        fflush(g_LogFile);
    }
}

void LogInfo(const char* msg)  { Log("INFO ", msg); }
void LogWarn(const char* msg)  { Log("WARN ", msg); }
void LogError(const char* msg) { Log("ERROR", msg); }

// -----------------------------------------------------------------------
// Probe: attempt a real call to confirm that the RPC is functional.
// m_Initialized only says that the constructor connected to the named pipe.
// It does not guarantee that the game is ready to receive calls.
// Use SEH (__try/__except) to catch access violations without crashing.
// -----------------------------------------------------------------------

bool ProbeRPC(MeathookInterface* mh) {
    try {
        char buf[256] = {0};
        int sz = sizeof(buf);
        // GetCurrentCheckpoint is safe outside levels: just returns false/empty
        mh->GetCurrentCheckpoint(&sz, buf);
        return true;   // completed without structured exception — RPC is alive
    } catch(...) {
        return false;  // access violation or similar — not ready yet
    }
}

// -----------------------------------------------------------------------
// Retry connection. Rebuild MeathookInterface on each attempt because the
// constructor can leave invalid state behind when RPC is unavailable.
// -----------------------------------------------------------------------

MeathookInterface* ConnectToMeathook(int timeout_seconds) {
    LogInfo("Waiting for Meathook RPC to become ready...");
    LogInfo("(This is normal — waiting for game to fully load the DLL)");

    const int interval_ms = 1000;
    const int max_ms = timeout_seconds * 1000;
    int elapsed = 0;

    while (elapsed < max_ms) {
        Sleep(interval_ms);
        elapsed += interval_ms;

        MeathookInterface* mh = new MeathookInterface();

        if (mh->m_Initialized && ProbeRPC(mh)) {
            LogInfo("Meathook RPC is ready and probe call succeeded!");
            return mh;
        }

        // The server is not ready yet. Discard this instance and retry.
        delete mh;

        if (elapsed % 5000 == 0) {
            char buf[64];
            snprintf(buf, sizeof(buf), "Still waiting... (%ds elapsed)", elapsed / 1000);
            LogInfo(buf);
        }
    }

    LogError("Meathook did not become ready within timeout.");
    return nullptr;
}

// -----------------------------------------------------------------------
// Game-state snapshot
// -----------------------------------------------------------------------

struct GameSnapshot {
    char checkpoint[512];
    char spawnInfo[512];
    bool hasCheckpoint;
    bool hasSpawnInfo;
};

GameSnapshot CollectSnapshot(MeathookInterface* mh) {
    GameSnapshot snap = {};
    try {
        int sz = sizeof(snap.checkpoint);
        snap.hasCheckpoint = mh->GetCurrentCheckpoint(&sz, snap.checkpoint);
    } catch(...) {
        snap.hasCheckpoint = false;
        strncpy(snap.checkpoint, "<exception>", sizeof(snap.checkpoint));
    }
    try {
        snap.hasSpawnInfo = mh->GetSpawnInfo((unsigned char*)snap.spawnInfo);
    } catch(...) {
        snap.hasSpawnInfo = false;
        strncpy(snap.spawnInfo, "<exception>", sizeof(snap.spawnInfo));
    }
    return snap;
}

void LogSnapshot(const GameSnapshot& snap) {
    if (snap.hasCheckpoint) {
        LogInfo((std::string("Checkpoint : ") + snap.checkpoint).c_str());
    } else {
        LogWarn("Checkpoint : <unavailable — menu, loading, or not in level>");
    }
    if (snap.hasSpawnInfo) {
        LogInfo((std::string("SpawnInfo  : ") + snap.spawnInfo).c_str());
    } else {
        LogWarn("SpawnInfo  : <unavailable>");
    }
}

bool SnapshotChanged(const GameSnapshot& prev, const GameSnapshot& curr) {
    if (prev.hasCheckpoint != curr.hasCheckpoint) return true;
    if (prev.hasSpawnInfo  != curr.hasSpawnInfo)  return true;
    if (prev.hasCheckpoint && strcmp(prev.checkpoint, curr.checkpoint) != 0) return true;
    if (prev.hasSpawnInfo  && strcmp(prev.spawnInfo,  curr.spawnInfo)  != 0) return true;
    return false;
}

// -----------------------------------------------------------------------
// Main loop
// -----------------------------------------------------------------------

void RunLogger(MeathookInterface* mh) {
    LogInfo("Logger running. Polling game state...");
    LogInfo("--------------------------------------------------");

    GameSnapshot prev = {};
    int tick = 0;
    const int FORCED_LOG_INTERVAL = 20;  // forced log every 10s (20 x 500ms)

    while (true) {
        Sleep(500);
        tick++;

        GameSnapshot curr = CollectSnapshot(mh);
        bool changed   = SnapshotChanged(prev, curr);
        bool forcedLog = (tick % FORCED_LOG_INTERVAL == 0);

        if (changed || forcedLog) {
            if (changed) LogInfo(">>> State change detected:");
            else         LogInfo("--- Periodic snapshot:");
            LogSnapshot(curr);
            LogInfo("--------------------------------------------------");
            prev = curr;
        }
    }
}

// -----------------------------------------------------------------------
// Entry point
// -----------------------------------------------------------------------

int main() {
    OpenLog();
    LogInfo("=== AP Logger starting ===");

    MeathookInterface* mh = ConnectToMeathook(120);  // 2 minutos de timeout

    if (!mh) {
        LogError("Could not connect to Meathook. Is xinput1_3.dll in the game folder?");
        LogError("Is the game running with the Meathook DLL loaded?");
        if (g_LogFile) fclose(g_LogFile);
        system("pause");
        return 1;
    }

    RunLogger(mh);

    delete mh;
    if (g_LogFile) fclose(g_LogFile);
    return 0;
}
