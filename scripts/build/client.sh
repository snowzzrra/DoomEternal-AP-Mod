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
mkdir -p "$BUILD_DIR"

distrobox enter doom-cpp -- bash -lc "
    set -euo pipefail
    cd '$REPO_ROOT'
    x86_64-w64-mingw32-gcc -D_M_AMD64 -O2 \
        -c native/client/meathook_interface_c.c -o '$BUILD_DIR/meathook_interface_c.o'
    x86_64-w64-mingw32-g++ -D_M_AMD64 -std=c++17 -O2 -I. \
        native/client/ap_client_exe.cpp native/client/ap_client_path_utils.cpp native/client/game_state_probe.cpp native/client/mhclient.cpp '$BUILD_DIR/meathook_interface_c.o' \
        -o '$BUILD_DIR/ap_client.exe' -lrpcrt4 -lbcrypt -lversion \
        -static -static-libgcc -static-libstdc++
    x86_64-w64-mingw32-g++ -std=c++17 -O2 \
        native/probes/save_death_probe.cpp -o '$BUILD_DIR/save_death_probe.exe' \
        -Wl,--subsystem,windows \
        -static -static-libgcc -static-libstdc++
    x86_64-w64-mingw32-strip '$BUILD_DIR/ap_client.exe'
    x86_64-w64-mingw32-strip '$BUILD_DIR/save_death_probe.exe'
    rm '$BUILD_DIR/meathook_interface_c.o'
"

file "$BUILD_DIR/ap_client.exe"
file "$BUILD_DIR/save_death_probe.exe"
