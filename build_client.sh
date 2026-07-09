#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

distrobox enter doom-cpp -- bash -lc "
    set -euo pipefail
    cd '$SCRIPT_DIR'
    x86_64-w64-mingw32-gcc -D_M_AMD64 -O2 \
        -c meathook_interface_c.c -o /tmp/meathook_interface_c.o
    x86_64-w64-mingw32-g++ -D_M_AMD64 -std=c++17 -O2 \
        ap_client_exe.cpp ap_client_path_utils.cpp game_state_probe.cpp mhclient.cpp /tmp/meathook_interface_c.o \
        -o /tmp/ap_client.exe -lrpcrt4 -lbcrypt -lversion \
        -static -static-libgcc -static-libstdc++
    x86_64-w64-mingw32-g++ -std=c++17 -O2 \
        save_death_probe.cpp -o /tmp/save_death_probe.exe \
        -Wl,--subsystem,windows \
        -static -static-libgcc -static-libstdc++
    cp /tmp/ap_client.exe '$SCRIPT_DIR/ap_client.exe'
    cp /tmp/save_death_probe.exe '$SCRIPT_DIR/save_death_probe.exe'
    x86_64-w64-mingw32-strip '$SCRIPT_DIR/ap_client.exe'
    x86_64-w64-mingw32-strip '$SCRIPT_DIR/save_death_probe.exe'
"

file "$SCRIPT_DIR/ap_client.exe"
file "$SCRIPT_DIR/save_death_probe.exe"
