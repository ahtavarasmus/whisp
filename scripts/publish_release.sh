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
    ls -la dist/ >&2 || true
    exit 1
fi

AUTH="Authorization: token ${FORGEJO_TOKEN}"

# Delete any existing "latest" release (and its tag). Ignore 404.
echo "Looking up existing 'latest' release..."
http=$(curl -sS -o /tmp/r.json -w "%{http_code}" -H "$AUTH" \
    "$API/releases/tags/latest" || echo "000")
echo "  GET releases/tags/latest -> HTTP $http"
if [ "$http" = "200" ]; then
    rid=$(python3 -c 'import json; print(json.load(open("/tmp/r.json"))["id"])')
    echo "  deleting old release id=$rid"
    curl -sS -X DELETE -H "$AUTH" "$API/releases/$rid" || true
fi
# Delete the tag too (separate from the release).
curl -sS -X DELETE -H "$AUTH" "$API/tags/latest" >/dev/null 2>&1 || true

# Build the JSON payload using a here-doc fed to python via stdin.
# Avoids quoting hell entirely.
echo "Creating release 'latest' at $SHORT"
python3 - <<PY > /tmp/payload.json
import json, os
sha = os.environ["SHA"]
short = sha[:7]
print(json.dumps({
    "tag_name": "latest",
    "target_commitish": sha,
    "name": f"latest ({short})",
    "body": f"Auto-built from {sha}.\n\nDownload Whisp.app.zip below, unzip, right-click -> Open the first time.",
    "draft": False,
    "prerelease": False,
}))
PY

http=$(curl -sS -o /tmp/release.json -w "%{http_code}" \
    -X POST -H "$AUTH" -H "Content-Type: application/json" \
    --data-binary @/tmp/payload.json \
    "$API/releases")
echo "  POST releases -> HTTP $http"
if [ "$http" != "201" ]; then
    echo "Release creation failed. Response:" >&2
    cat /tmp/release.json >&2
    exit 1
fi
rid=$(python3 -c 'import json; print(json.load(open("/tmp/release.json"))["id"])')
echo "  release id=$rid"

# Upload the asset.
echo "Uploading $ZIP"
http=$(curl -sS -o /tmp/asset.json -w "%{http_code}" \
    -X POST -H "$AUTH" \
    -F "attachment=@${ZIP};filename=Whisp.app.zip" \
    "$API/releases/${rid}/assets")
echo "  POST asset -> HTTP $http"
if [ "$http" != "201" ]; then
    echo "Asset upload failed. Response:" >&2
    cat /tmp/asset.json >&2
    exit 1
fi

echo
echo "Done. Download URL:"
echo "  https://${FORGEJO_HOST}/${REPO_OWNER}/${REPO_NAME}/releases/download/latest/Whisp.app.zip"
