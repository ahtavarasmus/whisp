#!/usr/bin/env bash
# Assemble Whisp.app — works on macOS and Linux (no compilation needed).
# Usage: scripts/build_app.sh [VERSION_STAMP]
# VERSION_STAMP defaults to `git rev-parse HEAD` or "dev".
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    if VERSION=$(git rev-parse HEAD 2>/dev/null); then :; else VERSION="dev"; fi
fi

OUT="dist"
APP="$OUT/Whisp.app"

# Clean previous build without `rm -rf` (project rule).
if [ -d "$OUT" ]; then
    if command -v trash >/dev/null 2>&1; then trash "$OUT" || true; else rm -r "$OUT"; fi
fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Info.plist with version substituted.
sed "s/__VERSION__/$VERSION/g" app/Info.plist > "$APP/Contents/Info.plist"

# Launcher.
install -m 0755 app/launcher.sh "$APP/Contents/MacOS/Whisp"

# Stamp the script with the build version and copy it into Resources.
# Anchor the pattern so we only rewrite the stamp line — the same literal
# also appears inside apply_update_and_restart() and must NOT be substituted,
# or self-update breaks.
sed 's|^__VERSION__ = "__VERSION_PLACEHOLDER__"$|__VERSION__ = "'"$VERSION"'"|' \
    whisp.py > "$APP/Contents/Resources/whisp.py"
chmod 0644 "$APP/Contents/Resources/whisp.py"

# Sanity check: the stamp must have been substituted exactly once.
if ! grep -q "^__VERSION__ = \"$VERSION\"$" "$APP/Contents/Resources/whisp.py"; then
    echo "build_app.sh: version stamp substitution failed" >&2
    exit 1
fi
if grep -q "__VERSION_PLACEHOLDER__" "$APP/Contents/Resources/whisp.py"; then
    : # placeholder still present in apply_update path - that's intentional
fi

# Optional icon
if [ -f app/AppIcon.icns ]; then
    cp app/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
fi

# Zip it for distribution.
( cd "$OUT" && zip -qr "Whisp-${VERSION:0:7}.zip" Whisp.app )
ln -sf "Whisp-${VERSION:0:7}.zip" "$OUT/Whisp-latest.zip"

echo "Built:"
echo "  $APP"
echo "  $OUT/Whisp-${VERSION:0:7}.zip"
echo "Version stamp: $VERSION"
