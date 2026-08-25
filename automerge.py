#!/usr/bin/env python3
"""Automerge -- Bitbucket Cloud automerge bot for this repository's workflow.

A pull request to `develop` is merged only when its pipelines are green and
every file it touches is covered by an approval from whoever owns it, with an
escape hatch for the files whose owner is one single person and for the files
nobody owns. See README.md.

    python automerge.py check --pr 407
    python automerge.py run
"""

import os
import sys

# The modules sit next to this file, so the bot can be launched from anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli import main  # noqa: E402  (import after the path is set up)

__version__ = "1.0.0"

if __name__ == "__main__":
    sys.exit(main())
