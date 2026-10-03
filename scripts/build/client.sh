#!/bin/bash
# Proton consumes the supported Windows x64 MSVC Game Link.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RELEASE_ROOT="$(realpath -m "$REPO_ROOT/build/release")"
BUILD_DIR="$(realpath -m "${1:-$RELEASE_ROOT/build/client}")"
SOURCE_DIR="${DOOMEAP_WINDOWS_CLIENT_DIR:-$RELEASE_ROOT/build/client}"
case "$BUILD_DIR/" in "$RELEASE_ROOT/"*) ;; *) echo "Native output outside release root" >&2; exit 1;; esac
for name in ap_client.exe save_death_probe.exe; do
 test -s "$SOURCE_DIR/$name" || { echo "Build with scripts/build/client_windows.ps1 and set DOOMEAP_WINDOWS_CLIENT_DIR." >&2; exit 1; }
done
mkdir -p "$BUILD_DIR"
for name in ap_client.exe save_death_probe.exe; do
 if [ "$(realpath "$SOURCE_DIR/$name")" != "$(realpath -m "$BUILD_DIR/$name")" ]; then cp "$SOURCE_DIR/$name" "$BUILD_DIR/$name"; fi
done
echo "NATIVE_CLIENT windows-msvc runtime=Proton output=$BUILD_DIR"
