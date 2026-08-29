# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller 6.x one-file spec for discord-audio-pipe (Windows x64).
#
# Target stack: Python 3.12, PyInstaller 6.22.x, PyQt6 6.11 (PyQt6-Qt6 6.11.2),
# sounddevice 0.5.6, discord.py[voice] 2.7.1.
#
# NOTES ON THE PORT FROM THE OLD PyInstaller 5.1 SPEC
# ---------------------------------------------------
# * The old spec stripped unused Qt5 DLLs with `a.binaries = a.binaries - TOC([...])`.
#   Since PyInstaller 5.11 the Analysis TOC members are plain `list` objects and the
#   `TOC` class is deprecated, so that subtraction no longer works. Filtering is now
#   done with ordinary list comprehensions over the (dest_name, src_name, typecode)
#   tuples.
# * `cipher` / `block_cipher` (bytecode encryption), `a.zipped_data`, `a.zipfiles`,
#   `win_no_prefer_redirects` and `win_private_assemblies` were all removed in
#   PyInstaller 6.0 and are gone from this spec.
# * The old spec referenced `os` and `SPECPATH` without importing `os`, and passed the
#   entry script and hook directory as CWD-relative paths. Everything here is derived
#   from `SPECPATH` with an explicit `import os`, so the spec builds identically no
#   matter which directory `pyinstaller` is invoked from.

import os

from PyInstaller.utils.hooks import collect_dynamic_libs

# Repository root (this spec lives in <repo>/build/).
DATAPATH = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# ---------------------------------------------------------------------------
# davey (discord.py 2.7 DAVE/E2EE voice extension)
# ---------------------------------------------------------------------------
# discord.py[voice] >= 2.7 pulls in `davey`, a compiled C extension that implements
# the DAVE protocol Discord has required for voice since March 2026. The old spec had
# no handling for it at all. `davey` is imported from within discord.py's voice stack
# rather than at package import time, so we do not want to rely on PyInstaller's
# module graph discovering it. Declare it as a hidden import and defensively collect
# any shared libraries that ship inside the wheel. Collecting a library PyInstaller
# would have found anyway is harmless (TOC entries are de-duplicated); missing one is
# a hard runtime failure when joining a voice channel.
try:
    davey_binaries = collect_dynamic_libs('davey')
except Exception:  # davey not installed / not importable at build time
    davey_binaries = []

a = Analysis(
    [os.path.join(DATAPATH, 'main.pyw')],
    pathex=[DATAPATH],
    binaries=davey_binaries,
    datas=[(os.path.join(DATAPATH, 'assets'), './assets')],
    hiddenimports=['PyQt6', 'discord', 'sounddevice', 'davey'],
    hookspath=[SPECPATH],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'tkinter', 'tcl'],
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------------------
# Trim Qt6 payload
# ---------------------------------------------------------------------------
# Deliberately conservative. PyInstaller 6's Qt hooks are far more precise than the
# PyInstaller 5 ones the original 44-entry exclusion list was written against, so most
# of the old exclusions are no-ops here; anything that IS still collected is collected
# because something links against it. Only families this app provably cannot reach are
# stripped: the QML/Quick/Quick3D stack, the Qt tooling libraries, and the software /
# ANGLE GL blobs (opengl32sw.dll alone is ~20 MB and the Qt5 build shipped without it
# for years). The app is QtWidgets-only and renders through the raster paint engine.
#
# Everything else from the old list was dropped on purpose - see the report / commit
# message. In particular Qt6Network, Qt6DBus, Qt6Multimedia, Qt6PrintSupport, Qt6Sql,
# Qt6Xml and the connectivity/sensor family are NOT stripped, because they are cheap
# and a mis-strip is an unusable exe. Note also that gui.py imports
# PyQt6.QtSvgWidgets, so Qt6Svg / Qt6SvgWidgets must never be excluded.

EXCLUDED_BINARIES = {
    # Software OpenGL and ANGLE - unused by a raster QtWidgets app.
    'opengl32sw.dll',
    'd3dcompiler_47.dll',
    'libegl.dll',
    'libglesv2.dll',
    # Qt tooling libraries - never loaded by a deployed app.
    'qt6test.dll',
    'qt6designer.dll',
    'qt6help.dll',
}

# The whole QML / Qt Quick / Qt Quick 3D / Qt Labs stack. This project contains no
# .qml files and imports no PyQt6.Qt* QML module.
EXCLUDED_BINARY_PREFIXES = (
    'qt6qml',
    'qt6quick',
    'qt63d',
    'qt6labs',
)


def _keep_binary(entry):
    name = os.path.basename(entry[0]).lower()
    if name in EXCLUDED_BINARIES:
        return False
    return not name.startswith(EXCLUDED_BINARY_PREFIXES)


a.binaries = [entry for entry in a.binaries if _keep_binary(entry)]

# Drop the bundled Qt translation catalogues (PyQt6\Qt6\translations\*.qm). The UI is
# English-only. dest_name uses the host path separator, so normalise before matching.
a.datas = [
    entry for entry in a.datas
    if 'pyqt6/qt6/translations/' not in entry[0].replace('\\', '/').lower()
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='dap',
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
    icon=os.path.join(DATAPATH, 'assets', 'favicon.ico'),
)
