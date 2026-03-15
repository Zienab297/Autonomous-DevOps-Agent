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
        from core.models import ...
        from ingestion.loader import ...
        from knowledge_core.retriever import ...
"""

import sys
import pathlib

# walk up until we find the folder that contains 'core' and 'ingestion'
_current = pathlib.Path(__file__).resolve().parent
while _current != _current.parent:
    if (_current / "core").exists() and (_current / "ingestion").exists():
        ROOT = _current
        break
    _current = _current.parent
else:
    raise RuntimeError("Could not find knowledge_agent root directory")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))