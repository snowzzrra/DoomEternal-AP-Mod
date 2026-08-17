#include <windows.h>
#include <rpc.h>
#include "ap_runtime_rpc.h"
#include "ap_runtime_rpc_seh.h"

#if defined(__MINGW32__) || defined(__MINGW64__)
int RPC_ENTRY RpcExceptionFilter(unsigned long ExceptionCode);
#endif

void* __RPC_USER MIDL_user_allocate(size_t size)
{
    return HeapAlloc(GetProcessHeap(), 0, size);
}

void __RPC_USER MIDL_user_free(void* pointer)
{
    if (pointer != NULL) {
        HeapFree(GetProcessHeap(), 0, pointer);
    }
}

void ApRpcSetImplicitBinding(RPC_BINDING_HANDLE binding)
{
    ap_runtime_rpc__MIDL_AutoBindHandle = binding;
}

void ApRpcClearImplicitBinding(void)
{
    ap_runtime_rpc__MIDL_AutoBindHandle = NULL;
}

RPC_STATUS ApRpcExecute(RPC_BINDING_HANDLE binding, unsigned char* command)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    ApRpcSetImplicitBinding(binding);
    RpcTryExcept {
        ap_execute(command);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    ApRpcClearImplicitBinding();
    return status;
}

RPC_STATUS ApRpcRequestEntities(
    RPC_BINDING_HANDLE binding, unsigned char* path, unsigned char begin, int size)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    ApRpcSetImplicitBinding(binding);
    RpcTryExcept {
        ap_request_entities(path, begin, size);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    ApRpcClearImplicitBinding();
    return status;
}

RPC_STATUS ApRpcRetrieveEntities(
    RPC_BINDING_HANDLE binding, int* size, unsigned char* data)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    ApRpcSetImplicitBinding(binding);
    RpcTryExcept {
        ap_retrieve_entities(size, data);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    ApRpcClearImplicitBinding();
    return status;
}

RPC_STATUS ApRpcCheckpoint(RPC_BINDING_HANDLE binding, int* size, unsigned char* data)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    ApRpcSetImplicitBinding(binding);
    RpcTryExcept {
        ap_retrieve_checkpoint(size, data);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    ApRpcClearImplicitBinding();
    return status;
}

RPC_STATUS ApRpcSpawn(RPC_BINDING_HANDLE binding, int* size, unsigned char* data)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    ApRpcSetImplicitBinding(binding);
    RpcTryExcept {
        ap_retrieve_spawn(size, data);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    ApRpcClearImplicitBinding();
    return status;
}

RPC_STATUS ApRpcHealth(RPC_BINDING_HANDLE binding, int* state)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    ApRpcSetImplicitBinding(binding);
    RpcTryExcept {
        ap_health(state);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    ApRpcClearImplicitBinding();
    return status;
}
