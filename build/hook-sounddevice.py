"""PyInstaller hook for sounddevice.

sounddevice is a single top-level module (``sounddevice.py``) that loads PortAudio
through CFFI at run time from the sibling ``_sounddevice_data`` package. Neither the
data package nor the shared library shows up in PyInstaller's import graph, so both
have to be collected explicitly, preserving the
``_sounddevice_data/portaudio-binaries`` layout that ``sounddevice`` looks for.

The directory layout is unchanged between sounddevice 0.4.6 (baseline leg) and 0.5.6
(modern leg). Guarded so it degrades to a no-op instead of raising if the package is
absent or laid out differently - PyInstaller 6 also ships its own sounddevice hook and
duplicate TOC entries are de-duplicated.

NOTE: this hook is shared by both build/main.spec and build/main-baseline.spec, so it
must stay compatible with PyInstaller 5.x as well as 6.x.
"""

import importlib.util
import os

datas = []

_spec = importlib.util.find_spec('sounddevice')
if _spec is not None and _spec.origin:
    _site_packages = os.path.dirname(_spec.origin)
    _binary_folder = os.path.join('_sounddevice_data', 'portaudio-binaries')
    _source = os.path.join(_site_packages, _binary_folder)

    if os.path.isdir(_source):
        datas.append((_source, _binary_folder))
