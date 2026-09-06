"""Pytest bootstrap for the repository root.

Without this file, a bare `pytest` invocation puts only `tests/` on sys.path
(importmode=prepend), so `import fpl_tools` fails during collection. `python -m
pytest` happens to work because that form also prepends the working directory —
which is why the failure went unnoticed. Adding the repo root here makes both
invocations behave identically.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
