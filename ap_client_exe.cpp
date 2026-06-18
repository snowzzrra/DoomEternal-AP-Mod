#include <windows.h>
#include <stdio.h>
#include <string>
#include "mhclient.h"

MeathookInterface* g_MhInterface = nullptr;

void LogDebug(const char* msg) {
    printf("%s\n", msg);
    FILE* f = fopen("base\\ap_client.log", "a");
    if (f) {
        fprintf(f, "%s\n", msg);
        fclose(f);
    }
}

int main() {
    LogDebug("Starting AP Client EXE...");
    g_MhInterface = new MeathookInterface();
    
    LogDebug("Waiting for Meathook RPC to initialize...");
    while (!g_MhInterface || !g_MhInterface->m_Initialized) {
        Sleep(100);
    }
    
    LogDebug("Connected to Meathook RPC!");

    const char* queueFile = "base\\ap_queue.txt";

    while (true) {
        Sleep(500); 

        FILE* f = fopen(queueFile, "r");
        if (!f) continue;

        char buffer[1024];
        if (fgets(buffer, sizeof(buffer), f) != NULL) {
            fclose(f);
            
            for(int i=0; i<sizeof(buffer); i++) {
                if (buffer[i] == '\n' || buffer[i] == '\r') {
                    buffer[i] = '\0';
                    break;
                }
            }

            if (buffer[0] != '\0') {
                LogDebug((std::string("Processing command from queue: ") + buffer).c_str());
                if (strncmp(buffer, "#DUMP_ENTITIES", 14) == 0) {
                    LogDebug("Action: #DUMP_ENTITIES triggered.");
                    size_t bufferSize = 4 * 1024 * 1024;
                    LogDebug("Allocating 4MB memory buffer...");
                    unsigned char* pBuffer = (unsigned char*)malloc(bufferSize);
                    if (pBuffer) {
                        LogDebug("Memory allocated successfully. Calling GetEntitiesFile via RPC...");
                        size_t actualSize = 0;
                        if (g_MhInterface->GetEntitiesFile(pBuffer, &actualSize)) {
                            LogDebug((std::string("RPC Success! Fetched ") + std::to_string(actualSize) + " bytes.").c_str());
                            FILE* dumpFile = fopen("base\\map.entities", "wb");
                            if (dumpFile) {
                                fwrite(pBuffer, 1, actualSize, dumpFile);
                                fclose(dumpFile);
                                LogDebug("SUCCESS: Entities written to base\\map.entities!");
                            } else {
                                LogDebug("ERROR: Failed to open base\\map.entities for writing.");
                            }
                        } else {
                            LogDebug("ERROR: GetEntitiesFile RPC call returned false!");
                        }
                        free(pBuffer);
                    } else {
                        LogDebug("ERROR: Out of memory trying to allocate 4MB.");
                    }
                } else if (strncmp(buffer, "#PUSH_ENTITIES ", 15) == 0) {
                    char* filePath = buffer + 15;
                    LogDebug((std::string("Action: #PUSH_ENTITIES triggered. Target path: ") + filePath).c_str());
                    LogDebug("Calling PushEntitiesFile via RPC...");
                    if (g_MhInterface->PushEntitiesFile(filePath, NULL, 0)) {
                        LogDebug("SUCCESS: PushEntitiesFile RPC call completed successfully!");
                    } else {
                        LogDebug("ERROR: PushEntitiesFile RPC call returned false!");
                    }
                } else {
                    LogDebug((std::string("Action: Standard Console Command -> ") + buffer).c_str());
                    if (g_MhInterface->ExecuteConsoleCommand((unsigned char*)buffer)) {
                        LogDebug("SUCCESS: ExecuteConsoleCommand completed.");
                    } else {
                        LogDebug("ERROR: ExecuteConsoleCommand failed.");
                    }
                }

                LogDebug("Clearing command from queue file...");
                f = fopen(queueFile, "w");
                if (f) fclose(f);
                LogDebug("Queue cleared. Waiting for next command.");
            }
        } else {
            fclose(f);
        }
    }
    LogDebug("Application exiting (reached end of main).");
    system("pause");
    return 0;
}
