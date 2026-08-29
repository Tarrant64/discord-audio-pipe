# -*- coding: utf-8 -*-
"""Windows VERSIONINFO resource for the modern (PyInstaller 6.x) build.

WHY THIS IS A .py MODULE AND NOT A pyi-grab_version TEXT FILE
------------------------------------------------------------
``EXE(version=...)`` accepts either a path to a version-info text file or an
already-constructed ``VSVersionInfo`` object.  When given a path, PyInstaller
loads it with ``versioninfo.load_version_info_from_text_file()``, which
``eval()``s the file as a *single expression* -- so a text file cannot read an
environment variable or do any computation.  Because we want the build number
and the git provenance strings to come from CI, this is an importable module
that builds and returns the ``VSVersionInfo`` object instead.  ``build/main.spec``
loads it and passes the object straight to ``EXE(version=...)``.

The structure below is exactly the one ``pyi-grab_version`` emits; only the way
it is fed to PyInstaller differs.

FAILURE POLICY
--------------
A build must never fail because version derivation failed.  Every value read
from the environment is parsed defensively and falls back to a static default,
and ``main.spec`` additionally wraps the whole load in ``try/except`` so a build
on a machine where this module cannot even be imported (e.g. a non-Windows host
where ``PyInstaller.utils.win32`` is unavailable) simply produces an exe with no
version resource rather than erroring out.

ENVIRONMENT VARIABLES (all optional)
------------------------------------
DAP_VERSION       Full dotted numeric override, e.g. "2.5.1" or "2.5.1.40".
                  Missing trailing components are zero-filled.
DAP_BUILD_NUMBER  Integer used as the 4th version component when DAP_VERSION
                  does not itself supply one.  CI sets this to github.run_number.
DAP_GIT_SHA       Full commit sha; the first 7 chars go into the version strings.
DAP_GIT_REF       Branch or tag name, recorded alongside the sha.
"""

import os

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

# Static fallback. `git describe --tags` on this fork reports 2.5-<n>-g<sha>:
# 2.5 is the last upstream release tag, so the 2.5.x lineage is the honest base.
# The 4th component is the build number and is filled in from CI when available.
BASE_VERSION = (2, 5, 0, 0)

# Windows VERSIONINFO constants (see MSDN VS_FIXEDFILEINFO).
VOS_NT_WINDOWS32 = 0x40004
VFT_APP = 0x1

# US English / Unicode (0x0409, codepage 1200 == 0x04B0).
LANG_CODEPAGE = "040904B0"
LANGID = 0x0409
CODEPAGE = 1200


def _int_or_none(raw):
    """int(raw) but None instead of an exception for anything unusable."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # VERSIONINFO components are 16-bit.
    if 0 <= value <= 0xFFFF:
        return value
    return None


def _numeric_version():
    """Return the 4-tuple of 16-bit ints for filevers/prodvers.

    Never raises: any malformed input degrades to BASE_VERSION.
    """
    parts = None
    supplied = 0

    override = os.environ.get("DAP_VERSION", "").strip()
    if override:
        # Tolerate a leading "v" and a trailing "+meta" / "-suffix".
        cleaned = override.lstrip("vV").split("+", 1)[0].split("-", 1)[0]
        parsed = [_int_or_none(chunk) for chunk in cleaned.split(".")[:4]]
        if parsed and parsed[0] is not None:
            # Zero-fill unparseable components rather than bailing out entirely.
            parts = [(value if value is not None else 0) for value in parsed]
            supplied = len(parts)
        # An unparseable DAP_VERSION falls through to BASE_VERSION below; it must
        # not also suppress DAP_BUILD_NUMBER.

    if parts is None:
        parts = list(BASE_VERSION)
        # BASE_VERSION's 4th component is a placeholder for the build number.
        supplied = 3

    parts = (parts + [0, 0, 0, 0])[:4]

    if supplied < 4:
        build_number = _int_or_none(os.environ.get("DAP_BUILD_NUMBER"))
        if build_number is not None:
            parts[3] = build_number

    return tuple(parts)


def _provenance():
    """Human-readable ' (ref @ sha)' suffix, or '' when nothing is known."""
    sha = os.environ.get("DAP_GIT_SHA", "").strip()[:7]
    ref = os.environ.get("DAP_GIT_REF", "").strip()
    if sha and ref:
        return " ({0} @ {1})".format(ref, sha)
    if sha:
        return " ({0})".format(sha)
    if ref:
        return " ({0})".format(ref)
    return " (local build)"


def build_version_info():
    """Construct the VSVersionInfo passed to EXE(version=...)."""
    numeric = _numeric_version()
    version_string = ".".join(str(component) for component in numeric)
    display_version = version_string + _provenance()

    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=numeric,
            prodvers=numeric,
            mask=0x3F,
            flags=0x0,
            OS=VOS_NT_WINDOWS32,
            fileType=VFT_APP,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable(
                    LANG_CODEPAGE,
                    [
                        # CompanyName is deliberately omitted: this fork has no
                        # company behind it and inventing one would be a lie
                        # baked into every binary.
                        StringStruct("FileDescription", "Discord Audio Pipe"),
                        StringStruct("FileVersion", display_version),
                        StringStruct("InternalName", "dap"),
                        StringStruct(
                            "LegalCopyright",
                            "Copyright (c) 2018 QiCuiHub. MIT License. "
                            "Modernized fork of QiCuiHub/discord-audio-pipe.",
                        ),
                        StringStruct("OriginalFilename", "dap.exe"),
                        StringStruct("ProductName", "Discord Audio Pipe"),
                        StringStruct("ProductVersion", display_version),
                        StringStruct(
                            "Comments",
                            "Pipes a system audio device into a Discord voice "
                            "channel. Built with PyInstaller 6.x / PyQt6.",
                        ),
                    ],
                )
            ]),
            VarFileInfo([VarStruct("Translation", [LANGID, CODEPAGE])]),
        ],
    )
