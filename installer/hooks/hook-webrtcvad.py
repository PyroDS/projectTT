# ------------------------------------------------------------------
# Local override for the pyinstaller-hooks-contrib `hook-webrtcvad.py`.
#
# The contrib hook unconditionally calls ``copy_metadata('webrtcvad')``,
# which fails when the installed package is ``webrtcvad-wheels`` — the
# pre-built-binary fork that ships the same ``webrtcvad`` Python module
# under a different pip distribution name.  Both packages are widely
# used and the contrib bug has been open since 2022.
#
# This hook tries each known distribution name and silently skips
# metadata collection if none is installed.  The webrtcvad module
# itself does not read its own metadata at runtime, so an empty
# `datas` list is harmless.
# ------------------------------------------------------------------

from PyInstaller.utils.hooks import copy_metadata

datas = []
for _dist in ("webrtcvad", "webrtcvad-wheels"):
    try:
        datas = copy_metadata(_dist)
        break
    except Exception:
        continue
