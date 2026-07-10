#include <stdlib.h>
#include <stdio.h>
#include <ctype.h>
#include "meathook_interface.h" 
#include <windows.h>
#include "mhclient.h"
#include <sstream>

static RpcCallResult ClassifyWaitNamedPipeFailure(DWORD error)
{
    switch (error) {
    case ERROR_FILE_NOT_FOUND:
        return PIPE_NOT_FOUND;
    case ERROR_PIPE_BUSY:
        return PIPE_BUSY;
    case ERROR_SEM_TIMEOUT:
        return WAIT_NAMED_PIPE_TIMEOUT;
    default:
        return UNKNOWN_TRANSPORT_ERROR;
    }
}

MeathookInterface::MeathookInterface()
{
    InitializeCriticalSection(&m_RpcMutex);
    m_RpcMutexInitialized = true;
    StartKeepAliveThread();
}

MeathookInterface::~MeathookInterface()
{
    if (m_RpcMutexInitialized) {
        DeleteCriticalSection(&m_RpcMutex);
        m_RpcMutexInitialized = false;
    }
}

void MeathookInterface::LogRpc(const std::string& message)
{
    if (m_LogCallback) {
        m_LogCallback(message);
    } else {
        printf("%s\n", message.c_str());
    }
}

DWORD MeathookInterface::LastSuccessfulKeepAliveAgeMs() const
{
    if (m_LastSuccessfulKeepAliveTick == 0) {
        return 0xFFFFFFFF;
    }
    return GetTickCount() - m_LastSuccessfulKeepAliveTick;
}

void MeathookInterface::MarkBindingInvalid()
{
    m_Initialized = false;
    InterlockedIncrement(&m_BindingGeneration);
}

bool MeathookInterface::EnterRpcCall(
    const char* operation,
    unsigned long long& callId,
    DWORD& waitMs
) {
    const DWORD waitStart = GetTickCount();
    EnterCriticalSection(&m_RpcMutex);
    waitMs = GetTickCount() - waitStart;
    callId = ++m_RpcCallSequence;
    std::ostringstream message;
    message
        << "RPC_CALL_START"
        << " rpc_call_id=" << callId
        << " command_id=" << m_CurrentCommandId
        << " thread_id=" << GetCurrentThreadId()
        << " operation=" << operation
        << " mutex_wait_ms=" << waitMs
        << " start_tick_ms=" << GetTickCount()
        << " last_successful_keepalive_age_ms=" << LastSuccessfulKeepAliveAgeMs()
        << " binding_generation=" << m_BindingGeneration;
    LogRpc(message.str());
    return true;
}

void MeathookInterface::LeaveRpcCall(
    const char* operation,
    unsigned long long callId,
    DWORD waitMs,
    DWORD startTick
) {
    std::ostringstream message;
    message
        << "RPC_CALL_END"
        << " rpc_call_id=" << callId
        << " command_id=" << m_CurrentCommandId
        << " thread_id=" << GetCurrentThreadId()
        << " operation=" << operation
        << " mutex_wait_ms=" << waitMs
        << " duration_ms=" << (GetTickCount() - startTick)
        << " last_successful_keepalive_age_ms=" << LastSuccessfulKeepAliveAgeMs()
        << " binding_generation=" << m_BindingGeneration
        << " result=" << m_LastRpcCallResult;
    LogRpc(message.str());
    LeaveCriticalSection(&m_RpcMutex);
}

// struct idMat3 {
//     idVec3			mat[3];
// };
// 
// class idAngles
// {
// public:
//     float			pitch;
//     float			yaw;
//     float			roll;
//     idMat3 ToMat3() const;
// };
// 
// #define DEG2RAD(a)				( (a) * idMath::M_DEG2RAD )
// 
// idMat3 idAngles::ToMat3() const
// {
//     idMat3 mat;
//     float sr, sp, sy, cr, cp, cy;
// 
//     idMath::SinCos(DEG2RAD(yaw), sy, cy);
//     idMath::SinCos(DEG2RAD(pitch), sp, cp);
//     idMath::SinCos(DEG2RAD(roll), sr, cr);
// 
//     mat.mat[0].Set(cp * cy, cp * sy, -sp);
//     mat.mat[1].Set(sr * sp * cy + cr * -sy, sr * sp * sy + cr * cy, sr * cp);
//     mat.mat[2].Set(cr * sp * cy + -sr * -sy, cr * sp * sy + -sr * cy, cr * cp);
//     return mat;
// }

DWORD WINAPI MeathookInterface::KeepAlive(LPVOID Data)
{
    MeathookInterface *pthis = (MeathookInterface*)Data;
    pthis->m_Initialized = false;
    Sleep(2000);
    while (1) {

        unsigned long long callId = 0;
        DWORD waitMs = 0;
        pthis->EnterRpcCall("KeepAlive", callId, waitMs);
        const DWORD startTick = GetTickCount();

        if (pthis->m_Initialized == false) {
            pthis->InitializeRpcInterface();
        }

        if (!WaitNamedPipeA("\\\\.\\pipe\\meathook_interface_rpc", 100)) {
            pthis->m_LastTransportError = GetLastError();
            pthis->m_LastRpcCallResult = ClassifyWaitNamedPipeFailure(
                pthis->m_LastTransportError
            );
            pthis->MarkBindingInvalid();
            pthis->LeaveRpcCall("KeepAlive", callId, waitMs, startTick);
            Sleep(1000);
            continue;
        }

        try {

        {
            
            int x;
            ::KeepAlive(meathook_interface_v1_0_c_ifspec, &x);
            pthis->m_Initialized = true;
            pthis->m_LastSuccessfulKeepAliveTick = GetTickCount();
            pthis->m_LastTransportError = ERROR_SUCCESS;
            pthis->m_LastRpcCallResult = RPC_CALL_DELIVERED;
        }
        } catch(...) {
        {
            int ulCode = 1;
            printf("Runtime reported exception 0x%lx = %ld\n", ulCode, ulCode);
            pthis->MarkBindingInvalid();
            pthis->m_LastTransportError = ERROR_GEN_FAILURE;
            pthis->m_LastRpcCallResult = RPC_EXCEPTION;
        }
        }
        pthis->LeaveRpcCall("KeepAlive", callId, waitMs, startTick);
        if (pthis->m_LastRpcCallResult == RPC_EXCEPTION) {
            Sleep(2000);
        }
        Sleep(5000);
    }

    return 0;
}

void MeathookInterface::StartKeepAliveThread()
{
    m_UnInitialized = CreateEvent(NULL, true, false, "MHThreadEvent");
    CreateThread(NULL, 0, MeathookInterface::KeepAlive, this, 0, &m_ThreadId);
}

bool MeathookInterface::GetSpawnInfo(unsigned char* pBuffer)
{
    try {
    {
        if (pBuffer != nullptr) {
            int Size = (int)sizeof(m_SpawnInfoBuffer);
            ::GetSpawnInfo(meathook_interface_v1_0_c_ifspec, &Size, (unsigned char*) m_SpawnInfoBuffer);
            strcpy_s((char*)pBuffer, 256, m_SpawnInfoBuffer);
            m_Initialized = true;

        } else {
            int Size = 0;
            ::GetSpawnInfo(meathook_interface_v1_0_c_ifspec, &Size, 0);
        }
        return true;
    }
    } catch(...) {
    {
        int ulCode = 1;
        printf("Runtime reported exception 0x%lx = %ld\n", ulCode, ulCode);
        m_Initialized = false;
        return false;
    }
    }
    return false;
}
bool MeathookInterface::GetEntitiesFile(unsigned char* pBuffer, size_t *Size)
{
    try {
    {
        int TempSize = (int)*Size;
        ::GetEntitiesFile(meathook_interface_v1_0_c_ifspec, &TempSize, pBuffer);
        *Size = TempSize;
        return true;
    }
    } catch(...) {
    {
        int ulCode = 1;
        printf("Runtime reported exception 0x%lx = %ld\n", ulCode, ulCode);
        return false;
    }
    }
    return false;
}

bool MeathookInterface::PushEntitiesFile(char* pFileName, char *pBuffer, int Size)
{
    unsigned long long callId = 0;
    DWORD waitMs = 0;
    EnterRpcCall("PushEntitiesFile", callId, waitMs);
    const DWORD startTick = GetTickCount();
    if (!WaitNamedPipeA("\\\\.\\pipe\\meathook_interface_rpc", 100)) {
        m_LastTransportError = GetLastError();
        m_LastRpcCallResult = ClassifyWaitNamedPipeFailure(m_LastTransportError);
        MarkBindingInvalid();
        LeaveRpcCall("PushEntitiesFile", callId, waitMs, startTick);
        return false;
    }
    try {

    {
        //MaxSize =  4194296 (0x3FFFF8) RPC interface hangs with anything above this size.
        ::PushEntitiesFile(meathook_interface_v1_0_c_ifspec, (unsigned char*)pFileName, true, 0);
        // int TotalSize = Size;
        // int ChunkSize = 500000;
        // int Offset = 0;
        // while (TotalSize > 0) {
        //     ::UploadData(meathook_interface_v1_0_c_ifspec, ChunkSize, Offset, (unsigned char*)(pBuffer + Offset));
        //     TotalSize -= ChunkSize;
        //     Offset += ChunkSize;
        // }
        // 
        // ::PushEntitiesFile(meathook_interface_v1_0_c_ifspec, (unsigned char*)pFileName, false, Size);
        m_LastTransportError = ERROR_SUCCESS;
        m_LastRpcCallResult = RPC_CALL_DELIVERED;
        LeaveRpcCall("PushEntitiesFile", callId, waitMs, startTick);
        return true;
    }
    } catch(...) {
    {
        int ulCode = 1;
        printf("Runtime reported exception 0x%lx = %ld\n", ulCode, ulCode);
        m_LastTransportError = ERROR_GEN_FAILURE;
        m_LastRpcCallResult = RPC_EXCEPTION;
        MarkBindingInvalid();
        LeaveRpcCall("PushEntitiesFile", callId, waitMs, startTick);
        return false;
    }
    }
    return false;
}

bool MeathookInterface::ExecuteConsoleCommand(unsigned char* pszString)
{
    unsigned long long callId = 0;
    DWORD waitMs = 0;
    EnterRpcCall("ExecuteConsoleCommand", callId, waitMs);
    const DWORD startTick = GetTickCount();
    if (!WaitNamedPipeA("\\\\.\\pipe\\meathook_interface_rpc", 100)) {
        m_LastTransportError = GetLastError();
        m_LastRpcCallResult = ClassifyWaitNamedPipeFailure(m_LastTransportError);
        MarkBindingInvalid();
        LeaveRpcCall("ExecuteConsoleCommand", callId, waitMs, startTick);
        return false;
    }
    try {

    {
        ::ExecuteConsoleCommand(meathook_interface_v1_0_c_ifspec, pszString);
        m_LastTransportError = ERROR_SUCCESS;
        m_LastRpcCallResult = RPC_CALL_DELIVERED;
        LeaveRpcCall("ExecuteConsoleCommand", callId, waitMs, startTick);
        return true;
    }
    } catch(...) {
    {
        int ulCode = 1;
        printf("Runtime reported exception 0x%lx = %ld\n", ulCode, ulCode);
        m_LastTransportError = ERROR_GEN_FAILURE;
        m_LastRpcCallResult = RPC_EXCEPTION;
        MarkBindingInvalid();
        LeaveRpcCall("ExecuteConsoleCommand", callId, waitMs, startTick);
        return false;
    }
    }
    return false;
}

bool MeathookInterface::DestroyRpcInterface() {
    int status = RpcStringFreeA(&pszStringBinding);
    if (status != 0) {
        return status;
    }

    status = RpcBindingFree(&meathook_interface_v1_0_c_ifspec);
    if (status != 0) {
        return status;
    }

    return 0;
}

bool MeathookInterface::GetActiveEncounter(int *Size, char* pBuffer)
{
    try {
    {
        ::GetActiveEncounter(meathook_interface_v1_0_c_ifspec, Size, (unsigned char*)pBuffer);
        return true;
    }
    } catch(...) {
    {
        int ulCode = 1;
        printf("Runtime reported exception 0x%lx = %ld\n", ulCode, ulCode);
        return false;
    }
    }

    return false;
}

bool MeathookInterface::GetCurrentCheckpoint(int* Size, char* pBuffer)
{
    try {
    {
        ::GetCurrentCheckpoint(meathook_interface_v1_0_c_ifspec, Size, (unsigned char*)pBuffer);
        return true;
    }
    } catch(...) {
    {
        int ulCode = 1;
        printf("Runtime reported exception 0x%lx = %ld\n", ulCode, ulCode);
        return false;
    }
    }

    return false;
}


bool MeathookInterface::InitializeRpcInterface()
{
    RPC_STATUS status;
    unsigned char* pszUuid = NULL;
    const char* pszProtocolSequence = "ncacn_np";
    unsigned char* pszNetworkAddress = NULL;
    const char* pszEndpoint = "\\pipe\\meathook_interface_rpc";
    unsigned char* pszOptions = NULL;

    status = RpcStringBindingComposeA(
        pszUuid,
        (unsigned char*)pszProtocolSequence,
        pszNetworkAddress,
        (unsigned char*)pszEndpoint,
        pszOptions,
        &pszStringBinding
        );

    if (status != 0) {
        return status;
    }

    status = RpcBindingFromStringBindingA(pszStringBinding, &meathook_interface_v1_0_c_ifspec);
    if (status != 0) {
        return status;
    }

    m_Initialized = true;
    return 0;
}

/******************************************************/
/*         MIDL allocate and free                     */
/******************************************************/

void __RPC_FAR* __RPC_USER midl_user_allocate(size_t len)
{
    return(malloc(len));
}

void __RPC_USER midl_user_free(void __RPC_FAR* ptr)
{
    free(ptr);
}
