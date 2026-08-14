#include <windows.h>
#include <rpc.h>
#include "ap_runtime_rpc.h"
#include "ap_runtime_rpc_seh.h"

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

RPC_STATUS ApRpcExecute(RPC_BINDING_HANDLE binding, unsigned char* command)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    RpcTryExcept {
        ap_execute(binding, command);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    return status;
}

RPC_STATUS ApRpcRequestEntities(
    RPC_BINDING_HANDLE binding, unsigned char* path, unsigned char begin, int size)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    RpcTryExcept {
        ap_request_entities(binding, path, begin, size);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    return status;
}

RPC_STATUS ApRpcRetrieveEntities(
    RPC_BINDING_HANDLE binding, int* size, unsigned char* data)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    RpcTryExcept {
        ap_retrieve_entities(binding, size, data);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    return status;
}

RPC_STATUS ApRpcCheckpoint(RPC_BINDING_HANDLE binding, int* size, unsigned char* data)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    RpcTryExcept {
        ap_retrieve_checkpoint(binding, size, data);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    return status;
}

RPC_STATUS ApRpcSpawn(RPC_BINDING_HANDLE binding, int* size, unsigned char* data)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    RpcTryExcept {
        ap_retrieve_spawn(binding, size, data);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    return status;
}

RPC_STATUS ApRpcHealth(RPC_BINDING_HANDLE binding, int* state)
{
    RPC_STATUS status = RPC_S_CALL_FAILED;
    RpcTryExcept {
        ap_health(binding, state);
        status = RPC_S_OK;
    }
    RpcExcept(RpcExceptionFilter(RpcExceptionCode())) {
        status = (RPC_STATUS)RpcExceptionCode();
    }
    RpcEndExcept
    return status;
}
