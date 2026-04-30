# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile GUI bundle for Windows (PyQt6 + Playwright)."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


APP_NAME = "AntidetectUI"

block_cipher = None

playwright_datas = collect_data_files("playwright", include_py_files=False)
hidden = collect_submodules("playwright")

# patchright is optional (may be absent in the build environment)
try:
    patchright_datas = collect_data_files("patchright", include_py_files=False)
    hidden += collect_submodules("patchright")
except Exception:
    patchright_datas = []

a = Analysis(
    ["src/qt_main.py"],
    pathex=["src"],
    binaries=[],
    datas=[*playwright_datas, *patchright_datas],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
