"""Minimal `.env` reader, so the token does not have to be exported by hand.

Deliberately not `python-dotenv`: the bot runs inside pipeline containers where
the fewer dependencies, the better. Values already present in the environment
always win, because that is where a real deployment injects its secrets.
"""

from __future__ import annotations

import os
from typing import Dict, Optional


def load_env_file(path: Optional[str] = None, override: bool = False) -> Dict[str, str]:
    """Load `KEY=value` lines from `path` into `os.environ`."""
    path = path or ".env"
    loaded: Dict[str, str] = {}
    if not os.path.exists(path):
        return loaded

    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if override or key not in os.environ:
                os.environ[key] = value
            loaded[key] = value
    return loaded
