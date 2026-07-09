#pragma once
#include <stdlib.h>
#include <stdio.h>
#include <ctype.h>
#include "meathook_interface.h" 
#include <windows.h>

enum RpcCallResult
{
    RPC_CALL_RESULT_NONE = 0,
    PIPE_NOT_FOUND,
    PIPE_BUSY,
    WAIT_NAMED_PIPE_TIMEOUT,
    RPC_CALL_DELIVERED,
    RPC_EXCEPTION,
    UNKNOWN_TRANSPORT_ERROR,
};

class MeathookInterface
{
    HANDLE m_UnInitialized;
    char m_SpawnInfoBuffer[MAX_PATH];
    void StartKeepAliveThread();
    DWORD m_ThreadId;
    unsigned char* pszStringBinding = NULL;

public:
    bool m_Initialized;
    RpcCallResult m_LastRpcCallResult = RPC_CALL_RESULT_NONE;
    DWORD m_LastTransportError = ERROR_SUCCESS;
    MeathookInterface() { StartKeepAliveThread(); }
    ~MeathookInterface() {}
    bool DestroyRpcInterface();
    bool InitializeRpcInterface();

    bool ExecuteConsoleCommand(unsigned char* pszString);
    bool PushEntitiesFile(char* pFileName, char* pBuffer, int Size);
    bool GetSpawnInfo(unsigned char* pBuffer);
    bool GetEntitiesFile(unsigned char* pBuffer, size_t* Size);
    bool GetActiveEncounter(int* Size, char* pBuffer);
    bool GetCurrentCheckpoint(int* Size, char* pBuffer);

    static DWORD WINAPI KeepAlive(LPVOID Data);
};
