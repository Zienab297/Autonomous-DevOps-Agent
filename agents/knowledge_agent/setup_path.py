"""
setup_path.py
-------------
Import this file FIRST in any file that needs to access project modules.

Usage (first line of any file):
    from setup_path import *

What it does:
    - Finds the knowledge_agent/ root automatically
    - Adds it to sys.path once
    - After this, all imports work normally:
        from shared.models import ...
        from ingestion.loader import ...
        from knowledge_core.retriever import ...
"""

import sys
import pathlib

# search for the folder named 'knowledge_agent'
_current = pathlib.Path(__file__).resolve().parent
while _current != _current.parent:
    if _current.name == "knowledge_agent":
        ROOT = _current
        break
    _current = _current.parent
else:
    # fallback - look for folder containing shared/ and ingestion/
    _current = pathlib.Path(__file__).resolve().parent
    while _current != _current.parent:
        if (_current / "shared").exists() and (_current / "ingestion").exists():
            ROOT = _current
            break
        _current = _current.parent
    else:
        raise RuntimeError("Could not find knowledge_agent root directory")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT)) 