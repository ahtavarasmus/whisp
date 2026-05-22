#!/usr/bin/env bash
# Whisp .app launcher
# Lives at Whisp.app/Contents/MacOS/Whisp and is invoked by Launch Services
# when the user opens the bundle.
set -u

# Locate uv binary - search common install paths first, then PATH.
UV=""
for c in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
    if [ -x "$c" ]; then UV="$c"; break; fi
done
if [ -z "$UV" ] && command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
fi
if [ -z "$UV" ]; then
    osascript -e 'display alert "Whisp cannot start" message "The uv runtime was not found.\n\nInstall it with:\n  brew install uv\n\nThen reopen Whisp." as critical' >/dev/null 2>&1
    exit 1
fi

# Resolve paths. Resources/whisp.py is the bundled "factory" script;
# we copy it to user space so self-update can overwrite it.
HERE="$(cd "$(dirname "$0")" && pwd)"
BUNDLED_SCRIPT="$HERE/../Resources/whisp.py"
SUPPORT_DIR="$HOME/Library/Application Support/Whisp"
USER_SCRIPT="$SUPPORT_DIR/whisp.py"
LOG_DIR="$HOME/Library/Logs"
LOG_FILE="$LOG_DIR/whisp.log"

mkdir -p "$SUPPORT_DIR" "$LOG_DIR"

# Install (or refresh-on-version-bump) the bundled script.
# We compare via the embedded __VERSION__ stamp: if the bundle ships a newer
# stamp than the user copy, refresh. The user's own self-updates still win
# between bundle upgrades because their stamp is also newer than the bundle.
extract_version() {
    grep -m1 '^__VERSION__ = ' "$1" 2>/dev/null | sed -E 's/^__VERSION__ = "(.*)"$/\1/' || true
}

if [ ! -f "$USER_SCRIPT" ]; then
    cp "$BUNDLED_SCRIPT" "$USER_SCRIPT"
fi
chmod +x "$USER_SCRIPT" 2>/dev/null || true

cd "$SUPPORT_DIR"
exec "$UV" run --script "$USER_SCRIPT" "$@" >>"$LOG_FILE" 2>&1
