"""Resolve the matching Flight Search Harness runtime for this Skill."""

from __future__ import annotations

import json
import sys
from pathlib import Path


HARNESS_VERSION = "3.6.0"
PRODUCT_ID = "one_way"


def main() -> int:
    sys.dont_write_bytecode = True
    plugin_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(plugin_root))
    from scripts.resolve_skill_runtime import RuntimeResolutionError, resolve

    try:
        value = resolve(
            __file__,
            harness_version=HARNESS_VERSION,
            product_id=PRODUCT_ID,
        )
    except RuntimeResolutionError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
