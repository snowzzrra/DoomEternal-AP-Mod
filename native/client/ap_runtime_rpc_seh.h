#pragma once

#include <rpc.h>

#ifdef __cplusplus
extern "C" {
#endif

RPC_STATUS ApRpcExecute(RPC_BINDING_HANDLE binding, unsigned char* command);
RPC_STATUS ApRpcRequestEntities(
    RPC_BINDING_HANDLE binding, unsigned char* path, unsigned char begin, int size);
RPC_STATUS ApRpcRetrieveEntities(
    RPC_BINDING_HANDLE binding, int* size, unsigned char* data);
RPC_STATUS ApRpcCheckpoint(
    RPC_BINDING_HANDLE binding, int* size, unsigned char* data);
RPC_STATUS ApRpcSpawn(
    RPC_BINDING_HANDLE binding, int* size, unsigned char* data);
RPC_STATUS ApRpcHealth(RPC_BINDING_HANDLE binding, int* state);

#ifdef __cplusplus
}
#endif
