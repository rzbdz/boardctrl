"""Role providers, site plugins, debug and sessions: the kernel of a board host.

``sys.path`` setup for the tests below: the repository root, where `core/`, `modules/` and
`frontends/` live, so they need no installed package.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
