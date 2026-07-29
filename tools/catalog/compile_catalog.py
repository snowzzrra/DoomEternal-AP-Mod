"""Compile catalog projections for APWorld and runtime staging."""

import sys
from pathlib import Path

MOD_ROOT = Path(__file__).resolve().parents[2]
if str(MOD_ROOT) not in sys.path:
    sys.path.insert(0, str(MOD_ROOT))
if str(MOD_ROOT / "tools" / "content") not in sys.path:
    sys.path.insert(0, str(MOD_ROOT / "tools" / "content"))

from tools.content.compile_content_catalog import compile_catalog, main

if __name__ == "__main__":
    raise SystemExit(main())
