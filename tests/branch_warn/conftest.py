# Test fixtures for branch-warn. Adds the plugin's scripts/ dir to
# sys.path so `from _lib import ...` works in tests.

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parent.parent.parent / "plugins" / "branch-warn" / "scripts"
)
if str(_PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SCRIPTS))
