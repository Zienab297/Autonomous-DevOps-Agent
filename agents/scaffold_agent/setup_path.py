"""
setup_path.py
-------------
Usage (first line of any file):
    from setup_path import *
"""

import sys
import pathlib

_current = pathlib.Path(__file__).resolve().parent
while _current != _current.parent:
    if _current.name == "scaffold_agent":
        ROOT = _current
        break
    _current = _current.parent
else:
    _current = pathlib.Path(__file__).resolve().parent
    while _current != _current.parent:
        if (_current / "shared").exists() and (_current / "core").exists():
            ROOT = _current
            break
        _current = _current.parent
    else:
        raise RuntimeError("Could not find scaffold_agent root directory")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))