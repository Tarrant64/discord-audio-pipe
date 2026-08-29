"""PyInstaller hook for discord.py.

discord.py ships a pre-built libopus DLL inside the Windows wheel at
``discord/bin/libopus-0.x64.dll``. It is loaded at run time through ctypes, so
PyInstaller's import analysis never sees it and it has to be collected by hand.
``discord.opus`` resolves the bundled library relative to the ``discord`` package
directory, so the destination must stay ``discord/bin``.

The ``discord/bin`` directory only exists in the Windows wheels - on macOS and Linux
discord.py expects a system libopus (``brew install opus`` / ``libopus0``). This hook
therefore no-ops off Windows rather than raising, so the same hook directory can be
reused by a future macOS spec.

NOTE: this hook is shared by both the modern spec (build/main.spec) and the
pre-modernization control spec (build/main-baseline.spec), so it must stay compatible
with PyInstaller 5.x as well as 6.x. Deliberately no `davey` handling here - that is
kept in build/main.spec so the baseline leg stays a faithful control.
"""

import glob
import importlib.util
import os
import sys

datas = []

_spec = importlib.util.find_spec('discord')
if _spec is not None and _spec.origin:
    _module_dir = os.path.dirname(_spec.origin)
    _bin_dir = os.path.join(_module_dir, 'bin')

    if sys.platform == 'win32' and os.path.isdir(_bin_dir):
        for _dll in glob.glob(os.path.join(_bin_dir, 'libopus*.dll')):
            datas.append((_dll, os.path.join('discord', 'bin')))
