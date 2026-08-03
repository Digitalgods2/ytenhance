# -*- mode: python ; coding: utf-8 -*-

hiddenimports = ["youtube_transcript_api"]
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
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
