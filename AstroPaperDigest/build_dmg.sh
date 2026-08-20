#!/bin/bash
# Build the self-contained AstroPaperDigest.app + dmg (dmg channel).
#
# Produces:
#   dist/AstroPaperDigest.app            self-contained app (no Python required)
#   dist/AstroPaperDigest-<v>.dmg        drag-to-install disk image
#   dist/AstroPaperDigest-v<v>.app.zip   update package (whole-bundle replace)
#
# Usage: ./build_dmg.sh
#   PYTHON=... ./build_dmg.sh        override the python that has PyInstaller
#
# Requires: macOS, a python3 with PyInstaller (e.g. pip install pyinstaller).
set -euo pipefail
cd "$(dirname "$0")"
APP_ROOT="$(pwd)"

VERSION="$(cat version.txt 2>/dev/null | tr -d '[:space:]')"
if [ -z "$VERSION" ]; then echo "ERROR: version.txt missing or empty." >&2; exit 1; fi

PYTHON="${PYTHON:-python3}"
APP_NAME="AstroPaperDigest"
BUNDLE_ID="com.arxivdailydigest.app"

echo "==> Building bundled CLI (apd-cli) ..."
rm -rf build/cli dist
mkdir -p build dist
"$PYTHON" -m PyInstaller --noconfirm --clean --console \
  --name apd-cli \
  --paths "$APP_ROOT" \
  --distpath "$APP_ROOT/build/cli" --workpath "$APP_ROOT/build/pyi-cli" --specpath "$APP_ROOT/build" \
  "$APP_ROOT/cli_entry.py" > "$APP_ROOT/build/cli-build.log" 2>&1

echo "==> Building self-contained .app ..."
"$PYTHON" -m PyInstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --icon "$APP_ROOT/assets/AppIcon.icns" \
  --paths "$APP_ROOT" \
  --collect-all webview \
  --add-binary "$APP_ROOT/build/cli/apd-cli:." \
  --distpath "$APP_ROOT/dist" --workpath "$APP_ROOT/build/pyi-app" --specpath "$APP_ROOT/build" \
  "$APP_ROOT/src/gui.py" > "$APP_ROOT/build/app-build.log" 2>&1

echo "==> Setting bundle version in Info.plist ..."
PLIST="$APP_ROOT/dist/$APP_NAME.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"

echo "==> Ad-hoc signing the .app (no Developer ID required) ..."
codesign --force --deep -s - "$APP_ROOT/dist/$APP_NAME.app"

echo "==> Creating dmg ..."
rm -rf "$APP_ROOT/build/dmgroot"
mkdir -p "$APP_ROOT/build/dmgroot"
cp -R "$APP_ROOT/dist/$APP_NAME.app" "$APP_ROOT/build/dmgroot/"
ln -s /Applications "$APP_ROOT/build/dmgroot/Applications"
DMG="$APP_ROOT/dist/$APP_NAME-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$APP_ROOT/build/dmgroot" -ov -format UDZO "$DMG" > "$APP_ROOT/build/dmg.log" 2>&1
codesign -s - "$DMG" 2>/dev/null || true

echo "==> Creating update package (.app zip) ..."
APPZIP="$APP_ROOT/dist/$APP_NAME-v$VERSION.app.zip"
rm -f "$APPZIP"
ditto -c -k --keepParent "$APP_ROOT/dist/$APP_NAME.app" "$APPZIP"

echo ""
echo "Done:"
echo "  dist/$APP_NAME.app"
echo "  $DMG"
echo "  $APPZIP"
