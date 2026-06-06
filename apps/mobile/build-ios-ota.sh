#!/usr/bin/env bash
# Build + sign + export an ad-hoc (OTA) IPA of the Kaveri iOS app.
# RUN THIS IN Terminal.app (your logged-in session) so codesign can reach your
# keychain and Apple Developer account — that's what a headless run can't do.
#
#   bash /Users/kaveri/kaveri/src/apps/mobile/build-ios-ota.sh
#
# Output: /Users/kaveri/kaveri-ios-export/  (App.ipa + manifest.plist)
# Then tell Claude "done" and it hosts them on ota.hellopulse.ai.

set -euo pipefail
export PATH="/opt/homebrew/bin:$PATH"

APPDIR=/Users/kaveri/kaveri/src/apps/mobile/ios/App
ARCH="$APPDIR/build/App.xcarchive"
EXPORT=/Users/kaveri/kaveri-ios-export
TEAM=M2ATZ7QRW4

cd "$APPDIR"

echo "==> Refreshing web bundle…"
npm --prefix /Users/kaveri/kaveri/src/apps/desktop run build >/dev/null
( cd /Users/kaveri/kaveri/src/apps/mobile && npx cap sync ios >/dev/null )

echo "==> Archiving UNSIGNED (we sign ad-hoc with the Distribution cert at export)…"
rm -rf "$ARCH" "$EXPORT"
xcodebuild archive \
  -project App.xcodeproj -scheme App -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath "$ARCH" -derivedDataPath build \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" CODE_SIGN_ENTITLEMENTS=""

echo "==> Exporting ad-hoc IPA + OTA manifest (signs with Apple Distribution / $TEAM)…"
xcodebuild -exportArchive \
  -archivePath "$ARCH" \
  -exportOptionsPlist ../exportOptions.plist \
  -exportPath "$EXPORT" \
  -allowProvisioningUpdates

echo
echo "✓ Export complete:"
ls -la "$EXPORT"
echo
echo "Now tell Claude 'done' (export is at $EXPORT) and it will host the OTA install."
