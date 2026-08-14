#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RELEASE_ROOT="$(realpath -m "$REPO_ROOT/build/release")"
BUILD_DIR="$(realpath -m "${1:-$RELEASE_ROOT/build/client}")"

case "$BUILD_DIR/" in
    "$RELEASE_ROOT/"*) ;;
    *)
        echo "Native build output must remain under $RELEASE_ROOT" >&2
        exit 1
        ;;
esac

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/generated-rpc"

TOOLCHAIN_LAUNCHER=()
for launcher in distrobox distrobox-host-exec; do
    command -v "$launcher" >/dev/null 2>&1 || continue
    for container in doom-cpp emile-dev-2026; do
        if [ "$launcher" = distrobox ]; then
            candidate=(distrobox enter "$container" --)
        else
            candidate=(distrobox-host-exec distrobox enter "$container" --)
        fi
        if "${candidate[@]}" true >/dev/null 2>&1; then
            TOOLCHAIN_LAUNCHER=("${candidate[@]}")
            break 2
        fi
    done
done
[ "${#TOOLCHAIN_LAUNCHER[@]}" -gt 0 ] || { echo "Neither doom-cpp nor emile-dev-2026 is available through distrobox." >&2; exit 1; }

"${TOOLCHAIN_LAUNCHER[@]}" bash -s -- "$REPO_ROOT" "$BUILD_DIR" <<'BUILD'
set -euo pipefail
REPO_ROOT="$1"
BUILD_DIR="$2"
cd "$REPO_ROOT"
WIDL_BIN=""
if command -v x86_64-w64-mingw32-widl >/dev/null; then
    WIDL_BIN="$(command -v x86_64-w64-mingw32-widl)"
elif command -v widl >/dev/null; then
    WIDL_BIN="$(command -v widl)"
else
    echo "missing tool: x86_64-w64-mingw32-widl or widl" >&2
    exit 1
fi
for tool in x86_64-w64-mingw32-gcc x86_64-w64-mingw32-g++ x86_64-w64-mingw32-strip x86_64-w64-mingw32-objdump file clang; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
SYSROOT="$(x86_64-w64-mingw32-gcc -print-sysroot)"
CLANG_SYSROOT=()
if [ -n "$SYSROOT" ]; then CLANG_SYSROOT=(--sysroot="$SYSROOT"); fi
rm -rf "$BUILD_DIR/generated-rpc"
mkdir -p "$BUILD_DIR/generated-rpc"
(cd "$BUILD_DIR/generated-rpc" && "$WIDL_BIN" --win64 -Oif -h -c -o ap_runtime_rpc "$REPO_ROOT/native/client/ap_runtime_rpc.idl")
test -s "$BUILD_DIR/generated-rpc/ap_runtime_rpc.h"
test -s "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c"
grep -q 'ap_runtime_rpc_v1_0_c_ifspec' "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c"
grep -q '1c9ca7c8' "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c"
last=0
for operation in ap_execute ap_request_entities ap_upload_chunk ap_retrieve_entities ap_retrieve_encounter ap_retrieve_checkpoint ap_retrieve_spawn ap_health; do
    escaped_operation=$(printf '%s' "$operation" | sed 's/[][\\.^$*+?(){}|]/\\&/g')
    line=$(grep -nE "^[[:space:]]*void[[:space:]]+(__cdecl[[:space:]]+)?${escaped_operation}[[:space:]]*\\(" "$BUILD_DIR/generated-rpc/ap_runtime_rpc.h" | cut -d: -f1)
    test -n "$line" && test "$line" -gt "$last"
    last="$line"
done
x86_64-w64-mingw32-gcc -D_M_AMD64 -O2 -I"$BUILD_DIR/generated-rpc" \
    -c "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c" -o "$BUILD_DIR/ap_runtime_rpc_c.o"
clang --target=x86_64-w64-windows-gnu -fms-extensions "${CLANG_SYSROOT[@]}" -O2 \
    -I"$BUILD_DIR/generated-rpc" -c native/client/ap_runtime_rpc_seh.c -o "$BUILD_DIR/ap_runtime_rpc_seh.o"
x86_64-w64-mingw32-g++ -D_M_AMD64 -std=c++17 -O2 -I. \
    native/client/ap_client_exe.cpp native/client/ap_client_path_utils.cpp native/client/game_state_probe.cpp native/client/ap_runtime_rpc_client.cpp \
    "$BUILD_DIR/ap_runtime_rpc_c.o" "$BUILD_DIR/ap_runtime_rpc_seh.o" \
    -o "$BUILD_DIR/ap_client.exe" -lrpcrt4 -lbcrypt -lversion -static -static-libgcc -static-libstdc++
x86_64-w64-mingw32-g++ -std=c++17 -O2 native/probes/save_death_probe.cpp \
    -o "$BUILD_DIR/save_death_probe.exe" -Wl,--subsystem,windows -static -static-libgcc -static-libstdc++
x86_64-w64-mingw32-strip "$BUILD_DIR/ap_client.exe"
x86_64-w64-mingw32-strip "$BUILD_DIR/save_death_probe.exe"
test "$(dd if="$BUILD_DIR/ap_client.exe" bs=2 count=1 2>/dev/null)" = "MZ"
file "$BUILD_DIR/ap_client.exe" | grep -E 'PE32\+.*x86-64'
x86_64-w64-mingw32-objdump -p "$BUILD_DIR/ap_client.exe" | grep -i 'RPCRT4.dll'
rm "$BUILD_DIR/ap_runtime_rpc_c.o" "$BUILD_DIR/ap_runtime_rpc_seh.o"
BUILD
