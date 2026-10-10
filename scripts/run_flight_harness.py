"""Launch the bundled Flight Search Harness without changing caller state."""

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    plugin_root = str(PLUGIN_ROOT)
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)

    from flight_search_harness.__main__ import main as harness_main

    return harness_main()


if __name__ == "__main__":
    raise SystemExit(main())
