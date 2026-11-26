"""HarenSmartAgent ADK package entrypoint for `adk web` and `adk run`."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure parent project directory is importable when invoked via ADK CLI from project root
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from . import agent  # noqa: E402
from .agent import root_agent  # noqa: E402

__all__ = ["agent", "root_agent"]
