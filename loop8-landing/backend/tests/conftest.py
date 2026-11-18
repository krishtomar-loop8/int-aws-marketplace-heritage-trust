"""Makes ``loop8-landing/backend/handler.py`` importable as a plain ``handler``
module from this directory's tests.

``loop8-landing`` has a hyphen, so it can never be a dotted Python package
(``import loop8-landing...`` is not valid syntax) - unlike ``backend/`` and
``services/`` at the repo root, which are real packages (``__init__.py``)
importable via ``backend.api...`` because the repo root has no ``__init__.py``
of its own. This keeps the landing-page backend independent of the main
product's package tree, per the task brief's "don't create a cross-directory
dependency" guidance - a small local ``sys.path`` addition instead of either
importing through ``backend.*`` or adding ``__init__.py`` files here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
