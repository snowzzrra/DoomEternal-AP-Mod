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

TOOLCHAIN_LAUNCHER=()
_has_direct_toolchain=true
for _tool in x86_64-w64-mingw32-gcc x86_64-w64-mingw32-g++ clang; do
    if ! command -v "$_tool" >/dev/null 2>&1; then
        _has_direct_toolchain=false
        break
    fi
done
if ! command -v x86_64-w64-mingw32-widl >/dev/null 2>&1 && ! command -v widl >/dev/null 2>&1; then
    _has_direct_toolchain=false
fi

if [ "$_has_direct_toolchain" = false ]; then
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
    [ "${#TOOLCHAIN_LAUNCHER[@]}" -gt 0 ] || { echo "Direct MinGW/Clang toolchain not found and neither doom-cpp nor emile-dev-2026 is available through distrobox." >&2; exit 1; }
fi

CACHE_ROOT="${AP_BUILD_CACHE_ROOT:-$REPO_ROOT/.cache/ap-build}"
TOOLCHAIN_ID="$("${TOOLCHAIN_LAUNCHER[@]}" bash -s <<'IDENTITY'
set -euo pipefail
for tool in x86_64-w64-mingw32-gcc x86_64-w64-mingw32-g++ x86_64-w64-mingw32-widl widl x86_64-w64-mingw32-strip x86_64-w64-mingw32-objdump clang; do
    if command -v "$tool" >/dev/null 2>&1; then
        printf 'tool=%s path=%s\n' "$tool" "$(command -v "$tool")"
        "$tool" --version 2>&1 || true
    fi
done
if command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
    printf 'sysroot=%s\n' "$(x86_64-w64-mingw32-gcc -print-sysroot)"
fi
IDENTITY
)"
CACHE_CONFIG="$(python3 -c 'import json,sys; print(json.dumps({"toolchain": sys.argv[1], "flags": "rpc=widl --win64 -Oif -h -c; cxx=-std=c++17 -O2 -static; strip=x86_64-w64-mingw32-strip"}))' "$TOOLCHAIN_ID")"
CACHE_KEY="$(PYTHONPATH="$REPO_ROOT" python3 -m tools.release.build_cache key \
    --kind native-client --root "$REPO_ROOT" \
    --input native/client --input native/probes/save_death_probe.cpp \
    --input scripts/build/client.sh --config "$CACHE_CONFIG")"

rm -rf "$BUILD_DIR"
if PYTHONPATH="$REPO_ROOT" python3 -m tools.release.build_cache restore \
    --cache-root "$CACHE_ROOT" --kind native-client --key "$CACHE_KEY" \
    --output-root "$BUILD_DIR" --output ap_client.exe --output save_death_probe.exe; then
    echo "NATIVE_CLIENT cache=hit key=$CACHE_KEY"
    exit 0
else
    echo "NATIVE_CLIENT cache=miss reason=missing-or-invalid-entry key=$CACHE_KEY"
fi

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
for tool in x86_64-w64-mingw32-gcc x86_64-w64-mingw32-g++ x86_64-w64-mingw32-strip x86_64-w64-mingw32-objdump clang; do
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
grep -q 'implicit_handle(handle_t ap_runtime_rpc__MIDL_AutoBindHandle)' "$REPO_ROOT/native/client/ap_runtime_rpc.idl"
! grep -q 'explicit_handle' "$REPO_ROOT/native/client/ap_runtime_rpc.idl"
grep -q 'ap_runtime_rpc_v1_0_c_ifspec' "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c"
grep -q '1c9ca7c8' "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c"
python3 - "$BUILD_DIR/generated-rpc/ap_runtime_rpc.h" "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c" <<'PY'
import re
import sys
from pathlib import Path

header = Path(sys.argv[1]).read_text(encoding="utf-8")
client = Path(sys.argv[2]).read_text(encoding="utf-8")
expected = [
    ("ap_execute", r"unsigned char \*command"),
    ("ap_request_entities", r"unsigned char \*path, boolean begin, int size"),
    ("ap_upload_chunk", r"int size, int offset, unsigned char \*data"),
    ("ap_retrieve_entities", r"int \*size, unsigned char \*data"),
    ("ap_retrieve_encounter", r"int \*size, unsigned char \*data"),
    ("ap_retrieve_checkpoint", r"int \*size, unsigned char \*data"),
    ("ap_retrieve_spawn", r"int \*size, unsigned char \*data"),
    ("ap_health", r"int \*state"),
]

declarations = re.findall(
    r"void\s+(?:__cdecl\s+)?(ap_[a-z_]+)\s*\((.*?)\);", header, re.S
)
if [name for name, _ in declarations] != [name for name, _ in expected]:
    raise SystemExit("generated RPC declaration order/opnums changed")
for (name, params), (_, expected_params) in zip(declarations, expected):
    normalized = " ".join(params.split())
    if "handle_t" in normalized or not re.fullmatch(expected_params, normalized):
        raise SystemExit(f"generated RPC ABI signature mismatch: {name}: {normalized}")
if "extern handle_t ap_runtime_rpc__MIDL_AutoBindHandle;" not in header:
    raise SystemExit("generated RPC header missing implicit binding handle")

definitions = re.findall(
    r"void\s+__cdecl\s+(ap_[a-z_]+)\s*\((.*?)\)\s*\{(.*?)\n\}", client, re.S
)
if [name for name, _, _ in definitions] != [name for name, _ in expected]:
    raise SystemExit("generated RPC procedure order/opnums changed")
offsets = []
for name, _, body in definitions:
    match = re.search(r"__MIDL_ProcFormatString\.Format\[(\d+)\]", body)
    if match is None or "binding" in body:
        raise SystemExit(f"generated RPC procedure ABI mismatch: {name}")
    offsets.append(int(match.group(1)))
if offsets[0] != 0 or offsets != sorted(offsets) or client.count("NdrClientCall2") != 8:
    raise SystemExit("generated RPC procedure formats do not cover opnums 0..7")
PY
x86_64-w64-mingw32-gcc -D_M_AMD64 -O2 -I"$BUILD_DIR/generated-rpc" \
    -c "$BUILD_DIR/generated-rpc/ap_runtime_rpc_c.c" -o "$BUILD_DIR/ap_runtime_rpc_c.o"
clang --target=x86_64-w64-windows-gnu -fms-extensions "${CLANG_SYSROOT[@]}" -O2 \
    -I"$BUILD_DIR/generated-rpc" -c native/client/ap_runtime_rpc_seh.c -o "$BUILD_DIR/ap_runtime_rpc_seh.o"
x86_64-w64-mingw32-g++ -D_M_AMD64 -std=c++17 -O2 -I. \
    native/client/ap_client_exe.cpp native/client/ap_client_path_utils.cpp native/client/game_state_probe.cpp native/client/ap_runtime_rpc_client.cpp native/client/ap_rpc_health_state.cpp native/client/ammo_hotkey.cpp \
    "$BUILD_DIR/ap_runtime_rpc_c.o" "$BUILD_DIR/ap_runtime_rpc_seh.o" \
    -o "$BUILD_DIR/ap_client.exe" -lrpcrt4 -lbcrypt -lversion -luser32 -static -static-libgcc -static-libstdc++
x86_64-w64-mingw32-g++ -std=c++17 -O2 native/probes/save_death_probe.cpp \
    -o "$BUILD_DIR/save_death_probe.exe" -Wl,--subsystem,windows -static -static-libgcc -static-libstdc++
x86_64-w64-mingw32-strip "$BUILD_DIR/ap_client.exe"
x86_64-w64-mingw32-strip "$BUILD_DIR/save_death_probe.exe"
test "$(dd if="$BUILD_DIR/ap_client.exe" bs=2 count=1 2>/dev/null)" = "MZ"
x86_64-w64-mingw32-objdump -f "$BUILD_DIR/ap_client.exe" | grep -E 'file format pei-x86-64'
x86_64-w64-mingw32-objdump -p "$BUILD_DIR/ap_client.exe" | grep -i 'RPCRT4.dll'
rm "$BUILD_DIR/ap_runtime_rpc_c.o" "$BUILD_DIR/ap_runtime_rpc_seh.o"
BUILD

PYTHONPATH="$REPO_ROOT" python3 -m tools.release.build_cache publish \
    --cache-root "$CACHE_ROOT" --kind native-client --key "$CACHE_KEY" \
    --output-root "$BUILD_DIR" --output ap_client.exe --output save_death_probe.exe
