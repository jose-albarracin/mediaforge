#!/bin/sh
# Regenerate the Heimdall app icons from branding/heimdall.svg.
#
# Needs: rsvg-convert (brew install librsvg), Pillow in the app's venv,
# and iconutil (macOS only, for the .icns). Run from the repo root:
#   sh tools/build_icons.sh
set -eu

SRC=branding/heimdall.svg
OUT=branding
PY=${PYTHON:-.venv/bin/python}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$OUT"

# Window / dock icon used by the app at runtime.
rsvg-convert -w 512 -h 512 "$SRC" -o "$OUT/heimdall.png"

# Windows .ico with the standard sizes.
for s in 16 24 32 48 64 128 256; do
  rsvg-convert -w $s -h $s "$SRC" -o "$TMP/$s.png"
done
"$PY" - "$TMP" "$OUT/heimdall.ico" <<'EOF'
import sys
from PIL import Image
tmp, dest = sys.argv[1], sys.argv[2]
sizes = [16, 24, 32, 48, 64, 128, 256]
base = Image.open(f"{tmp}/256.png")
base.save(dest, sizes=[(s, s) for s in sizes],
          append_images=[Image.open(f"{tmp}/{s}.png") for s in sizes[:-1]])
EOF

# macOS .icns (for a future .app bundle).
if command -v iconutil >/dev/null 2>&1; then
  SET="$TMP/heimdall.iconset"
  mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    rsvg-convert -w $s -h $s "$SRC" -o "$SET/icon_${s}x${s}.png"
    rsvg-convert -w $((s * 2)) -h $((s * 2)) "$SRC" -o "$SET/icon_${s}x${s}@2x.png"
  done
  iconutil -c icns "$SET" -o "$OUT/heimdall.icns"
fi

ls -l "$OUT"
