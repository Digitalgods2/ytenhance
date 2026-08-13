# -*- mode: python ; coding: utf-8 -*-

import os
import sys

hiddenimports = ["youtube_transcript_api", "keyring"]
prompt_data = [
    ("create_video_titles", "create_video_titles"),
    ("create_video_summary", "create_video_summary"),
    ("create_video_chapters", "create_video_chapters"),
]

a = Analysis(
    ["youtube_enhance.py"],
    pathex=[],
    binaries=[],
    datas=prompt_data,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

codesign_identity = (os.environ.get("YOUTUBE_ENHANCE_CODESIGN_IDENTITY") or None) if sys.platform == "darwin" else None
app_version = os.environ.get("YOUTUBE_ENHANCE_APP_VERSION", "1.0.0")
macos_min_version = os.environ.get("YOUTUBE_ENHANCE_MACOS_MIN_VERSION", "26.0")

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="YouTubeEnhance",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=codesign_identity,
        entitlements_file=None,
    )
    collected = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="YouTubeEnhance",
    )
    app = BUNDLE(
        collected,
        name="YouTube Enhance.app",
        icon=None,
        bundle_identifier="com.digitalgods.youtubeenhance",
        version=app_version,
        info_plist={
            "CFBundleDisplayName": "YouTube Enhance",
            "CFBundleName": "YouTube Enhance",
            "LSMinimumSystemVersion": macos_min_version,
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="YouTubeEnhance",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
    )
