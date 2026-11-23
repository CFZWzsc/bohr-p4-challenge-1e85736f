#!/usr/bin/env python3
"""Thin backward-compatible wrapper for scripts/sk_entry.py.

The grading harness accepts either entry point; keeping this file means an
older invocation such as ``python scripts/run_skill.py --case ... --output
...`` keeps working while all logic lives in ``sk_entry.py``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sk_entry import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
