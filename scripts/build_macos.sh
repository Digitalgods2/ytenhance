#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_VERSION="${YOUTUBE_ENHANCE_APP_VERSION:-1.0.0}"
NOTARY_PROFILE="${YOUTUBE_ENHANCE_NOTARY_PROFILE:-YouTubeEnhance-notary}"
MODE="${1:-release}"
BUILD_ROOT="${YOUTUBE_ENHANCE_BUILD_ROOT:-$HOME/Library/Caches/YouTubeEnhance/build}"
VENV_DIR="$BUILD_ROOT/venv"
DIST_DIR="$REPO_ROOT/dist"
APP_PATH="$DIST_DIR/YouTube Enhance.app"
DMG_PATH="$DIST_DIR/YouTubeEnhance-$APP_VERSION.dmg"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This release script must run on macOS." >&2
    exit 1
fi

if [[ "$MODE" != "release" && "$MODE" != "--build-only" && "$MODE" != "--adhoc-test" ]]; then
    echo "Usage: bash scripts/build_macos.sh [--build-only|--adhoc-test]" >&2
    exit 1
fi

for command_name in codesign ditto hdiutil python3 security shasum xcrun; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Required command is missing: $command_name" >&2
        exit 1
    fi
done

CODESIGN_IDENTITY="${YOUTUBE_ENHANCE_CODESIGN_IDENTITY:-}"
if [[ "$MODE" != "--adhoc-test" && -z "$CODESIGN_IDENTITY" ]]; then
    CODESIGN_IDENTITY="$(
        security find-identity -v -p codesigning \
            | sed -n 's/.*"\(Developer ID Application:.*\)"/\1/p' \
            | head -n 1
    )"
fi
if [[ "$MODE" != "--adhoc-test" && -z "$CODESIGN_IDENTITY" ]]; then
    echo "No Developer ID Application certificate is available in the login Keychain." >&2
    exit 1
fi

if [[ "$MODE" == "release" ]] && ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    echo "The notarytool profile '$NOTARY_PROFILE' is unavailable." >&2
    echo "Create it with: xcrun notarytool store-credentials '$NOTARY_PROFILE'" >&2
    exit 1
fi

mkdir -p "$BUILD_ROOT" "$DIST_DIR"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$REPO_ROOT/requirements.txt"

export YOUTUBE_ENHANCE_APP_VERSION="$APP_VERSION"
if [[ "$MODE" == "--adhoc-test" ]]; then
    unset YOUTUBE_ENHANCE_CODESIGN_IDENTITY
else
    export YOUTUBE_ENHANCE_CODESIGN_IDENTITY="$CODESIGN_IDENTITY"
fi
"$VENV_DIR/bin/python" -m PyInstaller \
    --clean \
    --noconfirm \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_ROOT/pyinstaller" \
    "$REPO_ROOT/youtube_enhance.spec"

if [[ ! -d "$APP_PATH" ]]; then
    echo "PyInstaller did not create $APP_PATH" >&2
    exit 1
fi

codesign --verify --deep --strict --verbose=2 "$APP_PATH"
"$APP_PATH/Contents/MacOS/YouTubeEnhance" --self-test

DMG_STAGE="$(mktemp -d "$BUILD_ROOT/dmg.XXXXXX")"
trap 'rm -rf "$DMG_STAGE"' EXIT
ditto "$APP_PATH" "$DMG_STAGE/YouTube Enhance.app"
ln -s /Applications "$DMG_STAGE/Applications"

hdiutil create \
    -ov \
    -format UDZO \
    -fs HFS+ \
    -volname "YouTube Enhance" \
    -srcfolder "$DMG_STAGE" \
    "$DMG_PATH"

if [[ "$MODE" == "--adhoc-test" ]]; then
    codesign --force --sign - "$DMG_PATH"
else
    codesign --force --sign "$CODESIGN_IDENTITY" --options runtime --timestamp "$DMG_PATH"
fi
codesign --verify --strict --verbose=2 "$DMG_PATH"
if [[ "$MODE" == "release" ]]; then
    xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG_PATH"
    xcrun stapler validate "$DMG_PATH"
    spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG_PATH"
elif [[ "$MODE" == "--build-only" ]]; then
    echo "Build-only mode: notarization and stapling were skipped."
else
    echo "Ad-hoc test mode: Developer ID signing, notarization, and stapling were skipped."
fi

echo "Release artifact: $DMG_PATH"
shasum -a 256 "$DMG_PATH"
