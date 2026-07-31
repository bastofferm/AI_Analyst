"""Project-local dependency path helpers."""
from __future__ import annotations

import sys
from pathlib import Path


def add_project_deps() -> None:
    root = Path(__file__).resolve().parents[2]
    for deps in (root / ".torch_deps", root / ".python_deps", root / "xbrl_sec" / "_vendor"):
        if deps.exists():
            dep_path = str(deps)
            if dep_path not in sys.path:
                if deps.name == ".torch_deps":
                    sys.path.insert(0, dep_path)
                else:
                    sys.path.append(dep_path)
