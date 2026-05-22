#!/usr/bin/env bash
# Publish dist/Whisp-<short>.zip as the "latest" release on Forgejo.
# Idempotent: deletes any prior "latest" release/tag, recreates with new asset.
#
# Required env:
#   FORGEJO_TOKEN  - access token with repo write
#   FORGEJO_HOST   - e.g. git.retardhub.com
#   REPO_OWNER     - e.g. ahtavarasmus
#   REPO_NAME      - e.g. whisp
#   SHA            - full commit sha
set -euo pipefail

: "${FORGEJO_TOKEN:?}"
: "${FORGEJO_HOST:?}"
: "${REPO_OWNER:?}"
: "${REPO_NAME:?}"
: "${SHA:?}"

API="https://${FORGEJO_HOST}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}"
SHORT="${SHA:0:7}"
ZIP="dist/Whisp-${SHORT}.zip"

if [ ! -f "$ZIP" ]; then
    echo "Build artifact not found: $ZIP" >&2
    exit 1
fi

auth=(-H "Authorization: token ${FORGEJO_TOKEN}")

# Delete any existing "latest" release (and its tag). Ignore 404.
echo "Looking up existing 'latest' release…"
existing=$(curl -fsS "${auth[@]}" "$API/releases/tags/latest" || true)
if [ -n "$existing" ] && [ "$existing" != "" ]; then
    rid=$(printf '%s' "$existing" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)
    if [ -n "$rid" ]; then
        echo "Deleting old release id=$rid"
        curl -fsS -X DELETE "${auth[@]}" "$API/releases/$rid" || true
    fi
fi
# Tag deletion is separate.
curl -fsS -X DELETE "${auth[@]}" "$API/tags/latest" >/dev/null 2>&1 || true

# Create the new release pointing at this commit.
echo "Creating release 'latest' at $SHORT"
payload=$(python3 -c '
import json, os
print(json.dumps({
    "tag_name": "latest",
    "target_commitish": os.environ["SHA"],
    "name": f"latest ({os.environ[\"SHA\"][:7]})",
    "body": f"Auto-built from {os.environ[\"SHA\"]}.\n\nDownload Whisp.app.zip below, unzip, right-click -> Open the first time.",
    "draft": False,
    "prerelease": False
}))
')
release=$(curl -fsS -X POST "${auth[@]}" -H "Content-Type: application/json" \
    -d "$payload" "$API/releases")
rid=$(printf '%s' "$release" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

# Upload the asset.
echo "Uploading $ZIP to release id=$rid"
curl -fsS -X POST "${auth[@]}" \
    -F "attachment=@${ZIP};filename=Whisp.app.zip" \
    "$API/releases/${rid}/assets" >/dev/null

echo "Done. Download URL:"
echo "  https://${FORGEJO_HOST}/${REPO_OWNER}/${REPO_NAME}/releases/download/latest/Whisp.app.zip"
