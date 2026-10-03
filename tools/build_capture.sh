#!/bin/sh
# Compile the macOS screen/audio capture helper used by "Grabar clase".
# Needs Xcode Command Line Tools (xcode-select --install) and macOS 13+.
#   sh tools/build_capture.sh   ->  bin/heimdall-capture
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$ROOT/bin"
swiftc -O -parse-as-library -target "$(uname -m)-apple-macos13.0" \
  -o "$ROOT/bin/heimdall-capture" "$ROOT/capture/heimdall_capture.swift"
codesign --force --sign - "$ROOT/bin/heimdall-capture" >/dev/null 2>&1 || true
echo "Listo: $ROOT/bin/heimdall-capture"
