#!/bin/bash
# Build a native, self-contained AstroPaperDigest.app from source.
# Usage: ./build_app.sh
# Requires: macOS and Python 3.9+.

set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"

VERSION="$(tr -d '[:space:]' < version.txt)"
case "$VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "ERROR: invalid version '$VERSION' in version.txt (expected x.y.z)." >&2; exit 1 ;;
esac

APP_NAME="AstroPaperDigest"
BUNDLE_ID="com.arxivdailydigest.app"
APP_DIR="$APP_ROOT/../$APP_NAME.app"
BUILD_ROOT="$APP_ROOT/build"
CLI_DIST="$BUILD_ROOT/app-cli-dist"
CLI_WORK="$BUILD_ROOT/pyi-app-cli"
APP_DIST="$BUILD_ROOT/app-dist"
APP_WORK="$BUILD_ROOT/pyi-app"

echo "Building native $APP_NAME.app v$VERSION..."

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Python 3.9+ first." >&2
    exit 1
fi

if [ ! -x ".venv/bin/python3" ] || ! .venv/bin/python3 -c "import sys; assert sys.prefix != sys.base_prefix" 2>/dev/null; then
    rm -rf .venv
    python3 -m venv .venv
fi

PYTHON_BIN="$APP_ROOT/.venv/bin/python3"
echo "Installing/updating Python dependencies..."
"$PYTHON_BIN" -m pip install -r requirements.txt -q

if ! "$PYTHON_BIN" -c "import PyInstaller" 2>/dev/null; then
    echo "Installing PyInstaller..."
    "$PYTHON_BIN" -m pip install pyinstaller -q
fi

# Build the CLI as a sibling executable embedded in the GUI bundle. The GUI
# launches this native executable in frozen mode, so no external Python
# process or shell launcher owns the desktop window.
rm -rf "$CLI_DIST" "$CLI_WORK" "$APP_DIST" "$APP_WORK" "$APP_DIR"
mkdir -p "$CLI_DIST" "$APP_DIST"

echo "Building embedded CLI..."
"$PYTHON_BIN" -m PyInstaller \
    --noconfirm --clean --console \
    --name apd-cli \
    --paths "$APP_ROOT" \
    --distpath "$CLI_DIST" \
    --workpath "$CLI_WORK" \
    --specpath "$BUILD_ROOT" \
    "$APP_ROOT/cli_entry.py"

echo "Building native GUI bundle..."
"$PYTHON_BIN" -m PyInstaller \
    --noconfirm --clean --windowed \
    --name "$APP_NAME" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --icon "$APP_ROOT/assets/AppIcon.icns" \
    --paths "$APP_ROOT" \
    --collect-all webview \
    --add-data "$APP_ROOT/static:static" \
    --add-binary "$CLI_DIST/apd-cli:." \
    --distpath "$APP_DIST" \
    --workpath "$APP_WORK" \
    --specpath "$BUILD_ROOT" \
    "$APP_ROOT/src/gui.py"

# PyInstaller creates the native bundle and icon metadata. Keep the version
# in sync with version.txt and ad-hoc sign the complete bundle so Launch
# Services treats it as one coherent macOS application.
PLIST="$APP_DIST/$APP_NAME.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"

codesign --force --deep -s - "$APP_DIST/$APP_NAME.app"
cp -R "$APP_DIST/$APP_NAME.app" "$APP_DIR"

echo ""
echo "Done! Created native app: $APP_DIR"
echo "Executable: Mach-O bundle with identifier $BUNDLE_ID"
