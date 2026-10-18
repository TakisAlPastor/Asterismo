# coding=utf-8
"""Vendored audfprint package for acoustic landmark fingerprinting.

Patched for Windows stealth execution, NumPy 2.x compatibility, and Python 3.12+.
I just made some little changes, most of the work isnt't mine.
"""

import sys
from pathlib import Path

# Ensure audfprint's internal flat sibling imports (e.g. `import audio_read`) resolve
_PKG_DIR = str(Path(__file__).resolve().parent)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
