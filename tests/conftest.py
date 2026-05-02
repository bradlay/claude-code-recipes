# Test fixtures for plan-review-loop. Adds the plugin's scripts/ dir to
# sys.path so `from _lib import ...` works in tests, mirroring what the
# hook launcher does at runtime.

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "plan-review-loop"
    / "scripts"
)
if str(_PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SCRIPTS))
