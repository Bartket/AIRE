# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the AI Race Engineer desktop app.

Build with:  pyinstaller AIRE.spec --noconfirm
"""

from PyInstaller.utils.hooks import collect_submodules

# uvicorn and pywebview resolve these by name at runtime, so static analysis
# does not see them and they must be requested explicitly.
hiddenimports = [
    *collect_submodules("uvicorn"),
    *collect_submodules("webview"),
    "anyio",
    "click",
    "h11",
    "httptools",
    "websockets",
    "watchfiles",
    "pystray._win32",
    "PIL._tkinter_finder",
]

# Windows-only extras: absent on other platforms, so keep them optional.
for name in ("irsdk", "pycaw", "comtypes", "psutil", "pygame"):
    try:
        __import__(name)
    except ImportError:
        continue
    hiddenimports.append(name)

a = Analysis(
    ["run_desktop.py"],
    pathex=[],
    binaries=[],
    # The web UI is served from disk at runtime, so it must ship with the app.
    datas=[("ai_race_engineer/static", "static")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim the heavyweight libraries we never import.
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AIRE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # console=False keeps the launcher from flashing a terminal on start.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="ai_race_engineer/static/icon.ico",
    version="file_version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AIRE",
)
