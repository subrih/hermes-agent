#!/usr/bin/env bash
# Publish a new web bundle to the iOS live-update channel (no re-sign/reinstall).
# Builds the renderer, zips it, and writes version.json so installed apps pull it
# on next launch (Capgo manual updater → src/platform/live-update.ts).
#
#   bash apps/mobile/publish-ios-update.sh [version]
#
# version defaults to a UTC timestamp. The app compares it to its current bundle.

set -euo pipefail
DESKTOP=/Users/kaveri/kaveri/src/apps/desktop
DIST="$DESKTOP/dist"
CHANNEL_DIR=/Users/kaveri/kaveri/var/ota/a3301684eb130bc7a0e7da61
CHANNEL_URL=https://ota.hellopulse.ai/a3301684eb130bc7a0e7da61
VERSION="${1:-$(date -u +%Y.%m.%d.%H%M%S)}"

echo "==> Building renderer…"
npm --prefix "$DESKTOP" run build >/dev/null

mkdir -p "$CHANNEL_DIR"
ZIP="$CHANNEL_DIR/$VERSION.zip"
echo "==> Zipping bundle ($VERSION)…"
rm -f "$ZIP"
( cd "$DIST" && zip -qr "$ZIP" . )

cat > "$CHANNEL_DIR/version.json" <<EOF
{"version":"$VERSION","url":"$CHANNEL_URL/$VERSION.zip"}
EOF

echo "✓ Published $VERSION  ($(du -h "$ZIP" | cut -f1))"
echo "  $CHANNEL_URL/version.json"
echo "  Installed apps pick it up on next launch."
